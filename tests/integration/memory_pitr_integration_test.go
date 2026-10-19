//go:build integration

package integration

import (
	"context"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	knowledgev1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/knowledge/v1"
)

// knowledgeProc is one Knowledge process (node dist/main.js) on a given
// database, with only the placements the scenario passes: no ambient
// ANVILKIT_KNOWLEDGE_* variable of the development foundation reaches it.
type knowledgeProc struct {
	t      *testing.T
	cmd    *exec.Cmd
	logs   string
	conn   *grpc.ClientConn
	memory knowledgev1.MemoryServiceClient
	// model proposes as the access sidecar: since P2.7 a proposal's origin
	// is its caller's, and the API workload serves users only.
	model knowledgev1.MemoryServiceClient
	once  sync.Once
}

func startKnowledgeProc(t *testing.T, root, dsn string, placements ...string) *knowledgeProc {
	t.Helper()
	dir := filepath.Join(root, "services", "agent", "knowledge")
	addr, health := "127.0.0.1:"+freePort(t), "127.0.0.1:"+freePort(t)
	k := &knowledgeProc{t: t, logs: filepath.Join(t.TempDir(), "knowledge.log")}
	f, err := os.Create(k.logs)
	require.NoError(t, err)
	k.cmd = exec.Command("node", "dist/main.js")
	k.cmd.Dir = dir
	for _, e := range os.Environ() {
		if !strings.HasPrefix(e, "ANVILKIT_KNOWLEDGE_") {
			k.cmd.Env = append(k.cmd.Env, e)
		}
	}
	k.cmd.Env = append(k.cmd.Env, "ANVILKIT_KNOWLEDGE_CONFIG="+filepath.Join(dir, "config.yaml"), "ANVILKIT_KNOWLEDGE_DATABASE_URL="+dsn,
		"ANVILKIT_KNOWLEDGE_LISTEN="+addr, "ANVILKIT_KNOWLEDGE_HEALTH_LISTEN="+health)
	k.cmd.Env = append(k.cmd.Env, identityEnv(t, "ANVILKIT_KNOWLEDGE_", principalKnowledge)...)
	k.cmd.Env = append(k.cmd.Env, placements...)
	k.cmd.Stdout, k.cmd.Stderr = f, f
	require.NoError(t, k.cmd.Start())
	t.Cleanup(k.stop)
	deadline := time.Now().Add(2 * time.Minute)
	for {
		resp, err := http.Get("http://" + health + "/readyz")
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				break
			}
		}
		if time.Now().After(deadline) || k.cmd.ProcessState != nil {
			raw, _ := os.ReadFile(k.logs)
			t.Fatalf("knowledge did not become ready: %s", tailLines(string(raw), 20))
		}
		time.Sleep(250 * time.Millisecond)
	}
	k.conn = dialKnowledgeAs(t, addr, principalAPI)
	k.memory = knowledgev1.NewMemoryServiceClient(k.conn)
	k.model = knowledgev1.NewMemoryServiceClient(dialKnowledgeAs(t, addr, principalSidecar))
	return k
}

func (k *knowledgeProc) stop() {
	k.once.Do(func() {
		if k.conn != nil {
			k.conn.Close()
		}
		_ = k.cmd.Process.Signal(syscall.SIGTERM)
		done := make(chan struct{})
		go func() { _ = k.cmd.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(30 * time.Second):
			_ = k.cmd.Process.Kill()
			<-done
		}
	})
}

func (k *knowledgeProc) logged(s string) bool {
	raw, _ := os.ReadFile(k.logs)
	return strings.Contains(string(raw), s)
}

// TestMemoryRemovalRecovery (P23-09, deletion/revocation preservation)
// loses Knowledge's database after memory deletions and revocations were
// committed and answered, recovers it from a base backup and the WAL
// archive to a point before them, and proves that none of them is undone:
// every removal was answered only once its record was in the removal
// inventory (the development foundation's MinIO bucket of its own user,
// standing in for the independent DR store); a Knowledge started on the
// restored database while the inventory is unreachable keeps memory closed
// (no fact is read, listed, decided or proposed) although the restore
// revived the removed content; once the inventory answers, every erased
// removal is re-applied under its original command identity before memory
// serves: the deleted facts are gone again, the revoked ones revoked (also
// one whose confirmation the restore erased with it), a fact the restore
// erased altogether cannot be proposed again by its retried command, the
// original commands replay, and removals before the restore point and
// facts never removed are as they were.
func TestMemoryRemovalRecovery(t *testing.T) {
	env := laneEnv(t, "ANVILKIT_KNOWLEDGE_REMOVALS_ENDPOINT", "ANVILKIT_KNOWLEDGE_REMOVALS_CREDENTIALS_FILE")
	root := repoRoot(t)
	if _, err := os.Stat(filepath.Join(root, "services/agent/knowledge/dist/main.js")); err != nil {
		skipMissing(t, "build:services/agent/knowledge/dist", "services/agent/knowledge/dist/main.js missing (pnpm run build in the package)")
	}
	p := startPITRDatabase(t)
	for _, s := range []string{"anvilkit_knowledge_app", "anvilkit_knowledge_migrator", "anvilkit_knowledge_relay", "anvilkit_knowledge_forwarder", "anvilkit_knowledge_cleanup",
		"anvilkit_knowledge_store_migrator", "anvilkit_knowledge_store"} {
		p.psql("postgres", "CREATE ROLE "+s+" LOGIN PASSWORD 'disposable'")
	}
	p.psql("postgres", "CREATE DATABASE anvilkit_knowledge OWNER anvilkit_knowledge_migrator")
	dsn := func(role string) string {
		return "postgres://" + role + ":disposable@127.0.0.1:" + p.port + "/anvilkit_knowledge?sslmode=disable"
	}
	migrate := exec.Command("go", "run", "./cmd/anvilkit-migration", "-domain", "knowledge", "-development", "-dsn", dsn("anvilkit_knowledge_migrator"))
	migrate.Dir = filepath.Join(root, "jobs", "migration")
	out, err := migrate.CombinedOutput()
	require.NoError(t, err, string(out))
	p.baseBackup()
	sql := func(q string) string { return p.psql("anvilkit_knowledge", q) }
	inventory := []string{"ANVILKIT_KNOWLEDGE_REMOVALS_ENDPOINT=" + env["ANVILKIT_KNOWLEDGE_REMOVALS_ENDPOINT"],
		"ANVILKIT_KNOWLEDGE_REMOVALS_CREDENTIALS_FILE=" + env["ANVILKIT_KNOWLEDGE_REMOVALS_CREDENTIALS_FILE"]}
	k := startKnowledgeProc(t, root, dsn("anvilkit_knowledge_app"), inventory...)
	ctx := context.Background()

	run := strconv.FormatInt(time.Now().UnixNano(), 36)
	tenant := "pitr-" + run
	scope := func(actor string) *knowledgev1.Scope {
		return &knowledgev1.Scope{TenantId: tenant, ProjectId: "project_pitr", ActorId: actor}
	}
	seq := 0
	command := func(actor string) *knowledgev1.CommandIdentity {
		seq++
		id := "p23-" + run + "-" + strconv.Itoa(seq)
		return &knowledgev1.CommandIdentity{TenantId: tenant, CommandId: id, ActorId: actor, RequestDigest: sha([]byte(id))}
	}
	propose := func(m knowledgev1.MemoryServiceClient, cmd *knowledgev1.CommandIdentity, content string) (*knowledgev1.MemoryFact, error) {
		r, err := m.ProposeFact(ctx, &knowledgev1.ProposeFactRequest{Command: cmd, Scope: scope("agent-coder"), SubjectType: "actor",
			SubjectId: "alice", Content: content, Origin: knowledgev1.FactOrigin_FACT_ORIGIN_MODEL})
		return r.GetFact(), err
	}
	decide := func(m knowledgev1.MemoryServiceClient, cmd *knowledgev1.CommandIdentity, factID, rev string, d knowledgev1.FactDecision) (*knowledgev1.DecideFactResponse, error) {
		return m.DecideFact(ctx, &knowledgev1.DecideFactRequest{Command: cmd, Scope: scope("alice"), FactId: factID, ExpectedRevision: rev, Decision: d})
	}
	remove := func(m knowledgev1.MemoryServiceClient, cmd *knowledgev1.CommandIdentity, factID, rev string) (*knowledgev1.DeleteFactResponse, error) {
		return m.DeleteFact(ctx, &knowledgev1.DeleteFactRequest{Command: cmd, Scope: scope("alice"), FactId: factID, ExpectedRevision: rev})
	}
	get := func(m knowledgev1.MemoryServiceClient, factID string) (*knowledgev1.MemoryFact, error) {
		r, err := m.GetFact(ctx, &knowledgev1.GetFactRequest{Scope: scope("alice"), FactId: factID})
		return r.GetFact(), err
	}
	confirm, revoke := knowledgev1.FactDecision_FACT_DECISION_CONFIRM, knowledgev1.FactDecision_FACT_DECISION_REVOKE
	confirmed := func(content string) *knowledgev1.MemoryFact {
		f, err := propose(k.model, command("agent-coder"), content)
		require.NoError(t, err)
		d, err := decide(k.memory, command("alice"), f.GetFactId(), "1", confirm)
		require.NoError(t, err)
		return d.GetFact()
	}

	// Before the restore point: four confirmed facts, one of them deleted, and a proposal.
	a := confirmed("alice prefers tabs (recovery drill)")
	b := confirmed("alice prefers short commits (recovery drill)")
	c := confirmed("alice prefers dark slides (recovery drill)")
	g := confirmed("alice prefers green builds (recovery drill)")
	_, err = remove(k.memory, command("alice"), c.GetFactId(), "2")
	require.NoError(t, err)
	d, err := propose(k.model, command("agent-coder"), "alice prefers rebases (recovery drill)")
	require.NoError(t, err)
	require.Equal(t, "recorded", sql("SELECT state FROM memory_removals WHERE fact_id = '"+c.GetFactId()+"'"))
	target := p.mark("memory-pitr-" + run)

	// After it: four removals, each answered only once recorded.
	delA := command("alice")
	_, err = remove(k.memory, delA, a.GetFactId(), "2")
	require.NoError(t, err)
	revB := command("alice")
	_, err = decide(k.memory, revB, b.GetFactId(), "2", revoke)
	require.NoError(t, err)
	confD := command("alice")
	_, err = decide(k.memory, confD, d.GetFactId(), "1", confirm)
	require.NoError(t, err)
	_, err = decide(k.memory, command("alice"), d.GetFactId(), "2", revoke)
	require.NoError(t, err)
	eProp := command("agent-coder")
	e, err := propose(k.model, eProp, "alice prefers spaces (recovery drill)")
	require.NoError(t, err)
	_, err = decide(k.memory, command("alice"), e.GetFactId(), "1", confirm)
	require.NoError(t, err)
	_, err = remove(k.memory, command("alice"), e.GetFactId(), "2")
	require.NoError(t, err)
	require.Equal(t, "5", sql("SELECT count(*) FROM memory_removals WHERE state = 'recorded' AND tenant_id = '"+tenant+"'"),
		"every removal's record was confirmed by the inventory before its answer")
	_, err = get(k.memory, a.GetFactId())
	require.Equal(t, codes.NotFound, status.Code(err))
	scopeID := sql("SELECT scope_id FROM memory_removal_scope")

	// Lose the database and recover it to the restore point.
	p.archiveCurrent()
	k.stop()
	p.restore(target)
	// Where the replay stopped, in the restored server's own words.
	t.Logf("recovery target: restore point %s", target)
	if raw, err := exec.Command("docker", "logs", p.name).CombinedOutput(); err == nil {
		for _, line := range strings.Split(string(raw), "\n") {
			for _, mark := range []string{"starting point-in-time recovery", "recovery stopping", "last completed transaction", "redo done", "consistent recovery state"} {
				if strings.Contains(line, mark) {
					t.Logf("restored server: %s", strings.TrimSpace(line))
				}
			}
		}
	}
	require.Equal(t, scopeID, sql("SELECT scope_id FROM memory_removal_scope"), "the restore keeps the database's removal scope")
	require.Equal(t, "confirmed|false|true", sql("SELECT state || '|' || deleted || '|' || (content <> '') FROM memory_facts WHERE fact_id = '"+a.GetFactId()+"'"),
		"the restore revived the deleted content")
	require.Equal(t, "1", sql("SELECT count(*) FROM memory_removals WHERE tenant_id = '"+tenant+"'"), "only the removal before the restore point survived")
	require.Equal(t, "0", sql("SELECT count(*) FROM memory_facts WHERE fact_id = '"+e.GetFactId()+"'"))

	t.Run("a Knowledge on the restored database keeps memory closed while the removal inventory is unreachable", func(t *testing.T) {
		down := startKnowledgeProc(t, root, dsn("anvilkit_knowledge_app"),
			"ANVILKIT_KNOWLEDGE_REMOVALS_ENDPOINT=http://127.0.0.1:"+freePort(t),
			"ANVILKIT_KNOWLEDGE_REMOVALS_CREDENTIALS_FILE="+env["ANVILKIT_KNOWLEDGE_REMOVALS_CREDENTIALS_FILE"])
		defer down.stop()
		_, err := get(down.memory, a.GetFactId())
		require.Equal(t, codes.Unavailable, status.Code(err), "the revived content is not read")
		_, err = down.memory.ListFacts(ctx, &knowledgev1.ListFactsRequest{Scope: scope("alice"), Limit: 10})
		require.Equal(t, codes.Unavailable, status.Code(err))
		_, err = remove(down.memory, command("alice"), b.GetFactId(), "2")
		require.Equal(t, codes.Unavailable, status.Code(err))
		_, err = propose(down.model, eProp, "alice prefers spaces (recovery drill)")
		require.Equal(t, codes.Unavailable, status.Code(err))
		require.True(t, down.logged("removal inventory not reconciled at start: memory is closed"))
		require.Equal(t, "confirmed", sql("SELECT state FROM memory_facts WHERE fact_id = '"+a.GetFactId()+"'"), "nothing decided while closed")
	})

	t.Run("once the inventory answers every erased removal is re-applied under its original command before memory serves", func(t *testing.T) {
		up := startKnowledgeProc(t, root, dsn("anvilkit_knowledge_app"), inventory...)
		defer up.stop()
		var aErr error
		require.Eventually(t, func() bool {
			_, aErr = get(up.memory, a.GetFactId())
			return status.Code(aErr) != codes.Unavailable
		}, time.Minute, 250*time.Millisecond)
		require.Equal(t, codes.NotFound, status.Code(aErr), "the deleted fact is gone again")
		require.Equal(t, "true|", sql("SELECT deleted || '|' || content FROM memory_facts WHERE fact_id = '"+a.GetFactId()+"'"))
		require.Equal(t, "delete|alice", sql("SELECT decision || '|' || decider FROM memory_decisions WHERE command_id = '"+delA.GetCommandId()+"' AND tenant_id = '"+tenant+"'"))
		bf, err := get(up.memory, b.GetFactId())
		require.NoError(t, err)
		require.Equal(t, knowledgev1.FactState_FACT_STATE_REVOKED, bf.GetState())
		df, err := get(up.memory, d.GetFactId())
		require.NoError(t, err)
		require.Equal(t, knowledgev1.FactState_FACT_STATE_REVOKED, df.GetState(), "revoked although the restore erased its confirmation too")
		require.Equal(t, "alice", df.GetConfirmer())
		_, err = decide(up.memory, confD, d.GetFactId(), "1", confirm)
		require.Error(t, err, "the retried confirmation the restore erased never revives it")
		_, err = decide(up.memory, command("alice"), d.GetFactId(), df.GetRevision(), confirm)
		require.Equal(t, codes.FailedPrecondition, status.Code(err))
		_, err = get(up.memory, e.GetFactId())
		require.Equal(t, codes.NotFound, status.Code(err))
		_, err = propose(up.model, eProp, "alice prefers spaces (recovery drill)")
		require.Equal(t, codes.FailedPrecondition, status.Code(err), "a retried proposal of a fact the restore erased stays removed")
		require.Equal(t, "0", sql("SELECT count(*) FROM memory_facts WHERE fact_id = '"+e.GetFactId()+"'"))
		replay, err := remove(up.memory, delA, a.GetFactId(), "2")
		require.NoError(t, err, "the original deletion command replays")
		require.True(t, replay.GetExisting())
		_, err = get(up.memory, c.GetFactId())
		require.Equal(t, codes.NotFound, status.Code(err), "the removal before the restore point stays")
		gf, err := get(up.memory, g.GetFactId())
		require.NoError(t, err)
		require.Equal(t, knowledgev1.FactState_FACT_STATE_CONFIRMED, gf.GetState(), "a fact never removed is untouched")
		require.Equal(t, "4", sql("SELECT count(*) FROM memory_removals WHERE state = 'restored' AND tenant_id = '"+tenant+"'"))
		listed, err := up.memory.ListFacts(ctx, &knowledgev1.ListFactsRequest{Scope: scope("alice"), Limit: 50})
		require.NoError(t, err)
		ids := map[string]bool{}
		for _, f := range listed.GetFacts() {
			ids[f.GetFactId()] = true
		}
		for _, id := range []string{a.GetFactId(), c.GetFactId(), e.GetFactId()} {
			require.False(t, ids[id], "a deleted fact is never listed")
		}
		require.True(t, ids[g.GetFactId()] && ids[b.GetFactId()] && ids[d.GetFactId()])
		require.True(t, up.logged("a removal a database restore erased was re-applied"))
	})
}
