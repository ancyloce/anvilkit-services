//go:build integration

package integration

import (
	"context"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// This gate targets the actual digest-pinned Helm services and their exclusively
// owned data namespace. It deliberately starts no host-side substitute services.
func TestDeployedRepairChain(t *testing.T) {
	if os.Getenv("ANVILKIT_REPAIR_KUBERNETES") != "1" {
		skipMissing(t, "ANVILKIT_REPAIR_KUBERNETES", "an explicitly owned repair environment is required")
	}
	e := &env{apiURL: os.Getenv("ANVILKIT_INTEGRATION_API_URL"), controlDSN: os.Getenv("ANVILKIT_DEV_CONTROL_DSN"), kubeconfig: os.Getenv("KUBECONFIG"), t: t}
	require.NotEmpty(t, e.apiURL)
	require.NotEmpty(t, e.controlDSN)
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Minute)
	defer cancel()
	cs := e.k8s()
	const ns = "anvilkit-verification"
	const sandbox = "anvilkit-verification-sandbox"
	for _, namespace := range []string{ns, sandbox} {
		n, err := cs.CoreV1().Namespaces().Get(ctx, namespace, metav1.GetOptions{})
		require.NoError(t, err)
		require.Equal(t, "repairs-f01-f12", n.Labels["anvilkit.io/owner"])
	}
	db := e.db()
	await := func(id, want string) map[string]any {
		var view map[string]any
		require.Eventually(e.t, func() bool {
			code, out := e.call("GET", "/api/v1/operations/"+id, tokenA, "")
			require.Equal(t, 200, code)
			view = out
			return out["lifecycle"] == want
		}, 2*time.Minute, 300*time.Millisecond, "operation %s must become %s", id, want)
		return view
	}
	for _, scenario := range []string{"success", "cancel", "workflow_restart"} {
		t.Run(scenario, func(t *testing.T) {
			e.t = t
			code, created := e.call("POST", "/api/v1/operations", tokenA, createBody(fmt.Sprintf("repair-%s-%d", scenario, time.Now().UnixNano())))
			require.Equal(t, 202, code, "%v", created)
			id := created["operationId"].(string)
			if scenario != "success" {
				require.Eventually(t, func() bool {
					jobs, err := cs.BatchV1().Jobs(sandbox).List(ctx, metav1.ListOptions{LabelSelector: "anvilkit.io/launch-key=" + launchKey(id)})
					return err == nil && len(jobs.Items) == 1
				}, 45*time.Second, 200*time.Millisecond)
			}
			want := "succeeded"
			if scenario == "cancel" {
				want = "canceled"
				code, view := e.call("GET", "/api/v1/operations/"+id, tokenA, "")
				require.Equal(t, 200, code)
				code, receipt := e.call("POST", "/api/v1/operations/"+id+"/commands", tokenA, fmt.Sprintf(`{"commandId":"cancel-%s","kind":"cancel","expectedRevision":"%s"}`, id, view["revision"]))
				require.Equal(t, 202, code, "%v", receipt)
			}
			if scenario == "workflow_restart" {
				pods, err := cs.CoreV1().Pods(ns).List(ctx, metav1.ListOptions{LabelSelector: "app.kubernetes.io/name=anvilkit-agent-workflow"})
				require.NoError(t, err)
				require.Len(t, pods.Items, 1)
				require.Equal(t, "repairs-f01-f12", pods.Items[0].Labels["anvilkit.io/owner"])
				uid := pods.Items[0].UID
				require.NoError(t, cs.CoreV1().Pods(ns).Delete(ctx, pods.Items[0].Name, metav1.DeleteOptions{Preconditions: &metav1.Preconditions{UID: &uid}}))
			}
			view := await(id, want)
			require.Equal(t, "complete", view["cleanup"])
			_, err := cs.BatchV1().Jobs(sandbox).Get(ctx, launchKey(id), metav1.GetOptions{})
			require.True(t, apierrors.IsNotFound(err), "terminal cleanup must remove original Job: %v", err)
			var active int
			require.NoError(t, db.QueryRow(ctx, "SELECT count(*) FROM permits WHERE owner_kind='operation' AND owner_id=$1 AND state='active'", id).Scan(&active))
			require.Zero(t, active)
			if scenario != "cancel" {
				e.assertSingleAcceptedResult(id)
			}
		})
	}
	e.t = t
	for _, service := range []string{"api", "control", "workflow", "model-proxy", "knowledge", "mcp", "background-worker"} {
		name := "anvilkit-agent-" + service
		dep, err := cs.AppsV1().Deployments(ns).Get(ctx, name, metav1.GetOptions{})
		require.NoError(t, err)
		require.EqualValues(t, 1, *dep.Spec.Replicas)
		require.Equal(t, "Recreate", string(dep.Spec.Strategy.Type))
		require.EqualValues(t, 1, dep.Status.ReadyReplicas)
		pods, err := cs.CoreV1().Pods(ns).List(ctx, metav1.ListOptions{LabelSelector: "app.kubernetes.io/name=" + name})
		require.NoError(t, err)
		businessPods := pods.Items[:0]
		for _, pod := range pods.Items {
			job := false
			for _, owner := range pod.OwnerReferences {
				if owner.Kind == "Job" {
					job = true
				}
			}
			if job {
				t.Logf("%s: temporary Job Pod counted separately: %s", name, pod.Name)
			} else {
				businessPods = append(businessPods, pod)
			}
		}
		pods.Items = businessPods
		require.Len(t, pods.Items, 1)
		require.Len(t, pods.Items[0].Spec.Containers, 1)
		require.Empty(t, pods.Items[0].Spec.InitContainers)
		require.Empty(t, pods.Items[0].Spec.EphemeralContainers)
		require.Contains(t, pods.Items[0].Spec.Containers[0].Image, "@sha256:")
		t.Logf("%s: one Ready Pod, one container, one replica, Recreate, digest pinned", name)
	}
	hpa, err := cs.AutoscalingV2().HorizontalPodAutoscalers(ns).List(ctx, metav1.ListOptions{})
	require.NoError(t, err)
	require.Empty(t, hpa.Items)
}
