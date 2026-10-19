//go:build integration

package integration

import (
	"bufio"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"math/big"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"
	"github.com/stretchr/testify/require"
	"github.com/testcontainers/testcontainers-go"
)

// laneDataPlane is the lane's data plane as the target platform runs it
// (P0.6): one CA per run issues the server certificate of PostgreSQL, NATS
// and Valkey for every address the host reaches them by. PostgreSQL refuses
// any connection without TLS (pg_hba hostnossl reject) and every DSN
// verifies the server (sslmode=verify-full); NATS serves TLS only and admits
// only the NKey users below, each limited to its own subjects; Valkey serves
// TLS only with the default user off and one ACL user per role limited to its
// owner's keys. The services read the same credential files the charts mount
// from the CSI volume in the cluster. The CA and its keys exist only for the
// run, in the test's own directory.
type laneDataPlane struct {
	dir           string
	ca, cert, key string
	pool          *x509.CertPool
	natsSeeds     map[string]string // NKey user -> seed file
	natsPublic    map[string]string // NKey user -> public key
	queueUsers    map[string]string // ACL user -> password
	natsURL       string
	queueEndpoint string
	queue         testcontainers.Container
}

// The NKey users of the lane's JetStream and what each may do: a forwarder
// publishes its owner's events (with the JetStream acknowledgement inbox), a
// relay drives its own durable pull consumer on its owner's stream, admin
// provisions the streams for the test and observes them.
var natsPermissions = map[string][2][]string{
	"knowledge-forwarder": {{"anvilkit.knowledge.>", "$JS.API.INFO", "$JS.API.STREAM.INFO.ANVILKIT_KNOWLEDGE"}, {"_INBOX.>"}},
	"mcp-forwarder":       {{"anvilkit.mcp.>", "$JS.API.INFO", "$JS.API.STREAM.INFO.ANVILKIT_MCP"}, {"_INBOX.>"}},
	"knowledge-relay":     {relayPublish("ANVILKIT_KNOWLEDGE"), {"_INBOX.>"}},
	"mcp-relay":           {relayPublish("ANVILKIT_MCP"), {"_INBOX.>"}},
	"admin":               {{">"}, {">"}},
}

func relayPublish(stream string) []string {
	return []string{"$JS.API.INFO", "$JS.API.STREAM.INFO." + stream, "$JS.API.CONSUMER.*." + stream, "$JS.API.CONSUMER.*." + stream + ".>",
		"$JS.API.CONSUMER.MSG.NEXT." + stream + ".>", "$JS.API.CONSUMER.DURABLE.CREATE." + stream + ".>", "$JS.ACK." + stream + ".>"}
}

// The Valkey ACL users: the worker consumes every owner's queue, each relay
// writes only its owner's; none may run the dangerous commands (FLUSHALL,
// KEYS, CONFIG, DEBUG, …) beyond INFO, which BullMQ reads for the server
// version. admin is the test's own (FLUSHDB between scenarios, ACL rotation).
var queueACL = map[string]string{
	"worker":          "~anvilkit:* &anvilkit:* +@all -@dangerous +info",
	"relay-knowledge": "~anvilkit:knowledge:* &anvilkit:knowledge:* +@all -@dangerous +info",
	"relay-mcp":       "~anvilkit:mcp:* &anvilkit:mcp:* +@all -@dangerous +info",
	"admin":           "~* &* +@all",
}

func newLaneDataPlane(t *testing.T, ctx context.Context) *laneDataPlane {
	t.Helper()
	d := &laneDataPlane{dir: t.TempDir(), natsSeeds: map[string]string{}, natsPublic: map[string]string{}, queueUsers: map[string]string{}}
	hosts := []string{"localhost", "127.0.0.1", "::1"}
	if p, err := testcontainers.NewDockerProvider(); err == nil {
		if h, err := p.DaemonHost(ctx); err == nil && h != "" {
			hosts = append(hosts, h)
		}
	}
	d.issue(t, hosts)
	for user := range natsPermissions {
		kp, err := nkeys.CreateUser()
		require.NoError(t, err)
		seed, err := kp.Seed()
		require.NoError(t, err)
		pub, err := kp.PublicKey()
		require.NoError(t, err)
		d.natsSeeds[user] = d.write(t, "nats-"+user+".nk", seed, 0o600)
		d.natsPublic[user] = pub
	}
	for user := range queueACL {
		d.queueUsers[user] = randomHex(16)
	}
	d.write(t, "nats.conf", []byte(d.natsConfig()), 0o644)
	d.write(t, "users.acl", []byte(d.aclFile()), 0o644)
	d.write(t, "pg_hba.conf", []byte("local all all trust\nhostssl all all all scram-sha-256\nhostnossl all all all reject\n"), 0o644)
	return d
}

func (d *laneDataPlane) write(t *testing.T, name string, data []byte, mode os.FileMode) string {
	path := filepath.Join(d.dir, name)
	require.NoError(t, os.WriteFile(path, data, mode))
	require.NoError(t, os.Chmod(path, mode))
	return path
}

func (d *laneDataPlane) issue(t *testing.T, hosts []string) {
	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	require.NoError(t, err)
	now := time.Now()
	caTmpl := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "anvilkit-lane-ca"}, NotBefore: now.Add(-time.Hour), NotAfter: now.Add(24 * time.Hour),
		IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageCRLSign}
	caDER, err := x509.CreateCertificate(rand.Reader, caTmpl, caTmpl, &caKey.PublicKey, caKey)
	require.NoError(t, err)
	caCert, err := x509.ParseCertificate(caDER)
	require.NoError(t, err)
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	require.NoError(t, err)
	tmpl := &x509.Certificate{SerialNumber: big.NewInt(2), Subject: pkix.Name{CommonName: "anvilkit-lane-dataplane"}, NotBefore: now.Add(-time.Hour), NotAfter: now.Add(24 * time.Hour),
		KeyUsage: x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	for _, h := range hosts {
		if ip := net.ParseIP(h); ip != nil {
			tmpl.IPAddresses = append(tmpl.IPAddresses, ip)
		} else {
			tmpl.DNSNames = append(tmpl.DNSNames, h)
		}
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, caCert, &key.PublicKey, caKey)
	require.NoError(t, err)
	pkcs8, err := x509.MarshalPKCS8PrivateKey(key)
	require.NoError(t, err)
	d.ca = d.write(t, "ca.crt", pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER}), 0o644)
	d.cert = d.write(t, "tls.crt", pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0o644)
	// Readable by the containers' own users (postgres re-installs it 0600).
	d.key = d.write(t, "tls.key", pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: pkcs8}), 0o644)
	d.pool = x509.NewCertPool()
	d.pool.AddCert(caCert)
}

// files are the TLS material and the server configurations under /tls in a container.
func (d *laneDataPlane) files() []testcontainers.ContainerFile {
	var out []testcontainers.ContainerFile
	for _, name := range []string{"ca.crt", "tls.crt", "tls.key", "nats.conf", "users.acl", "pg_hba.conf"} {
		out = append(out, testcontainers.ContainerFile{HostFilePath: filepath.Join(d.dir, name), ContainerFilePath: "/tls/" + name, FileMode: 0o644})
	}
	return out
}

func (d *laneDataPlane) natsConfig() string {
	users := make([]string, 0, len(natsPermissions))
	for _, user := range sortedKeys(natsPermissions) {
		p := natsPermissions[user]
		users = append(users, fmt.Sprintf("    { nkey: %q, permissions: { publish: { allow: %s }, subscribe: { allow: %s } } }", d.natsPublic[user], quoteList(p[0]), quoteList(p[1])))
	}
	return "port: 4222\njetstream { store_dir: \"/tmp/nats-js\" }\ntls {\n  cert_file: \"/tls/tls.crt\"\n  key_file: \"/tls/tls.key\"\n  timeout: 5\n}\n" +
		"authorization {\n  users: [\n" + strings.Join(users, "\n") + "\n  ]\n}\n"
}

func (d *laneDataPlane) aclFile() string {
	lines := []string{"user default off"}
	for _, user := range sortedKeys(queueACL) {
		lines = append(lines, fmt.Sprintf("user %s on >%s %s", user, d.queueUsers[user], queueACL[user]))
	}
	return strings.Join(lines, "\n") + "\n"
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func quoteList(items []string) string {
	q := make([]string, len(items))
	for i, s := range items {
		q[i] = fmt.Sprintf("%q", s)
	}
	return "[" + strings.Join(q, ", ") + "]"
}

// dsnTLS is the verification suffix of every lane DSN.
func (d *laneDataPlane) dsnTLS() string {
	return "sslmode=verify-full&sslrootcert=" + url.QueryEscape(d.ca)
}

// queueURL is the rediss:// URL of one ACL user.
func (d *laneDataPlane) queueURL(user, password string) string {
	return fmt.Sprintf("rediss://%s:%s@%s", user, password, d.queueEndpoint)
}

func (d *laneDataPlane) natsAdmin(t *testing.T) *nats.Conn {
	t.Helper()
	nc, err := nats.Connect(d.natsURL, nats.RootCAs(d.ca), d.natsSeed(t, "admin"), nats.Timeout(10*time.Second))
	require.NoError(t, err)
	return nc
}

func (d *laneDataPlane) natsSeed(t *testing.T, user string) nats.Option {
	t.Helper()
	opt, err := nats.NkeyOptionFromSeed(d.natsSeeds[user])
	require.NoError(t, err)
	return opt
}

// valkey sends one command over TLS (after AUTH when a user is given) and
// returns the first line of the reply ("+PONG", "-NOAUTH …", "-WRONGPASS …").
func (d *laneDataPlane) valkey(t *testing.T, user, password string, args ...string) string {
	t.Helper()
	conn, err := tls.Dial("tcp", d.queueEndpoint, &tls.Config{RootCAs: d.pool, ServerName: "localhost", MinVersion: tls.VersionTLS12})
	require.NoError(t, err)
	defer conn.Close()
	r := bufio.NewReader(conn)
	send := func(parts ...string) string {
		var b strings.Builder
		fmt.Fprintf(&b, "*%d\r\n", len(parts))
		for _, p := range parts {
			fmt.Fprintf(&b, "$%d\r\n%s\r\n", len(p), p)
		}
		_ = conn.SetDeadline(time.Now().Add(10 * time.Second))
		_, err := conn.Write([]byte(b.String()))
		require.NoError(t, err)
		line, err := r.ReadString('\n')
		require.NoError(t, err)
		return strings.TrimSpace(line)
	}
	if user != "" {
		if reply := send("AUTH", user, password); reply != "+OK" {
			return reply
		}
	}
	return send(args...)
}

// natsEnv is a NATS client's file-based TLS and credential settings under
// one of the services' variable prefixes.
func (d *laneDataPlane) natsEnv(prefix, user string) []string {
	return []string{prefix + "NATS_URL=" + d.natsURL, prefix + "NATS_TLS_MODE=tls", prefix + "NATS_TLS_CA_FILE=" + d.ca, prefix + "NATS_TLS_SERVER_NAME=localhost",
		prefix + "NATS_CREDS_FILE=" + d.natsSeeds[user]}
}

// probeDataPlane (P0.6 AC3): plaintext and anonymous clients are refused by
// every data-plane server, and an authenticated client is held to its own
// scope: a forwarder cannot publish into another owner's subjects, a relay
// cannot write another owner's queue keys.
func (l *lane) probeDataPlane(t *testing.T) {
	d := l.dataPlane
	ctx := context.Background()
	// NATS: plaintext, no credential, and out-of-scope publication.
	_, err := nats.Connect(strings.Replace(d.natsURL, "tls://", "nats://", 1), nats.Timeout(5*time.Second), nats.NoReconnect())
	require.Error(t, err, "a plaintext NATS client is refused")
	_, err = nats.Connect(d.natsURL, nats.RootCAs(d.ca), nats.Timeout(5*time.Second), nats.NoReconnect())
	require.ErrorContains(t, err, "Authorization Violation", "a TLS client without credentials is refused")
	stranger, err := nkeys.CreateUser()
	require.NoError(t, err)
	seed, err := stranger.Seed()
	require.NoError(t, err)
	strangerSeed := d.write(t, "nats-stranger.nk", seed, 0o600)
	opt, err := nats.NkeyOptionFromSeed(strangerSeed)
	require.NoError(t, err)
	_, err = nats.Connect(d.natsURL, nats.RootCAs(d.ca), opt, nats.Timeout(5*time.Second), nats.NoReconnect())
	require.ErrorContains(t, err, "Authorization Violation", "an NKey the server does not list is refused")
	admin := d.natsAdmin(t)
	defer admin.Close()
	js, err := admin.JetStream()
	require.NoError(t, err)
	info, err := js.StreamInfo("ANVILKIT_KNOWLEDGE")
	require.NoError(t, err)
	before := info.State.LastSeq
	violations := make(chan error, 4)
	nc, err := nats.Connect(d.natsURL, nats.RootCAs(d.ca), d.natsSeed(t, "mcp-forwarder"), nats.NoReconnect(),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) { violations <- err }))
	require.NoError(t, err)
	require.NoError(t, nc.Publish("anvilkit.knowledge.probe", []byte("forged")))
	require.NoError(t, nc.Flush())
	select {
	case err := <-violations:
		require.ErrorContains(t, err, "Permissions Violation")
	case <-time.After(10 * time.Second):
		t.Fatal("the MCP forwarder published into the Knowledge subjects without a permissions violation")
	}
	nc.Close()
	info, err = js.StreamInfo("ANVILKIT_KNOWLEDGE")
	require.NoError(t, err)
	require.Equal(t, before, info.State.LastSeq, "nothing reached the Knowledge stream")
	// Valkey: plaintext, the default (anonymous) user, a wrong password and
	// another owner's keys.
	raw, err := net.DialTimeout("tcp", d.queueEndpoint, 5*time.Second)
	require.NoError(t, err)
	_ = raw.SetDeadline(time.Now().Add(5 * time.Second))
	_, _ = raw.Write([]byte("*1\r\n$4\r\nPING\r\n"))
	reply, _ := bufio.NewReader(raw).ReadString('\n')
	raw.Close()
	require.NotContains(t, reply, "PONG", "the TLS port answers no plaintext command")
	require.Contains(t, d.valkey(t, "", "", "PING"), "NOAUTH", "the default user is off")
	require.Contains(t, d.valkey(t, "worker", "wrong-password", "PING"), "WRONGPASS")
	require.Contains(t, d.valkey(t, "relay-knowledge", l.queuePassword["relay-knowledge"], "SET", "anvilkit:mcp:probe", "forged"), "NOPERM")
	require.Contains(t, d.valkey(t, "worker", l.queuePassword["worker"], "FLUSHALL"), "NOPERM", "no dangerous command for a service user")
	require.Equal(t, "+PONG", d.valkey(t, "worker", l.queuePassword["worker"], "PING"))
	// PostgreSQL: a connection without TLS, and one that verifies against
	// another CA, are refused.
	dsn := os.Getenv("ANVILKIT_DEV_KNOWLEDGE_DSN")
	_, err = pgxConnect(ctx, strings.Replace(dsn, d.dsnTLS(), "sslmode=disable", 1))
	require.ErrorContains(t, err, "no encryption", "pg_hba refuses a plaintext connection")
	other := t.TempDir()
	foreign := &laneDataPlane{dir: other}
	foreign.issue(t, []string{"localhost", "127.0.0.1"})
	_, err = pgxConnect(ctx, strings.Replace(dsn, url.QueryEscape(d.ca), url.QueryEscape(foreign.ca), 1))
	require.ErrorContains(t, err, "certificate", "verify-full refuses a server the configured CA did not issue")
}

// rotateQueuePassword (P0.6 AC4, Valkey): the worker's ACL user gains a
// second password, the worker restarts with it (a client rollout, not a
// Valkey restart), the old password is removed and refused, and the lane
// keeps accepting work; the Valkey container never restarts.
func (l *lane) rotateQueuePassword(t *testing.T) {
	d := l.dataPlane
	ctx := context.Background()
	before, err := d.queue.Inspect(ctx)
	require.NoError(t, err)
	old, rotated := l.queuePassword["worker"], randomHex(16)
	admin := l.queuePassword["admin"]
	require.Equal(t, "+OK", d.valkey(t, "admin", admin, "ACL", "SETUSER", "worker", ">"+rotated))
	require.Equal(t, "+PONG", d.valkey(t, "worker", old, "PING"), "both passwords are valid during the rollout")
	require.Equal(t, "+PONG", d.valkey(t, "worker", rotated, "PING"))
	l.procs["worker"].term(30 * time.Second)
	l.queuePassword["worker"] = rotated
	l.startWorker()
	k := l.requestKnowledge("--bytes", "rotated-queue-password")
	l.waitState("knowledge", k.TaskID, "ACCEPTED", 60*time.Second)
	require.Equal(t, "+OK", d.valkey(t, "admin", admin, "ACL", "SETUSER", "worker", "<"+old))
	require.Contains(t, d.valkey(t, "worker", old, "PING"), "WRONGPASS", "the retired password is refused")
	m := l.requestMCP("-bytes", "after-retirement")
	l.waitState("mcp", m.TaskID, "ACCEPTED", 60*time.Second)
	after, err := d.queue.Inspect(ctx)
	require.NoError(t, err)
	require.Equal(t, before.State.StartedAt, after.State.StartedAt, "Valkey was not restarted")
	require.Equal(t, before.RestartCount, after.RestartCount)
}

// pgxConnect opens and closes one connection: only the refusal matters.
func pgxConnect(ctx context.Context, dsn string) (struct{}, error) {
	c, err := pgx.Connect(ctx, dsn)
	if err == nil {
		_ = c.Close(ctx)
	}
	return struct{}{}, err
}
