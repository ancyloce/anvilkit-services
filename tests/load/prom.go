package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// clusterQueries are read from the P3.6 metrics stack over the run's window.
// Gauges report p50/p95/max of the series; counters (kind "delta") report the
// increase from the window's first to its last sample.
var clusterQueries = []struct {
	Name, Expr, Kind string
}{
	{"queueOldestWaitingSeconds", `max(anvilkit_background_queue_oldest_waiting_seconds)`, "gauge"},
	{"outboxOldestUnforwardedSeconds", `max(anvilkit_knowledge_outbox_oldest_unforwarded_seconds)`, "gauge"},
	{"relayBacklog", `max(anvilkit_control_relay_backlog)`, "gauge"},
	{"controlGrpcPoolUtilization", `max(anvilkit_control_grpc_pool_in_use / anvilkit_control_grpc_pool_limit)`, "gauge"},
	{"controlDbPoolUtilization", `max(anvilkit_control_db_pool_acquired_connections / anvilkit_control_db_pool_max_connections)`, "gauge"},
	{"unresolvedObligationOldestSeconds", `max(anvilkit_control_unresolved_obligation_oldest_seconds)`, "gauge"},
	{"capacityRefusals", `sum(anvilkit_control_capacity_refusals_total)`, "delta"},
	// The outcomes counter has a series only for label values it has counted, so a
	// scraped Model Proxy that never lost a sender reads 0 through its always-present
	// pending gauge; an unscraped one stays missing.
	{"modelProxySenderLost", `sum(anvilkit_model_proxy_call_outcomes_total{error_code="SENDER_LOST"}) or 0 * sum(anvilkit_model_proxy_pending_observations)`, "delta"},
	{"appPodRestarts", `sum(kube_pod_container_status_restarts_total{namespace="anvilkit-apps"})`, "delta"},
}

// ClusterMetric is one readout; Missing names a query without samples (the
// metric is not scraped there), never a zero.
type ClusterMetric struct {
	Kind    string  `json:"kind"`
	P50     float64 `json:"p50,omitempty"`
	P95     float64 `json:"p95,omitempty"`
	Max     float64 `json:"max,omitempty"`
	Delta   float64 `json:"delta,omitempty"`
	Samples int     `json:"samples"`
	Missing bool    `json:"missing,omitempty"`
}

func readCluster(ctx context.Context, base string, start, end time.Time) (map[string]ClusterMetric, error) {
	out := map[string]ClusterMetric{}
	hc := &http.Client{Timeout: 30 * time.Second}
	for _, q := range clusterQueries {
		values, err := queryRange(ctx, hc, base, q.Expr, start, end)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", q.Name, err)
		}
		m := ClusterMetric{Kind: q.Kind, Samples: len(values)}
		switch {
		case len(values) == 0:
			m.Missing = true
		case q.Kind == "delta":
			// A counter reset (a restarted Pod) shows as a drop; the delta
			// counts the increase segments only.
			for i := 1; i < len(values); i++ {
				if d := values[i] - values[i-1]; d > 0 {
					m.Delta += d
				}
			}
		default:
			d := distribution(values)
			m.P50, m.P95, m.Max = d.P50, d.P95, d.Max
		}
		out[q.Name] = m
	}
	return out, nil
}

func queryRange(ctx context.Context, hc *http.Client, base, expr string, start, end time.Time) ([]float64, error) {
	v := url.Values{"query": {expr}, "start": {strconv.FormatInt(start.Unix(), 10)}, "end": {strconv.FormatInt(end.Unix(), 10)}, "step": {"15"}}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+"/api/v1/query_range?"+v.Encode(), nil)
	if err != nil {
		return nil, err
	}
	resp, err := hc.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var body struct {
		Status string `json:"status"`
		Error  string `json:"error"`
		Data   struct {
			Result []struct {
				Values [][2]any `json:"values"`
			} `json:"result"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, err
	}
	if body.Status != "success" {
		return nil, fmt.Errorf("prometheus: %s", body.Error)
	}
	var out []float64
	for _, series := range body.Data.Result {
		for _, pair := range series.Values {
			s, _ := pair[1].(string)
			if f, err := strconv.ParseFloat(s, 64); err == nil {
				out = append(out, f)
			}
		}
	}
	return out, nil
}
