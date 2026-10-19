//go:build integration

package integration

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"

	controlv1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/control/v1"
	knowledgev1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/knowledge/v1"
	mcpv1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/mcp/v1"
)

// The workload identities of the development PKI (deploy/dev/certs.sh):
// every harness dial presents the certificate of the role it simulates,
// never a test-only all-purpose identity. The negatives are trusted-CA
// certificates the servers must refuse: a foreign trust domain and a
// certificate without a URI SAN.
const (
	principalAPI        = "anvilkit-agent-api"
	principalWorkflow   = "anvilkit-agent-workflow"
	principalModelProxy = "anvilkit-agent-model-proxy"
	principalMCP        = "anvilkit-agent-mcp"
	principalKnowledge  = "anvilkit-agent-knowledge"
	principalWorker     = "anvilkit-agent-background-worker"
	principalInference  = "anvilkit-agent-inference"
	principalSidecar    = "anvilkit-job-access-sidecar"

	// sidecarObserver is the observer Control records for a result the
	// access sidecar submits: its verified identity, never the in-Pod
	// observer's label (P0.2 AC2).
	sidecarObserver   = "spiffe://anvilkit.local/ns/anvilkit-components/sa/" + principalSidecar
	negativeForeignTD = "negative-foreign-trust-domain"
	negativeNoURI     = "negative-no-uri"
)

// certsDir is the synchronized host-side material of the development PKI.
func certsDir(t testing.TB) string {
	t.Helper()
	dir := os.Getenv("ANVILKIT_DEV_CERTS_DIR")
	if dir == "" {
		if tt, ok := t.(*testing.T); ok {
			dir = filepath.Join(repoRoot(tt), ".local", "dev", "certs")
		} else {
			t.Fatal("ANVILKIT_DEV_CERTS_DIR is not set (source .local/dev/env.sh)")
		}
	}
	if _, err := os.Stat(filepath.Join(dir, "ca.crt")); err != nil {
		t.Fatalf("UNEXECUTED: development certificates not found under %s (run deploy/dev/certs.sh): %v", dir, err)
	}
	return dir
}

// identityFiles names a principal's mounted files.
func identityFiles(t testing.TB, principal string) (cert, key, ca string) {
	t.Helper()
	d := filepath.Join(certsDir(t), principal)
	return filepath.Join(d, "tls.crt"), filepath.Join(d, "tls.key"), filepath.Join(d, "ca.crt")
}

// identityEnv is the environment a host-side process needs for its workload
// identity under the given variable prefix (ANVILKIT_<SERVICE>_IDENTITY_*).
func identityEnv(t testing.TB, prefix, principal string) []string {
	t.Helper()
	cert, key, ca := identityFiles(t, principal)
	return []string{prefix + "IDENTITY_CERT_FILE=" + cert, prefix + "IDENTITY_KEY_FILE=" + key, prefix + "IDENTITY_CA_FILE=" + ca}
}

// identityCreds is the mTLS client credential of a principal with standard
// hostname verification against serverName (a DNS SAN of the server's
// certificate); caBundle nil means the development CA.
func identityCreds(t testing.TB, principal, serverName string) credentials.TransportCredentials {
	t.Helper()
	certFile, keyFile, caFile := identityFiles(t, principal)
	cert, err := tls.LoadX509KeyPair(certFile, keyFile)
	require.NoError(t, err)
	caPEM, err := os.ReadFile(caFile)
	require.NoError(t, err)
	pool := x509.NewCertPool()
	require.True(t, pool.AppendCertsFromPEM(caPEM))
	return credentials.NewTLS(&tls.Config{Certificates: []tls.Certificate{cert}, RootCAs: pool, ServerName: serverName, MinVersion: tls.VersionTLS13})
}

// operatorScope is the verified user scope the API carries for an operator
// (P0.3): Control begins a recovery run or records a disposition only for
// a scope of the command's tenant and actor that holds the operator role.
func operatorScope(tenant, actor string, roles ...string) *controlv1.Scope {
	if roles == nil {
		roles = []string{"operator"}
	}
	return &controlv1.Scope{TenantId: tenant, ActorId: actor, Roles: roles}
}

// dialAs connects to a server as the given principal.
func dialAs(t testing.TB, addr, principal, serverName string) *grpc.ClientConn {
	t.Helper()
	conn, err := grpc.NewClient(addr, grpc.WithTransportCredentials(identityCreds(t, principal, serverName)))
	require.NoError(t, err)
	t.Cleanup(func() { conn.Close() })
	return conn
}

func dialControlAs(t testing.TB, addr, principal string) *grpc.ClientConn {
	return dialAs(t, addr, principal, "anvilkit-agent-control")
}

func dialKnowledgeAs(t testing.TB, addr, principal string) *grpc.ClientConn {
	return dialAs(t, addr, principal, "anvilkit-agent-knowledge")
}

func dialMCPAs(t testing.TB, addr, principal string) *grpc.ClientConn {
	return dialAs(t, addr, principal, "anvilkit-agent-mcp")
}

// TestWorkloadIdentityRefusals is the SEC-01 negative evidence of P0.1 on
// the development foundation: for each of the three servers a plaintext
// dial, a certificate of a foreign CA, a trusted certificate with a foreign
// trust domain, a trusted certificate without a URI SAN and a trusted
// workload that the method does not allow are refused as the plan
// specifies, and the allowed workload of the same method is admitted. JWT
// audience validation (P0.3) is a separate case and not claimed here.
func TestWorkloadIdentityRefusals(t *testing.T) {
	e := setup(t)
	l := setupLane(t)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	foreignCA := throwawayCreds(t)

	type probe struct {
		name       string
		addr       string
		serverName string
		call       func(conn *grpc.ClientConn) error
		allowed    string
		denied     string
	}
	probes := []probe{
		{name: "control", addr: e.controlAddr, serverName: "anvilkit-agent-control", allowed: principalAPI, denied: principalInference, call: func(c *grpc.ClientConn) error {
			_, err := controlv1.NewOperationServiceClient(c).GetOperation(ctx, &controlv1.GetOperationRequest{Scope: &controlv1.Scope{TenantId: "tenant_a", ActorId: "u"}, OperationId: "op_none"})
			return err
		}},
		{name: "knowledge", addr: l.knowledgeAddr, serverName: "anvilkit-agent-knowledge", allowed: principalAPI, denied: principalInference, call: func(c *grpc.ClientConn) error {
			_, err := knowledgev1.NewSourceServiceClient(c).GetSource(ctx, &knowledgev1.GetSourceRequest{Scope: &knowledgev1.Scope{TenantId: "tenant_a", ActorId: "u"}, SourceId: "src_none"})
			return err
		}},
		{name: "mcp", addr: l.mcpAddr, serverName: "anvilkit-agent-mcp", allowed: principalAPI, denied: principalInference, call: func(c *grpc.ClientConn) error {
			_, err := mcpv1.NewGrantServiceClient(c).GetGrant(ctx, &mcpv1.GetGrantRequest{Scope: &mcpv1.Scope{TenantId: "tenant_a", ActorId: "u"}, GrantId: "g_none"})
			return err
		}},
	}
	for _, p := range probes {
		t.Run(p.name, func(t *testing.T) {
			plain, err := grpc.NewClient(p.addr, grpc.WithTransportCredentials(insecure.NewCredentials()))
			require.NoError(t, err)
			defer plain.Close()
			require.Equal(t, codes.Unavailable, status.Code(p.call(plain)), "plaintext is refused at the handshake")

			foreign, err := grpc.NewClient(p.addr, grpc.WithTransportCredentials(foreignCA(p.serverName)))
			require.NoError(t, err)
			defer foreign.Close()
			require.Equal(t, codes.Unavailable, status.Code(p.call(foreign)), "a certificate of another CA is refused at the handshake")

			err = p.call(dialAs(t, p.addr, negativeForeignTD, p.serverName))
			require.Equal(t, codes.PermissionDenied, status.Code(err), "wrong trust domain: %v", err)
			err = p.call(dialAs(t, p.addr, negativeNoURI, p.serverName))
			require.Equal(t, codes.Unauthenticated, status.Code(err), "no URI SAN: %v", err)
			err = p.call(dialAs(t, p.addr, p.denied, p.serverName))
			require.Equal(t, codes.PermissionDenied, status.Code(err), "wrong workload (%s): %v", p.denied, err)
			err = p.call(dialAs(t, p.addr, p.allowed, p.serverName))
			require.Equal(t, codes.NotFound, status.Code(err), "the allowed workload reaches the handler (%s): %v", p.allowed, err)
		})
	}
}

func execCommand(name string, args ...string) ([]byte, error) {
	return exec.Command(name, args...).CombinedOutput()
}

// throwawayCreds mints a CA the servers do not trust and returns a client
// credential factory presenting a leaf of that CA with the API's identity.
func throwawayCreds(t testing.TB) func(serverName string) credentials.TransportCredentials {
	t.Helper()
	dir := t.TempDir()
	run := func(args ...string) {
		out, err := execCommand("openssl", args...)
		require.NoError(t, err, string(out))
	}
	run("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", filepath.Join(dir, "ca.key"), "-out", filepath.Join(dir, "ca.crt"), "-days", "1", "-subj", "/CN=untrusted", "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign")
	run("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", filepath.Join(dir, "tls.key"), "-out", filepath.Join(dir, "req.csr"), "-subj", "/CN=anvilkit-agent-api")
	require.NoError(t, os.WriteFile(filepath.Join(dir, "ext.cnf"), []byte("subjectAltName=URI:spiffe://anvilkit.local/ns/anvilkit-apps/sa/anvilkit-agent-api\nextendedKeyUsage=clientAuth\n"), 0o600))
	run("x509", "-req", "-in", filepath.Join(dir, "req.csr"), "-CA", filepath.Join(dir, "ca.crt"), "-CAkey", filepath.Join(dir, "ca.key"), "-CAcreateserial", "-out", filepath.Join(dir, "tls.crt"), "-days", "1", "-extfile", filepath.Join(dir, "ext.cnf"))
	cert, err := tls.LoadX509KeyPair(filepath.Join(dir, "tls.crt"), filepath.Join(dir, "tls.key"))
	require.NoError(t, err)
	caPEM, err := os.ReadFile(filepath.Join(certsDir(t), "ca.crt"))
	require.NoError(t, err)
	pool := x509.NewCertPool()
	require.True(t, pool.AppendCertsFromPEM(caPEM))
	return func(serverName string) credentials.TransportCredentials {
		// The client trusts the real server; only its own certificate is foreign.
		return credentials.NewTLS(&tls.Config{Certificates: []tls.Certificate{cert}, RootCAs: pool, ServerName: serverName, MinVersion: tls.VersionTLS13})
	}
}
