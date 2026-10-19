//go:build integration

package integration

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	controlv1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/control/v1"
)

// selfSigned is a JWT of the development issuer's form that the issuer
// never signed: a fresh RS256 key under the issuer's kid, or no signature.
func selfSigned(t *testing.T, alg string, claims map[string]any) string {
	t.Helper()
	enc := func(v any) string {
		raw, err := json.Marshal(v)
		require.NoError(t, err)
		return base64.RawURLEncoding.EncodeToString(raw)
	}
	head := enc(map[string]any{"alg": alg, "typ": "JWT", "kid": "anvilkit"})
	body := enc(claims)
	if alg == "none" {
		return head + "." + body + "."
	}
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	require.NoError(t, err)
	sum := sha256.Sum256([]byte(head + "." + body))
	sig, err := rsa.SignPKCS1v15(rand.Reader, key, crypto.SHA256, sum[:])
	require.NoError(t, err)
	return head + "." + body + "." + base64.RawURLEncoding.EncodeToString(sig)
}

// callHeaders is e.call that also answers the response headers.
func (e *env) callHeaders(method, path, token, body string) (int, http.Header, map[string]any) {
	req, err := http.NewRequest(method, e.apiURL+path, strings.NewReader(body))
	require.NoError(e.t, err)
	req.Header.Set("Authorization", "Bearer "+token)
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := http.DefaultClient.Do(req)
	require.NoError(e.t, err)
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, resp.Header, out
}

// TestUserAuthorization is the SEC-01 user-side negative evidence of P0.3
// on the development foundation: the API verifies real tokens of the
// development OIDC provider; a token of another audience, of an unmapped
// user, with a forged signature or none, and an opaque fixture token are
// refused with WWW-Authenticate; a tenant or actor stated in a body never
// reaches Control; an unknown action is refused; a user of another project
// of the same tenant neither reads nor commands the operation while the
// operator role crosses projects; only a user holding the operator role
// begins a recovery run. Token expiry, issuer, unknown kid and the
// algorithm allowlist are the verifier's table test (oidcauth_test.go).
func TestUserAuthorization(t *testing.T) {
	e := setup(t)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	run := fmt.Sprint(time.Now().UnixNano())
	other, operator := oidcToken(t, "user-a-other-project"), oidcToken(t, "operator-a")

	t.Run("tokens not issued to the API by the issuer are refused", func(t *testing.T) {
		issuer := os.Getenv("ANVILKIT_DEV_OIDC_ISSUER")
		claims := map[string]any{"iss": issuer, "aud": "anvilkit-agent-api", "sub": "user_a", "tenant": "tenant_a", "project": "proj_a", "roles": []string{"operator"},
			"iat": time.Now().Unix(), "exp": time.Now().Add(time.Hour).Unix()}
		for name, token := range map[string]string{
			"another audience":      oidcToken(t, "wrong-audience"),
			"an unmapped user":      oidcToken(t, "nobody-at-all"),
			"a forged signature":    selfSigned(t, "RS256", claims),
			"no signature":          selfSigned(t, "none", claims),
			"an opaque dev token":   "dev-token-tenant-a-0123456789abcdef",
			"an expired forgery":    selfSigned(t, "RS256", map[string]any{"iss": issuer, "aud": "anvilkit-agent-api", "sub": "user_a", "tenant": "tenant_a", "project": "proj_a", "exp": time.Now().Add(-time.Hour).Unix()}),
			"a tenant-less forgery": selfSigned(t, "RS256", map[string]any{"iss": issuer, "aud": "anvilkit-agent-api", "sub": "user_a", "exp": time.Now().Add(time.Hour).Unix()}),
		} {
			code, hdr, out := e.callHeaders("POST", "/api/v1/operations", token, createBody("cmd-"+run+"-forged"))
			require.Equal(t, 401, code, "%s: %v", name, out)
			require.Equal(t, `Bearer realm="anvilkit", error="invalid_token"`, hdr.Get("WWW-Authenticate"), name)
		}
	})

	var opID string
	t.Run("a tenant or actor stated in a request never reaches Control; the token's tenant governs", func(t *testing.T) {
		code, out := e.call("POST", "/api/v1/operations", tokenA, `{"commandId":"cmd-`+run+`-tenant","kind":"local_check","subject":{"profileId":"local-check-v1","subjectDigest":"`+subjectDigest+`"},"tenantId":"tenant_b"}`)
		require.Equal(t, 400, code, "a stated tenant is an unknown member: %v", out)
		code, out = e.call("POST", "/api/v1/operations", tokenA, `{"commandId":"cmd-`+run+`-actor","kind":"local_check","subject":{"profileId":"local-check-v1","subjectDigest":"`+subjectDigest+`"},"actorId":"operator_a"}`)
		require.Equal(t, 400, code, "a stated actor is an unknown member: %v", out)
		code, out = e.call("POST", "/api/v1/operations", tokenA, createBody("cmd-"+run+"-own"))
		require.Equal(t, 202, code, "%v", out)
		opID = out["operationId"].(string)
		require.Equal(t, "proj_a", out["projectId"], "the operation takes the token's project")
		require.Equal(t, "user_a", out["actorId"])
		code, _ = e.call("GET", "/api/v1/operations/"+opID, tokenB, "")
		require.Equal(t, 404, code, "another tenant's user")
	})

	t.Run("an unknown action is refused", func(t *testing.T) {
		code, out := e.call("POST", "/api/v1/operations/"+opID+"/commands", tokenA, `{"commandId":"cmd-`+run+`-delete","kind":"delete","expectedRevision":"1"}`)
		require.Equal(t, 400, code, "%v", out)
		code, _ = e.call("POST", "/api/v1/operations/"+opID+"/escalations", tokenA, `{}`)
		require.Contains(t, []int{404, 405}, code, "an undeclared route")
	})

	t.Run("another project's user neither reads nor commands the operation; the operator role crosses projects", func(t *testing.T) {
		for _, path := range []string{"", "/events", "/source", "/preview", "/release"} {
			code, _ := e.call("GET", "/api/v1/operations/"+opID+path, other, "")
			require.Equal(t, 404, code, "GET %s by another project's user", path)
		}
		code, view := e.call("GET", "/api/v1/operations/"+opID, tokenA, "")
		require.Equal(t, 200, code)
		code, out := e.call("POST", "/api/v1/operations/"+opID+"/commands", other, `{"commandId":"cmd-`+run+`-other-cancel","kind":"cancel","expectedRevision":"`+view["revision"].(string)+`"}`)
		require.Equal(t, 404, code, "cancel by another project's user: %v", out)
		code, view = e.call("GET", "/api/v1/operations/"+opID, operator, "")
		require.Equal(t, 200, code, "an operator reads every project of the tenant: %v", view)
	})

	t.Run("only a user holding the operator role begins a recovery run", func(t *testing.T) {
		rec := controlv1.NewRecoveryServiceClient(e.dialControl(e.controlAddr, principalAPI))
		// A run-unique tenant: the run fences nothing another scenario uses.
		tenant := "tenant_authz_" + run[len(run)-8:]
		begin := func(scope *controlv1.Scope, id string) error {
			_, err := rec.BeginRecovery(ctx, &controlv1.BeginRecoveryRequest{
				Command: &controlv1.CommandIdentity{TenantId: tenant, CommandId: id, ActorId: scope.GetActorId(), RequestDigest: digestOf(id)},
				Scope:   scope, ScopeKey: tenant, WindowStart: timestamppb.New(time.Now().Add(-time.Hour)), WindowEnd: timestamppb.New(time.Now().Add(time.Minute)),
				ClockUncertainty: durationpb.New(5 * time.Second), Reason: "P0.3 operator role",
			})
			return err
		}
		require.Equal(t, codes.PermissionDenied, status.Code(begin(operatorScope(tenant, "user_a", "author"), "rec-"+run+"-author")), "an author is no operator")
		_, err := rec.BeginRecovery(ctx, &controlv1.BeginRecoveryRequest{
			Command: &controlv1.CommandIdentity{TenantId: tenant, CommandId: "rec-" + run + "-foreign", ActorId: "operator_a", RequestDigest: digestOf("foreign")},
			Scope:   operatorScope("tenant_a", "operator_a"), ScopeKey: tenant, WindowStart: timestamppb.New(time.Now().Add(-time.Hour)), WindowEnd: timestamppb.New(time.Now().Add(time.Minute)),
		})
		require.Equal(t, codes.PermissionDenied, status.Code(err), "an operator of another tenant")
		require.NoError(t, begin(operatorScope(tenant, "operator_a"), "rec-"+run+"-operator"))
		// Roles from any workload other than the API are not the user's.
		workflowRec := controlv1.NewRecoveryServiceClient(e.dialControl(e.controlAddr, principalWorkflow))
		_, err = workflowRec.BeginRecovery(ctx, &controlv1.BeginRecoveryRequest{Command: &controlv1.CommandIdentity{TenantId: tenant, CommandId: "x", ActorId: "operator_a", RequestDigest: digestOf("x")},
			Scope: operatorScope(tenant, "operator_a"), ScopeKey: tenant, WindowStart: timestamppb.Now(), WindowEnd: timestamppb.Now()})
		require.Equal(t, codes.PermissionDenied, status.Code(err), "the Workflow may not call BeginRecovery at all")
	})
}
