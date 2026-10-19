// Command load drives the public API at a profile's arrival rates and reports
// latency distributions (P3.5, plan 0004). See README.md.
//
//	go run . --api https://api.example --profile profiles/development.json \
//	    [--prometheus http://127.0.0.1:9090] [--out DIR] [--run ID]
//
// The bearer token is read from ANVILKIT_LOAD_TOKEN and never printed.
// Exit 0 MEASURED, 1 FAIL (a scenario's failed-request ratio over the
// profile's bound), 2 PARTIAL (a scenario or the cluster readout not
// exercised) or a refused invocation.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"time"
)

// Summary is summary.json of one run.
type Summary struct {
	Tool           string                    `json:"tool"`
	RunID          string                    `json:"runId"`
	ProfileID      string                    `json:"profileId"`
	Classification string                    `json:"classification"`
	Source         string                    `json:"source,omitempty"`
	APIHost        string                    `json:"apiHost"`
	StartedAt      time.Time                 `json:"startedAt"`
	FinishedAt     time.Time                 `json:"finishedAt"`
	Scenarios      map[string]ScenarioResult `json:"scenarios"`
	Cluster        map[string]ClusterMetric  `json:"cluster,omitempty"`
	ClusterStatus  string                    `json:"clusterStatus"`
	Status         string                    `json:"status"`
	Reasons        []string                  `json:"reasons,omitempty"`
}

func main() { os.Exit(run()) }

func run() int {
	api := flag.String("api", os.Getenv("ANVILKIT_LOAD_API_URL"), "the public API base URL")
	profilePath := flag.String("profile", "profiles/development.json", "the load profile")
	prometheus := flag.String("prometheus", os.Getenv("ANVILKIT_LOAD_PROMETHEUS_URL"), "the metrics stack's Prometheus base URL (optional)")
	out := flag.String("out", "", "the output directory (default outputs/load/<run>)")
	runID := flag.String("run", time.Now().UTC().Format("20060102T150405Z"), "the run id")
	flag.Parse()
	profile, err := loadProfile(*profilePath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "refused:", err)
		return 2
	}
	token := os.Getenv("ANVILKIT_LOAD_TOKEN")
	u, err := url.Parse(*api)
	if *api == "" || err != nil || (u.Scheme != "https" && u.Scheme != "http") || token == "" {
		fmt.Fprintln(os.Stderr, "UNEXECUTED: --api (or ANVILKIT_LOAD_API_URL) and ANVILKIT_LOAD_TOKEN name the target API and a tenant's bearer token")
		return 2
	}
	if *out == "" {
		*out = filepath.Join(repoRoot(), "outputs/load", *runID)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	s := execute(ctx, newClient(*api, token), profile, inputsFromEnv(), *runID, *prometheus)
	s.APIHost = u.Host
	if err := write(*out, s); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	brief := map[string]any{"status": s.Status, "reasons": s.Reasons, "summary": filepath.Join(*out, "summary.json")}
	raw, _ := json.MarshalIndent(brief, "", " ")
	fmt.Println(string(raw))
	switch s.Status {
	case "MEASURED":
		return 0
	case "FAIL":
		return 1
	}
	return 2
}

// execute runs the profile and derives the status: FAIL when a scenario's
// failed-request ratio is over the bound, PARTIAL when a scenario was not
// exercised or the cluster readout is missing, MEASURED otherwise. A
// DEVELOPMENT_ONLY profile measures but never sizes production; the
// classification is carried to the summary.
func execute(ctx context.Context, c *client, p Profile, in Inputs, runID, prometheus string) Summary {
	s := Summary{Tool: "anvilkit-load", RunID: runID, ProfileID: p.ProfileID, Classification: p.Classification, Source: p.Source, StartedAt: time.Now().UTC()}
	r := &runner{c: c, profile: p, in: in, runID: runID, rec: map[string]*recorder{}}
	s.Scenarios = r.run(ctx)
	s.FinishedAt = time.Now().UTC()
	names := make([]string, 0, len(s.Scenarios))
	for name := range s.Scenarios {
		names = append(names, name)
	}
	sort.Strings(names)
	failed := false
	for _, name := range names {
		res := s.Scenarios[name]
		switch {
		case res.Status != "EXERCISED":
			s.Reasons = append(s.Reasons, fmt.Sprintf("%s NOT_EXERCISED: %s", name, res.Reason))
		case res.Requests == 0:
			s.Reasons = append(s.Reasons, name+": no request completed")
		case res.ErrorRatio > p.MaxErrorRatio:
			failed = true
			s.Reasons = append(s.Reasons, fmt.Sprintf("%s: failed-request ratio %.4f over the bound %.4f", name, res.ErrorRatio, p.MaxErrorRatio))
		}
	}
	if prometheus == "" {
		s.ClusterStatus = "NOT_MEASURED"
		s.Reasons = append(s.Reasons, "cluster readout NOT_MEASURED: no --prometheus (queue oldest age and pool utilization come from the P3.6 stack)")
	} else if m, err := readCluster(ctx, prometheus, s.StartedAt, s.FinishedAt); err != nil {
		s.ClusterStatus = "NOT_MEASURED"
		s.Reasons = append(s.Reasons, "cluster readout NOT_MEASURED: "+err.Error())
	} else {
		s.Cluster, s.ClusterStatus = m, "MEASURED"
		for name, v := range m {
			if v.Missing {
				s.ClusterStatus = "PARTIAL"
				s.Reasons = append(s.Reasons, "cluster metric "+name+" has no samples")
			}
		}
	}
	sort.Strings(s.Reasons)
	switch {
	case failed:
		s.Status = "FAIL"
	case len(s.Reasons) > 0:
		s.Status = "PARTIAL"
	default:
		s.Status = "MEASURED"
	}
	return s
}

func write(dir string, s Summary) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, "summary.json"), append(raw, '\n'), 0o644)
}

// repoRoot is the checkout root: the nearest parent holding go.work.
func repoRoot() string {
	dir, _ := os.Getwd()
	for d := dir; d != filepath.Dir(d); d = filepath.Dir(d) {
		if _, err := os.Stat(filepath.Join(d, "go.work")); err == nil {
			return d
		}
	}
	return dir
}
