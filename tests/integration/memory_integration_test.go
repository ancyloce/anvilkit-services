//go:build integration

package integration

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"github.com/testcontainers/testcontainers-go"
	"github.com/testcontainers/testcontainers-go/wait"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	knowledgev1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/knowledge/v1"
)

// P17 on the development foundation: MemoryFact through the real lane —
// MemoryService on Knowledge, the decision and its memory-project requests
// committed with their outbox events, the forwarder, JetStream, the owner
// queue relay, the Background Worker's memory-project handler, the
// PostgresStore projection (its vendor schema migrated by its own identity)
// and memory points in the accepted Qdrant generation embedded by the real
// BGE-M3 Inference service — and RecallFacts under the current authority.
// Asserted: zero false confirmations over the adversarial decisions, zero
// tenant contamination or unauthorized disclosure over every recall trial,
// zero stale revival (no content in the Store and no point for any removed
// fact) and exactly one point per live fact, the deletion purge marker, the
// reviewed expiry rule, and a Store rebuild through memoryctl that never
// revives a removal. Reported, never asserted: hit rate, no-answer behavior,
// latency, projection lag and purge time on the DEVELOPMENT_ONLY cases of
// tests/evals/memory.

type memFact struct {
	ID          string `json:"id"`
	Tenant      string `json:"tenant"`
	SubjectType string `json:"subjectType"`
	Subject     string `json:"subject"`
	Origin      string `json:"origin"`
	Proposer    string `json:"proposer"`
	Decider     string `json:"decider"`
	Fate        string `json:"fate"`
	Content     string `json:"content"`
}

type memRecall struct {
	ID        string   `json:"id"`
	Stratum   string   `json:"stratum"`
	Tenant    string   `json:"tenant"`
	As        string   `json:"as"`
	Query     string   `json:"query"`
	Expected  []string `json:"expected"`
	Forbidden []string `json:"forbidden"`
}

type memCases struct {
	SchemaVersion int         `json:"schemaVersion"`
	Trials        int         `json:"trials"`
	K             int         `json:"k"`
	Project       string      `json:"project"`
	Facts         []memFact   `json:"facts"`
	Recalls       []memRecall `json:"recalls"`
}

type memAttempt struct {
	Name      string `json:"name"`
	Code      string `json:"code"`
	Succeeded bool   `json:"succeeded"`
}

type memTrial struct {
	Trial       int      `json:"trial"`
	LatencyMs   float64  `json:"latencyMs"`
	NoAnswer    bool     `json:"noAnswer"`
	Returned    []string `json:"returned"`
	HitRate     *float64 `json:"expectedHitRate,omitempty"`
	Disclosures int      `json:"unauthorizedDisclosures"`
	Failures    []string `json:"failureClasses"`
}

type memCaseReport struct {
	ID      string     `json:"id"`
	Stratum string     `json:"stratum"`
	Trials  []memTrial `json:"trials"`
}

// ownQdrant starts a disposable Qdrant of the pinned foundation image with an API key.
func ownQdrant(t *testing.T) (string, string) {
	t.Helper()
	ctx := context.Background()
	key := "p17-" + strconv.FormatInt(time.Now().UnixNano(), 36)
	c, err := testcontainers.GenericContainer(ctx, testcontainers.GenericContainerRequest{ContainerRequest: testcontainers.ContainerRequest{
		Image:        "qdrant/qdrant:v1.19.0@sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc",
		ExposedPorts: []string{"6333/tcp"},
		Env:          map[string]string{"QDRANT__SERVICE__API_KEY": key, "QDRANT__TELEMETRY_DISABLED": "true"},
		WaitingFor:   wait.ForHTTP("/readyz").WithPort("6333/tcp").WithStartupTimeout(time.Minute),
	}, Started: true})
	require.NoError(t, err)
	t.Cleanup(func() { _ = c.Terminate(ctx) })
	endpoint, err := c.PortEndpoint(ctx, "6333/tcp", "http")
	require.NoError(t, err)
	return endpoint, key
}

func grpcCode(err error) string {
	if err == nil {
		return "OK"
	}
	return status.Code(err).String()
}

func TestMemory(t *testing.T) {
	laneEnv(t, "ANVILKIT_KNOWLEDGE_OBJECTS_ENDPOINT", "ANVILKIT_KNOWLEDGE_OBJECTS_STAGE_ENDPOINT", "ANVILKIT_KNOWLEDGE_OBJECTS_CREDENTIALS_FILE",
		"ANVILKIT_KNOWLEDGE_KUBECONFIG", "ANVILKIT_KNOWLEDGE_IMAGE_REGISTRY", "ANVILKIT_DEV_KNOWLEDGE_MIGRATOR_DSN", "ANVILKIT_DEV_INFERENCE_URL",
		"ANVILKIT_KNOWLEDGE_QDRANT_URL", "ANVILKIT_KNOWLEDGE_QDRANT_API_KEY", "ANVILKIT_DEV_KNOWLEDGE_STORE_DSN")
	root := repoRoot(t)
	if _, err := os.Stat(filepath.Join(root, "services/agent/knowledge/dist/memoryctl.js")); err != nil {
		skipMissing(t, "build:services/agent/knowledge/dist", "services/agent/knowledge/dist/memoryctl.js missing (pnpm run build in the package)")
	}
	raw, err := os.ReadFile(filepath.Join(root, "tests", "evals", "memory", "cases.json"))
	require.NoError(t, err)
	var cases memCases
	require.NoError(t, json.Unmarshal(raw, &cases))
	casesDigest := sha(raw)
	infURL := inferenceURL(t)
	// The scenario owns its Qdrant like the lane owns its database: the
	// lane's fresh database numbers generations from 1, so a shared node's
	// earlier collections would never qualify.
	qdrantURL, qdrantKey := ownQdrant(t)
	t.Setenv("ANVILKIT_KNOWLEDGE_QDRANT_URL", qdrantURL)
	t.Setenv("ANVILKIT_KNOWLEDGE_QDRANT_API_KEY", qdrantKey)
	l := setupLaneWith(t, laneOptions{
		knowledgeReplacements: [][2]string{{`  profile: ""`, "  profile: parser-docling-dev-v1"}, {"  node_pool: parsing", "  node_pool: components"}, inferenceTLS(t)},
		knowledgeEnv: []string{
			"ANVILKIT_KNOWLEDGE_OBJECTS_ENDPOINT=" + os.Getenv("ANVILKIT_KNOWLEDGE_OBJECTS_ENDPOINT"),
			"ANVILKIT_KNOWLEDGE_OBJECTS_STAGE_ENDPOINT=" + os.Getenv("ANVILKIT_KNOWLEDGE_OBJECTS_STAGE_ENDPOINT"),
			"ANVILKIT_KNOWLEDGE_OBJECTS_CREDENTIALS_FILE=" + os.Getenv("ANVILKIT_KNOWLEDGE_OBJECTS_CREDENTIALS_FILE"),
			"ANVILKIT_KNOWLEDGE_KUBECONFIG=" + os.Getenv("ANVILKIT_KNOWLEDGE_KUBECONFIG"),
			"ANVILKIT_KNOWLEDGE_IMAGE_REGISTRY=" + os.Getenv("ANVILKIT_KNOWLEDGE_IMAGE_REGISTRY"),
			"ANVILKIT_KNOWLEDGE_INFERENCE_URL=" + infURL,
			"ANVILKIT_KNOWLEDGE_QDRANT_URL=" + os.Getenv("ANVILKIT_KNOWLEDGE_QDRANT_URL"),
			"ANVILKIT_KNOWLEDGE_QDRANT_API_KEY=" + os.Getenv("ANVILKIT_KNOWLEDGE_QDRANT_API_KEY"),
		},
	})
	conn := dialKnowledgeAs(t, l.knowledgeAddr, principalAPI)
	memory := knowledgev1.NewMemoryServiceClient(conn)
	// P2.7: a proposal's origin is its caller's: a model proposes over the
	// model route (the access sidecar's identity), a worker over the worker
	// route; the API serves users.
	proposers := map[knowledgev1.FactOrigin]knowledgev1.MemoryServiceClient{
		knowledgev1.FactOrigin_FACT_ORIGIN_USER:   memory,
		knowledgev1.FactOrigin_FACT_ORIGIN_MODEL:  knowledgev1.NewMemoryServiceClient(dialKnowledgeAs(t, l.knowledgeAddr, principalSidecar)),
		knowledgev1.FactOrigin_FACT_ORIGIN_WORKER: knowledgev1.NewMemoryServiceClient(dialKnowledgeAs(t, l.knowledgeAddr, principalWorker)),
	}
	propose := func(ctx context.Context, req *knowledgev1.ProposeFactRequest) (*knowledgev1.ProposeFactResponse, error) {
		return proposers[req.GetOrigin()].ProposeFact(ctx, req)
	}
	migrator := os.Getenv("ANVILKIT_DEV_KNOWLEDGE_MIGRATOR_DSN")
	storeDSN := os.Getenv("ANVILKIT_DEV_KNOWLEDGE_STORE_DSN")
	ctx := context.Background()

	run := strconv.FormatInt(time.Now().Unix(), 36)
	tenants := map[string]string{"a": "mem-" + run + "-a", "b": "mem-" + run + "-b"}
	scope := func(tenant, actor string) *knowledgev1.Scope {
		// P2.7: users confirm project and tenant facts under the verified confirm role.
		return &knowledgev1.Scope{TenantId: tenant, ProjectId: cases.Project, ActorId: actor, Roles: []string{"memory_confirmer"}}
	}
	seq := 0
	var seqMu sync.Mutex
	command := func(tenant, actor string) *knowledgev1.CommandIdentity {
		seqMu.Lock()
		seq++
		id := fmt.Sprintf("p17-%s-%d", run, seq)
		seqMu.Unlock()
		return &knowledgev1.CommandIdentity{TenantId: tenant, CommandId: id, ActorId: actor, RequestDigest: sha([]byte(id))}
	}
	origins := map[string]knowledgev1.FactOrigin{"user": knowledgev1.FactOrigin_FACT_ORIGIN_USER, "model": knowledgev1.FactOrigin_FACT_ORIGIN_MODEL, "worker": knowledgev1.FactOrigin_FACT_ORIGIN_WORKER}

	// The Index Builder creates and accepts the generation of the reviewed space.
	var collection, generation string
	l.eventually(5*time.Minute, "an accepted index generation", func() bool {
		rows := l.query(migrator, "SELECT generation::text AS g, collection_name FROM index_generations WHERE state = 'accepted'")
		if len(rows) != 1 {
			return false
		}
		generation, collection = rows[0]["g"].(string), rows[0]["collection_name"].(string)
		return true
	})

	factOf := map[string]*knowledgev1.MemoryFact{} // case fact id -> fact
	idOf := map[string]string{}                    // fact id -> case fact id
	caseOf := map[string]memFact{}
	confirmedAt := map[string]time.Time{}
	var attempts []memAttempt
	attempt := func(name string, err error) {
		attempts = append(attempts, memAttempt{Name: name, Code: grpcCode(err), Succeeded: err == nil})
	}

	t.Run("facts are proposed by their model, worker or user identities and decided by their users", func(t *testing.T) {
		for _, f := range cases.Facts {
			tenant := tenants[f.Tenant]
			subject := f.Subject
			if subject == "$tenant" {
				subject = tenant
			}
			req := &knowledgev1.ProposeFactRequest{
				Command: command(tenant, f.Proposer), Scope: scope(tenant, f.Proposer), SubjectType: f.SubjectType, SubjectId: subject,
				Content: f.Content, Origin: origins[f.Origin],
			}
			if f.Fate == "expired" {
				req.ExpiresAt = timestamppb.New(time.Now().Add(45 * time.Second))
			}
			p, err := propose(ctx, req)
			require.NoError(t, err, f.ID)
			fact := p.GetFact()
			require.Equal(t, knowledgev1.FactState_FACT_STATE_PROPOSED, fact.GetState(), f.ID)
			factOf[f.ID], idOf[fact.GetFactId()], caseOf[f.ID] = fact, f.ID, f
			switch f.Fate {
			case "proposed":
			case "rejected":
				d, err := memory.DecideFact(ctx, &knowledgev1.DecideFactRequest{Command: command(tenant, f.Decider), Scope: scope(tenant, f.Decider), FactId: fact.GetFactId(), ExpectedRevision: "1", Decision: knowledgev1.FactDecision_FACT_DECISION_REJECT})
				require.NoError(t, err, f.ID)
				factOf[f.ID] = d.GetFact()
			default:
				d, err := memory.DecideFact(ctx, &knowledgev1.DecideFactRequest{Command: command(tenant, f.Decider), Scope: scope(tenant, f.Decider), FactId: fact.GetFactId(), ExpectedRevision: "1", Decision: knowledgev1.FactDecision_FACT_DECISION_CONFIRM})
				require.NoError(t, err, f.ID)
				require.Equal(t, knowledgev1.FactState_FACT_STATE_CONFIRMED, d.GetFact().GetState())
				factOf[f.ID] = d.GetFact()
				confirmedAt[f.ID] = time.Now()
			}
		}
	})

	t.Run("adversarial decisions never confirm, revoke or delete anything", func(t *testing.T) {
		a := tenants["a"]
		proposed := factOf["a-alice-proposed"].GetFactId()
		dark := factOf["a-alice-dark"].GetFactId()
		decide := func(tenant, actor, factID, rev string, d knowledgev1.FactDecision) error {
			_, err := memory.DecideFact(ctx, &knowledgev1.DecideFactRequest{Command: command(tenant, actor), Scope: scope(tenant, actor), FactId: factID, ExpectedRevision: rev, Decision: d})
			return err
		}
		confirm := knowledgev1.FactDecision_FACT_DECISION_CONFIRM
		revoke := knowledgev1.FactDecision_FACT_DECISION_REVOKE
		// Identities that speak for a model or a worker. A project fact is
		// visible to the model's identity, so the decision guard answers; a
		// fact about alice is not even visible to it.
		own, err := propose(ctx, &knowledgev1.ProposeFactRequest{Command: command(a, "agent-coder"), Scope: scope(a, "agent-coder"), SubjectType: "project", SubjectId: cases.Project, Content: "This project pins Node 24 " + run, Origin: knowledgev1.FactOrigin_FACT_ORIGIN_MODEL})
		require.NoError(t, err)
		attempt("model self-confirms its proposal", decide(a, "agent-coder", own.GetFact().GetFactId(), "1", confirm))
		attempt("model identity confirms a proposal about the user", decide(a, "agent-coder", proposed, "1", confirm))
		attempt("worker identity revokes a confirmed fact", decide(a, "worker-memory", dark, "2", revoke))
		_, err = memory.DeleteFact(ctx, &knowledgev1.DeleteFactRequest{Command: command(a, "agent-coder"), Scope: scope(a, "agent-coder"), FactId: dark, ExpectedRevision: "2"})
		attempt("model identity deletes a confirmed fact", err)
		// Other principals.
		attempt("another tenant's actor with the subject's name confirms", decide(tenants["b"], "alice", proposed, "1", confirm))
		attempt("another actor confirms a fact about alice", decide(a, "bob", proposed, "1", confirm))
		attempt("a stale revision revokes", decide(a, "alice", dark, "1", revoke))
		// A second proposal of the same content cannot become a second live fact.
		dup, err := propose(ctx, &knowledgev1.ProposeFactRequest{Command: command(a, "agent-coder"), Scope: scope(a, "agent-coder"), SubjectType: "actor", SubjectId: "alice", Content: caseOf["a-alice-dark"].Content, Origin: knowledgev1.FactOrigin_FACT_ORIGIN_MODEL})
		require.NoError(t, err)
		attempt("a duplicate of a confirmed fact is confirmed", decide(a, "alice", dup.GetFact().GetFactId(), "1", confirm))
		for _, x := range attempts {
			require.False(t, x.Succeeded, x.Name)
		}
		require.Equal(t, codes.PermissionDenied.String(), attempts[0].Code)
		require.Equal(t, codes.NotFound.String(), attempts[1].Code)
		require.Equal(t, codes.NotFound.String(), attempts[4].Code)

		// Racing decisions under one expected revision: exactly one wins.
		wins := 0
		for i := 0; i < 5; i++ {
			p, err := propose(ctx, &knowledgev1.ProposeFactRequest{Command: command(a, "agent-coder"), Scope: scope(a, "agent-coder"), SubjectType: "actor", SubjectId: "alice", Content: fmt.Sprintf("Alice race fact %d of %s", i, run), Origin: knowledgev1.FactOrigin_FACT_ORIGIN_MODEL})
			require.NoError(t, err)
			var wg sync.WaitGroup
			errs := make([]error, 3)
			for j, d := range []knowledgev1.FactDecision{confirm, knowledgev1.FactDecision_FACT_DECISION_REJECT, confirm} {
				wg.Add(1)
				go func(j int, d knowledgev1.FactDecision) {
					defer wg.Done()
					errs[j] = decide(a, "alice", p.GetFact().GetFactId(), "1", d)
				}(j, d)
			}
			wg.Wait()
			n := 0
			for _, e := range errs {
				if e == nil {
					n++
				} else {
					require.Equal(t, codes.FailedPrecondition, status.Code(e))
				}
			}
			require.Equal(t, 1, n)
			wins += n
			attempts = append(attempts, memAttempt{Name: fmt.Sprintf("race %d: three decisions on revision 1", i), Code: fmt.Sprintf("%d winner", n), Succeeded: n != 1})
		}
		require.Equal(t, 5, wins)
		// Authority check on the ledger: every confirmation is a user's, never a model or worker identity's.
		bad := l.query(migrator, `SELECT count(*)::int AS n FROM memory_decisions d JOIN memory_facts f ON f.fact_id = d.fact_id
			WHERE f.tenant_id = ANY($1) AND d.decision <> 'propose' AND (d.authority NOT IN ('user', 'policy')
			  OR EXISTS (SELECT 1 FROM memory_facts m WHERE m.tenant_id = f.tenant_id AND m.proposer = d.decider AND m.origin IN ('model', 'worker')))`,
			[]string{tenants["a"], tenants["b"]})
		require.EqualValues(t, 0, bad[0]["n"])
	})

	t.Run("revocation, deletion and the reviewed expiry rule take effect", func(t *testing.T) {
		a := tenants["a"]
		_, err := memory.DecideFact(ctx, &knowledgev1.DecideFactRequest{Command: command(a, "alice"), Scope: scope(a, "alice"), FactId: factOf["a-alice-revoked"].GetFactId(), ExpectedRevision: "2", Decision: knowledgev1.FactDecision_FACT_DECISION_REVOKE})
		require.NoError(t, err)
		deleted := factOf["a-alice-deleted"].GetFactId()
		d, err := memory.DeleteFact(ctx, &knowledgev1.DeleteFactRequest{Command: command(a, "alice"), Scope: scope(a, "alice"), FactId: deleted, ExpectedRevision: "2"})
		require.NoError(t, err)
		require.True(t, d.GetFact().GetDeleted())
		require.Empty(t, d.GetFact().GetContent())
		_, err = memory.GetFact(ctx, &knowledgev1.GetFactRequest{Scope: scope(a, "alice"), FactId: deleted})
		require.Equal(t, codes.NotFound, status.Code(err), "readability ends in the deletion transaction")
		// Reconfirmation of a revoked fact is refused.
		_, err = memory.DecideFact(ctx, &knowledgev1.DecideFactRequest{Command: command(a, "alice"), Scope: scope(a, "alice"), FactId: factOf["a-alice-revoked"].GetFactId(), ExpectedRevision: "3", Decision: knowledgev1.FactDecision_FACT_DECISION_CONFIRM})
		attempt("a revoked fact is confirmed again", err)
		require.Equal(t, codes.FailedPrecondition, status.Code(err))
		expired := factOf["a-alice-expired"].GetFactId()
		l.eventually(3*time.Minute, "the reviewed expiry rule", func() bool {
			rows := l.query(migrator, "SELECT state FROM memory_facts WHERE fact_id = $1", expired)
			return rows[0]["state"] == "expired"
		})
		pol := l.query(migrator, "SELECT authority, decider, policy_revision FROM memory_decisions WHERE fact_id = $1 AND decision = 'expire'", expired)
		require.Len(t, pol, 1)
		require.Equal(t, "policy", pol[0]["authority"])
	})

	ids := []string{}
	for _, f := range factOf {
		ids = append(ids, f.GetFactId())
	}
	var projectionLag, purgeSeconds []float64
	// P3.4 (EVAL-04): the report's memory metrics are counted from these
	// checks, never literals: a duplicate point of a live fact, and a point
	// or Store content left for a removed fact (a stale revival).
	duplicateProjections, staleRevivals := 0, 0
	t.Run("every projection settles through the lane: content only for live facts, one point each, tombstones elsewhere", func(t *testing.T) {
		l.eventually(15*time.Minute, "every projection accepted at the fact's current revision", func() bool {
			rows := l.query(migrator, `SELECT count(*)::int AS open FROM memory_facts f
				WHERE f.tenant_id = ANY($1) AND (NOT EXISTS (SELECT 1 FROM memory_projections p WHERE p.fact_id = f.fact_id) AND f.state = 'confirmed' AND NOT f.deleted
				   OR EXISTS (SELECT 1 FROM memory_projections p WHERE p.fact_id = f.fact_id AND (p.fact_revision <> f.revision OR p.state <> 'accepted')))`,
				[]string{tenants["a"], tenants["b"]})
			return rows[0]["open"] == int32(0)
		})
		for cid, at := range confirmedAt {
			rows := l.query(migrator, "SELECT max(updated_at) AS t FROM memory_projections WHERE fact_id = $1", factOf[cid].GetFactId())
			if ts, ok := rows[0]["t"].(time.Time); ok {
				projectionLag = append(projectionLag, ts.Sub(at).Seconds())
			}
		}
		purged := l.query(migrator, "SELECT purged_at, deleted_at FROM memory_facts WHERE fact_id = $1", factOf["a-alice-deleted"].GetFactId())
		require.NotNil(t, purged[0]["purged_at"], "deletion purged from every target")
		purgeSeconds = append(purgeSeconds, purged[0]["purged_at"].(time.Time).Sub(purged[0]["deleted_at"].(time.Time)).Seconds())

		facts := l.query(migrator, "SELECT fact_id, state, deleted, revision::int AS rev, content FROM memory_facts WHERE fact_id = ANY($1)", ids)
		gens := l.query(migrator, "SELECT collection_name, state FROM index_generations")
		for _, f := range facts {
			id := f["fact_id"].(string)
			live := f["state"] == "confirmed" && f["deleted"] == false
			stored := l.query(storeDSN, `SELECT value::text AS v FROM "memory_store".store WHERE key = $1`, id)
			for _, g := range gens {
				n := qdrantCount(t, g["collection_name"].(string), "fact_id", id)
				if live && g["state"] != "retired" {
					duplicateProjections += max(0, n-1)
					require.Equal(t, 1, n, "one point per live fact in %s", g["collection_name"])
				} else {
					if !live {
						staleRevivals += n
					}
					require.Zero(t, n, "no point for a removed fact (%s, %s)", idOf[id], f["state"])
				}
			}
			if f["state"] == "proposed" || f["state"] == "rejected" {
				require.Empty(t, stored, "an unconfirmed fact is never projected")
				continue
			}
			require.Len(t, stored, 1, idOf[id])
			var v map[string]any
			require.NoError(t, json.Unmarshal([]byte(stored[0]["v"].(string)), &v))
			require.EqualValues(t, f["rev"], v["revision"])
			if live {
				require.Equal(t, f["content"], v["content"])
			} else {
				if _, revived := v["content"]; revived {
					staleRevivals++
				}
				require.Equal(t, true, v["tombstone"], idOf[id])
				require.NotContains(t, v, "content", "no content left for a removed fact")
			}
		}
	})

	var reports []memCaseReport
	var latencies []float64
	disclosures, contaminations := 0, 0
	t.Run("the recall cases are scored over repeated trials under the current authority", func(t *testing.T) {
		for _, c := range cases.Recalls {
			tenant := tenants[c.Tenant]
			rep := memCaseReport{ID: c.ID, Stratum: c.Stratum}
			forbidden := map[string]bool{}
			for _, x := range c.Forbidden {
				forbidden[factOf[x].GetFactId()] = true
			}
			for trial := 1; trial <= cases.Trials; trial++ {
				begun := time.Now()
				r, err := memory.RecallFacts(ctx, &knowledgev1.RecallFactsRequest{Scope: scope(tenant, c.As), Query: c.Query, MaxFacts: uint32(cases.K),
					RetrievalProfileId: "hybrid-bge-m3-dev-v1", Deadline: timestamppb.New(time.Now().Add(60 * time.Second))})
				require.NoError(t, err, c.ID)
				took := time.Since(begun)
				latencies = append(latencies, float64(took.Milliseconds()))
				tr := memTrial{Trial: trial, LatencyMs: float64(took.Milliseconds()), NoAnswer: r.GetNoAnswer(), Failures: []string{}}
				for _, f := range r.GetFacts() {
					tr.Returned = append(tr.Returned, idOf[f.GetFactId()])
					cf, known := caseOf[idOf[f.GetFactId()]]
					visible := known && tenants[cf.Tenant] == tenant && (cf.SubjectType != "actor" || cf.Subject == c.As)
					if !known || tenants[cf.Tenant] != tenant {
						contaminations++
					}
					if forbidden[f.GetFactId()] || !visible || f.GetState() != knowledgev1.FactState_FACT_STATE_CONFIRMED {
						tr.Disclosures++
						tr.Failures = append(tr.Failures, "disclosure")
					}
				}
				disclosures += tr.Disclosures
				if len(c.Expected) > 0 {
					hit := 0
					for _, e := range c.Expected {
						for _, x := range tr.Returned {
							if x == e {
								hit++
							}
						}
					}
					rate := float64(hit) / float64(len(c.Expected))
					tr.HitRate = &rate
					if hit < len(c.Expected) {
						tr.Failures = append(tr.Failures, "missed_fact")
					}
				} else if !r.GetNoAnswer() {
					tr.Failures = append(tr.Failures, "answer_without_expected_fact")
				}
				rep.Trials = append(rep.Trials, tr)
			}
			reports = append(reports, rep)
		}
		require.Zero(t, contaminations, "tenant contamination")
		require.Zero(t, disclosures, "unauthorized disclosure")
	})

	t.Run("a lost Store is rebuilt from the confirmed current facts without reviving any removal", func(t *testing.T) {
		l.sql(storeDSN, `DELETE FROM "memory_store".store WHERE key = ANY($1)`, ids)
		cmd := exec.Command("node", "dist/memoryctl.js", "rebuild", "--target", "0")
		cmd.Dir = filepath.Join(root, "services", "agent", "knowledge")
		cmd.Env = append(os.Environ(), "ANVILKIT_KNOWLEDGE_CONFIG="+l.knowledgeCfg, "ANVILKIT_KNOWLEDGE_DATABASE_URL="+os.Getenv("ANVILKIT_DEV_KNOWLEDGE_DSN"))
		out, err := cmd.CombinedOutput()
		require.NoError(t, err, string(out))
		l.eventually(10*time.Minute, "the Store rebuilt", func() bool {
			rows := l.query(migrator, `SELECT count(*)::int AS open FROM memory_projections p JOIN memory_facts f ON f.fact_id = p.fact_id
				WHERE f.fact_id = ANY($1) AND p.target = 0 AND (p.state <> 'accepted' OR p.fact_revision <> f.revision)`, ids)
			return rows[0]["open"] == int32(0)
		})
		for cid, f := range factOf {
			stored := l.query(storeDSN, `SELECT value::text AS v FROM "memory_store".store WHERE key = $1`, f.GetFactId())
			fate := caseOf[cid].Fate
			if fate == "proposed" || fate == "rejected" {
				require.Empty(t, stored, cid)
				continue
			}
			require.Len(t, stored, 1, cid)
			if fate == "confirmed" {
				require.Contains(t, stored[0]["v"], caseOf[cid].Content, cid)
			} else {
				if strings.Contains(fmt.Sprint(stored[0]["v"]), caseOf[cid].Content) {
					staleRevivals++
				}
				require.NotContains(t, stored[0]["v"], caseOf[cid].Content, "a rebuild never revives %s", cid)
			}
		}
	})

	// The report: every trial kept, nothing pooled or selected.
	hit, hits := 0.0, 0
	answered, unanswerable, noAnswerRight := 0, 0, 0
	for _, r := range reports {
		for _, tr := range r.Trials {
			if tr.HitRate != nil {
				hit += *tr.HitRate
				hits++
			} else {
				unanswerable++
				if tr.NoAnswer {
					noAnswerRight++
				}
			}
			if !tr.NoAnswer {
				answered++
			}
		}
	}
	falseConfirmations := 0
	for _, a := range attempts {
		if a.Succeeded {
			falseConfirmations++
		}
	}
	report := map[string]any{
		"run": run, "casesDigest": casesDigest, "generation": generation, "collection": collection,
		"profile": "hybrid-bge-m3-dev-v1", "inference": infURL, "trials": cases.Trials, "k": cases.K,
		"attempts": attempts, "falseConfirmations": falseConfirmations, "tenantContamination": contaminations,
		"unauthorizedDisclosures": disclosures, "staleRevivals": staleRevivals, "duplicateProjections": duplicateProjections,
		"expectedHitRate": hit / float64(max(hits, 1)), "noAnswerOnFactFreeTrials": fmt.Sprintf("%d/%d", noAnswerRight, unanswerable),
		"recallLatencyMs":      map[string]float64{"p50": percentile(latencies, 0.5), "p95": percentile(latencies, 0.95), "max": percentile(latencies, 1)},
		"projectionLagSeconds": map[string]float64{"p50": percentile(projectionLag, 0.5), "max": percentile(projectionLag, 1)},
		"deletionPurgeSeconds": purgeSeconds, "cases": reports,
	}
	dir := filepath.Join(root, "outputs", "memory-eval")
	require.NoError(t, os.MkdirAll(dir, 0o755))
	body, err := json.MarshalIndent(report, "", "  ")
	require.NoError(t, err)
	path := filepath.Join(dir, "report-"+run+".json")
	require.NoError(t, os.WriteFile(path, body, 0o644))
	t.Logf("memory evaluation %s (%s): falseConfirmations=%d contamination=%d disclosures=%d hitRate=%.3f noAnswer=%d/%d recall p50=%.0fms p95=%.0fms projection lag p50=%.1fs max=%.1fs purge=%v",
		run, sha(body), falseConfirmations, contaminations, disclosures, hit/float64(max(hits, 1)), noAnswerRight, unanswerable,
		percentile(latencies, 0.5), percentile(latencies, 0.95), percentile(projectionLag, 0.5), percentile(projectionLag, 1), purgeSeconds)
}
