//go:build integration

package integration

import (
	"bufio"
	"context"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"
	"github.com/stretchr/testify/require"
)

// qualificationDataPlane is the description deploy/qualification/check-dataplane.py
// writes for one probe run: the qualification root's public certificate, the
// local ends of its port-forwards to the data-plane Services with the name each
// server certificate carries, and the files holding the credentials the probe
// presents (never the credentials themselves).
type qualificationDataPlane struct {
	CA   string `json:"ca"`
	NATS struct {
		Address    string            `json:"address"`
		ServerName string            `json:"serverName"`
		Seeds      map[string]string `json:"seeds"`
	} `json:"nats"`
	Valkey []struct {
		Name         string `json:"name"`
		Address      string `json:"address"`
		ServerName   string `json:"serverName"`
		PasswordFile string `json:"passwordFile"`
	} `json:"valkey"`
	Postgres struct {
		Address    string `json:"address"`
		ServerName string `json:"serverName"`
		Database   string `json:"database"`
		Role       string `json:"role"`
	} `json:"postgres"`
}

// TestQualificationDataPlane (P0.6 AC3 in the qualification environment): a
// client presenting no NATS credential cannot publish to anvilkit.* subjects,
// and a Valkey client without credentials is refused. Run by
// deploy/qualification/check-dataplane.py; skipped without its description.
// Each server also accepts one valid credential (the positive control), so a
// refusal is the server's decision and not a broken tunnel. Nothing here
// writes to a stream or a queue key. The PostgreSQL subtest is supplementary
// (not part of AC3): it records whether pg_hba refuses a connection without
// TLS before authentication, presenting a wrong password only.
func TestQualificationDataPlane(t *testing.T) {
	file := os.Getenv("ANVILKIT_QUALIFICATION_DATAPLANE")
	if file == "" {
		skipMissing(t, "ANVILKIT_QUALIFICATION_DATAPLANE", "not set; run deploy/qualification/check-dataplane.py")
	}
	raw, err := os.ReadFile(file)
	require.NoError(t, err)
	var q qualificationDataPlane
	require.NoError(t, json.Unmarshal(raw, &q))
	pem, err := os.ReadFile(q.CA)
	require.NoError(t, err)
	pool := x509.NewCertPool()
	require.True(t, pool.AppendCertsFromPEM(pem), "the qualification root certificate parses")
	tlsTo := func(name string) *tls.Config {
		return &tls.Config{RootCAs: pool, ServerName: name, MinVersion: tls.VersionTLS12}
	}

	natsURL := "tls://" + q.NATS.Address
	seed := func(t *testing.T, user string) nats.Option {
		t.Helper()
		opt, err := nats.NkeyOptionFromSeed(q.NATS.Seeds[user])
		require.NoError(t, err)
		return opt
	}
	t.Run("nats/plaintext-refused", func(t *testing.T) {
		// A raw client: nats.go would upgrade a nats:// URL to TLS on its own.
		conn, err := net.DialTimeout("tcp", q.NATS.Address, 5*time.Second)
		require.NoError(t, err)
		defer conn.Close()
		_ = conn.SetDeadline(time.Now().Add(10 * time.Second))
		r := bufio.NewReader(conn)
		info, err := r.ReadString('\n')
		require.NoError(t, err)
		require.Contains(t, info, `"tls_required":true`, "the server announces TLS as required")
		_, _ = conn.Write([]byte("CONNECT {\"verbose\":false,\"pedantic\":false}\r\nPUB anvilkit.knowledge.probe 6\r\nforged\r\nPING\r\n"))
		// The server expects a TLS handshake after INFO: it closes the connection
		// (perhaps after a TLS alert) instead of answering the PING.
		var got strings.Builder
		buf := make([]byte, 512)
		var end error
		for end == nil {
			var n int
			n, end = r.Read(buf)
			got.Write(buf[:n])
		}
		t.Logf("plaintext reply: %q, then %v", got.String(), end)
		require.NotContains(t, got.String(), "PONG", "no plaintext session is established")
		require.NotContains(t, got.String(), "+OK", "no plaintext session is established")
		var timeout net.Error
		require.False(t, errors.As(end, &timeout) && timeout.Timeout(), "the server closes the plaintext connection")
	})
	t.Run("nats/anonymous-refused", func(t *testing.T) {
		_, err := nats.Connect(natsURL, nats.Secure(tlsTo(q.NATS.ServerName)), nats.Timeout(10*time.Second), nats.NoReconnect())
		require.ErrorContains(t, err, "Authorization Violation", "a TLS client without credentials is refused")
	})
	t.Run("nats/unlisted-nkey-refused", func(t *testing.T) {
		stranger, err := nkeys.CreateUser()
		require.NoError(t, err)
		pub, err := stranger.PublicKey()
		require.NoError(t, err)
		_, err = nats.Connect(natsURL, nats.Secure(tlsTo(q.NATS.ServerName)), nats.Timeout(10*time.Second), nats.NoReconnect(),
			nats.Nkey(pub, stranger.Sign))
		require.ErrorContains(t, err, "Authorization Violation", "an NKey the server does not list is refused")
	})
	t.Run("nats/out-of-scope-publish-refused", func(t *testing.T) {
		admin, err := nats.Connect(natsURL, nats.Secure(tlsTo(q.NATS.ServerName)), seed(t, "streams"), nats.Timeout(10*time.Second), nats.NoReconnect())
		require.NoError(t, err)
		defer admin.Close()
		js, err := admin.JetStream()
		require.NoError(t, err)
		probe := &nats.StreamInfoRequest{SubjectsFilter: "anvilkit.knowledge.probe"}
		before, err := js.StreamInfo("ANVILKIT_KNOWLEDGE", probe)
		require.NoError(t, err)
		violations := make(chan error, 4)
		nc, err := nats.Connect(natsURL, nats.Secure(tlsTo(q.NATS.ServerName)), seed(t, "mcp-forwarder"), nats.Timeout(10*time.Second), nats.NoReconnect(),
			nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) { violations <- err }))
		require.NoError(t, err, "the MCP forwarder's own credential is accepted (positive control)")
		defer nc.Close()
		own, err := nc.JetStream()
		require.NoError(t, err)
		_, err = own.StreamInfo("ANVILKIT_MCP")
		require.NoError(t, err, "the MCP forwarder reaches its own stream's API (positive control)")
		require.NoError(t, nc.Publish("anvilkit.knowledge.probe", []byte("forged")))
		require.NoError(t, nc.Flush())
		select {
		case err := <-violations:
			t.Logf("violation: %v", err)
			require.ErrorContains(t, err, "Permissions Violation")
		case <-time.After(10 * time.Second):
			t.Fatal("the MCP forwarder published into the Knowledge subjects without a permissions violation")
		}
		after, err := js.StreamInfo("ANVILKIT_KNOWLEDGE", probe)
		require.NoError(t, err)
		require.Zero(t, after.State.Subjects["anvilkit.knowledge.probe"], "nothing reached the Knowledge stream on the probe subject")
		t.Logf("ANVILKIT_KNOWLEDGE last sequence %d before, %d after", before.State.LastSeq, after.State.LastSeq)
	})

	for _, v := range q.Valkey {
		password, err := os.ReadFile(v.PasswordFile)
		require.NoError(t, err)
		secret := strings.TrimSpace(string(password))
		t.Run("valkey/"+v.Name+"/no-credential-refused", func(t *testing.T) {
			require.Contains(t, valkeyCommand(t, v.Address, tlsTo(v.ServerName), "", "PING"), "NOAUTH", "a client without credentials is refused")
		})
		t.Run("valkey/"+v.Name+"/wrong-password-refused", func(t *testing.T) {
			b := make([]byte, 24)
			_, _ = rand.Read(b)
			require.Contains(t, valkeyCommand(t, v.Address, tlsTo(v.ServerName), hex.EncodeToString(b), "PING"), "WRONGPASS")
		})
		t.Run("valkey/"+v.Name+"/credential-accepted", func(t *testing.T) {
			require.Equal(t, "+PONG", valkeyCommand(t, v.Address, tlsTo(v.ServerName), secret, "PING"), "the deployed credential is accepted (positive control)")
		})
		if v.Name == "server" {
			// Read-only: the ACL users the server holds (the per-owner ACL users are a
			// recorded gap of this combination; nothing here writes or flushes).
			t.Run("valkey/acl-users", func(t *testing.T) {
				reply := valkeyArray(t, v.Address, tlsTo(v.ServerName), secret, "ACL", "USERS")
				t.Logf("ACL users: %s", strings.Join(reply, ","))
			})
		}
		// Last: a server that resets the plaintext connection can end the port-forward.
		t.Run("valkey/"+v.Name+"/plaintext-refused", func(t *testing.T) {
			conn, err := net.DialTimeout("tcp", v.Address, 5*time.Second)
			require.NoError(t, err)
			defer conn.Close()
			_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
			_, _ = conn.Write([]byte("*1\r\n$4\r\nPING\r\n"))
			reply, _ := bufio.NewReader(conn).ReadString('\n')
			require.NotContains(t, reply, "PONG", "the TLS port answers no plaintext command")
			require.NotContains(t, reply, "NOAUTH", "the TLS port does not parse plaintext commands")
		})
	}

	if q.Postgres.Address != "" {
		t.Run("postgres/plaintext-refused-before-authentication", func(t *testing.T) {
			b := make([]byte, 24)
			_, _ = rand.Read(b)
			host, port, err := net.SplitHostPort(q.Postgres.Address)
			require.NoError(t, err)
			dsn := fmt.Sprintf("host=%s port=%s user=%s dbname=%s password=%s sslmode=disable connect_timeout=10", host, port, q.Postgres.Role, q.Postgres.Database, hex.EncodeToString(b))
			ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
			defer cancel()
			conn, err := pgx.Connect(ctx, dsn)
			if conn != nil {
				_ = conn.Close(ctx)
			}
			require.Error(t, err)
			t.Logf("plaintext connection error: %v", err)
			require.ErrorContains(t, err, "no encryption", "pg_hba refuses a connection without TLS before any authentication")
		})
	}
}

// valkeyConn opens a TLS connection and authenticates as the default user
// when a password is given; it returns the AUTH reply when that is refused.
func valkeyConn(t *testing.T, address string, cfg *tls.Config, password string) (*tls.Conn, *bufio.Reader, string) {
	t.Helper()
	conn, err := tls.DialWithDialer(&net.Dialer{Timeout: 10 * time.Second}, "tcp", address, cfg)
	require.NoError(t, err)
	r := bufio.NewReader(conn)
	if password != "" {
		writeRESP(t, conn, "AUTH", password)
		if line := readLine(t, r); line != "+OK" {
			return conn, r, line
		}
	}
	return conn, r, ""
}

// valkeyCommand sends one command (after AUTH when a password is given) and
// returns the first line of the reply ("+PONG", "-NOAUTH …", "-WRONGPASS …").
func valkeyCommand(t *testing.T, address string, cfg *tls.Config, password string, args ...string) string {
	t.Helper()
	conn, r, refused := valkeyConn(t, address, cfg, password)
	defer conn.Close()
	if refused != "" {
		return refused
	}
	writeRESP(t, conn, args...)
	return readLine(t, r)
}

// valkeyArray sends one command after AUTH and reads an array of bulk strings.
func valkeyArray(t *testing.T, address string, cfg *tls.Config, password string, args ...string) []string {
	t.Helper()
	conn, r, refused := valkeyConn(t, address, cfg, password)
	defer conn.Close()
	require.Empty(t, refused)
	writeRESP(t, conn, args...)
	head := readLine(t, r)
	require.True(t, strings.HasPrefix(head, "*"), "an array reply, got %q", head)
	n, err := strconv.Atoi(head[1:])
	require.NoError(t, err)
	out := make([]string, 0, n)
	for i := 0; i < n; i++ {
		size := readLine(t, r)
		require.True(t, strings.HasPrefix(size, "$"), "a bulk string, got %q", size)
		out = append(out, readLine(t, r))
	}
	return out
}

func writeRESP(t *testing.T, conn net.Conn, parts ...string) {
	t.Helper()
	var b strings.Builder
	fmt.Fprintf(&b, "*%d\r\n", len(parts))
	for _, p := range parts {
		fmt.Fprintf(&b, "$%d\r\n%s\r\n", len(p), p)
	}
	_ = conn.SetDeadline(time.Now().Add(10 * time.Second))
	_, err := conn.Write([]byte(b.String()))
	require.NoError(t, err)
}

func readLine(t *testing.T, r *bufio.Reader) string {
	t.Helper()
	line, err := r.ReadString('\n')
	require.NoError(t, err)
	return strings.TrimSpace(line)
}
