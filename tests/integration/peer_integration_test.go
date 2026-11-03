//go:build integration

package integration

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net"
	"net/http"
	"os"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// The access sidecar's second peer binding (DEC-045 B2, DD-03 §5;
// outputs/dec-045/RECORD.md §9), as the host-run scenarios exercise it: the
// sidecar listens on two abstract-namespace names, the launcher's secret
// reaches the sidecar and the trusted process by environment, every request
// carries the role's token and a fresh nonce, every answer carries the
// sidecar's proof. The derivations here are the exact byte layouts of the
// sidecar's internal/peer package.
const (
	peerHeaderToken = "Anvilkit-Peer-Token"
	peerHeaderNonce = "Anvilkit-Peer-Nonce"
	peerHeaderProof = "Anvilkit-Sidecar-Proof"
)

// peerSocket is one of the sidecar's sockets as a client reaches it: the
// abstract name (leading @) and the token of the role the socket serves.
type peerSocket struct {
	Name  string
	Token string
}

// abstractNames gives a launch's two socket names, unique per test process
// and layout so that parallel scenarios never share a sidecar.
func abstractNames(name string) (trusted, candidate string) {
	base := fmt.Sprintf("@anvilkit-it/%d-%s", os.Getpid(), name)
	return base + "/trusted", base + "/candidate"
}

// newLaunchSecret draws a launch secret the way the Workflow launcher does:
// 32 random bytes as 64 lowercase hex characters.
func newLaunchSecret(t *testing.T) string {
	t.Helper()
	var b [32]byte
	_, err := rand.Read(b[:])
	require.NoError(t, err)
	return hex.EncodeToString(b[:])
}

// peerToken derives token(role) = hex(HMAC-SHA256(secret, "anvilkit/peer-token/v1/" + role)).
func peerToken(secretHex, role string) string {
	secret, err := hex.DecodeString(secretHex)
	if err != nil {
		panic(err)
	}
	m := hmac.New(sha256.New, secret)
	m.Write([]byte("anvilkit/peer-token/v1/" + role))
	return hex.EncodeToString(m.Sum(nil))
}

// peerProof derives hex(HMAC-SHA256(hexdecode(token), "anvilkit/sidecar-proof/v1/" + nonce)).
func peerProof(token, nonce string) string {
	key, err := hex.DecodeString(token)
	if err != nil {
		panic(err)
	}
	m := hmac.New(sha256.New, key)
	m.Write([]byte("anvilkit/sidecar-proof/v1/" + nonce))
	return hex.EncodeToString(m.Sum(nil))
}

// peerNonce draws a fresh 16-byte nonce as 32 lowercase hex characters.
func peerNonce(t *testing.T) string {
	t.Helper()
	var b [16]byte
	_, err := rand.Read(b[:])
	require.NoError(t, err)
	return hex.EncodeToString(b[:])
}

// bindPeer sets the token and a fresh nonce on a request and returns the nonce.
func bindPeer(t *testing.T, req *http.Request, s peerSocket) string {
	t.Helper()
	nonce := peerNonce(t)
	req.Header.Set(peerHeaderToken, s.Token)
	req.Header.Set(peerHeaderNonce, nonce)
	return nonce
}

// requirePeerProof fails the test unless the answer carries the sidecar's
// proof for the nonce (every answer of a request that passed the guard).
func requirePeerProof(t *testing.T, resp *http.Response, s peerSocket, nonce string) {
	t.Helper()
	require.Equal(t, peerProof(s.Token, nonce), resp.Header.Get(peerHeaderProof), "the sidecar's proof on %s %s", resp.Request.Method, resp.Request.URL.Path)
}

// sidecarUp reports whether the sidecar accepts connections on the name.
func sidecarUp(name string) bool {
	c, err := net.DialTimeout("unix", name, time.Second)
	if err != nil {
		return false
	}
	_ = c.Close()
	return true
}
