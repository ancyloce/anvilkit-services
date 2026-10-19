package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

const testToken = "tok-load-secret-0123456789"

// apiDouble is an in-process double of the public API surface the tool
// drives: operations with an SSE stream (accepted → running → running →
// succeeded, or canceled after a cancel command), searches and the MCP catalog.
type apiDouble struct {
	mu       sync.Mutex
	ops      map[string]*opState
	created  atomic.Int64
	fail     bool          // every create answers 503
	slow     time.Duration // between frames
	skipSeq  bool          // a resumed stream skips one event (a gap)
	drainAt  int           // a first stream ends with `end` after this event (a draining replica)
	commands map[string]string
	accepted map[string]int
}

type opState struct {
	canceled bool
}

func newAPIDouble() *apiDouble {
	return &apiDouble{ops: map[string]*opState{}, commands: map[string]string{}, accepted: map[string]int{}, slow: 20 * time.Millisecond}
}

func (d *apiDouble) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("Authorization") != "Bearer "+testToken {
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	path := strings.TrimPrefix(r.URL.Path, "/api/v1")
	switch {
	case r.Method == http.MethodPost && (path == "/operations" || path == "/preparations"):
		if d.fail {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		var body struct {
			CommandID string `json:"commandId"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		d.mu.Lock()
		id, ok := d.commands[body.CommandID]
		if !ok {
			id = fmt.Sprintf("op-%d", d.created.Add(1))
			d.commands[body.CommandID] = id
			d.ops[id] = &opState{}
		}
		d.mu.Unlock()
		w.WriteHeader(http.StatusAccepted)
		_ = json.NewEncoder(w).Encode(map[string]any{"operationId": id, "revision": "1", "coveredEventSeq": "1", "lifecycle": "accepted"})
	case r.Method == http.MethodGet && strings.HasSuffix(path, "/events"):
		d.stream(w, r, strings.TrimSuffix(strings.TrimPrefix(path, "/operations/"), "/events"))
	case r.Method == http.MethodPost && strings.HasSuffix(path, "/commands"):
		id := strings.TrimSuffix(strings.TrimPrefix(path, "/operations/"), "/commands")
		var body struct {
			Kind             string `json:"kind"`
			ExpectedRevision string `json:"expectedRevision"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		// cancel and hold at the running revision 2; resume at the held revision 3.
		want := map[string]string{"cancel": "2", "hold": "2", "resume": "3"}[body.Kind]
		d.mu.Lock()
		op := d.ops[id]
		if op != nil && body.Kind == "cancel" && body.ExpectedRevision == want {
			op.canceled = true
		}
		if op != nil && body.ExpectedRevision == want {
			d.accepted[body.Kind]++
		}
		d.mu.Unlock()
		if op == nil || body.ExpectedRevision != want {
			w.WriteHeader(http.StatusConflict)
			return
		}
		w.WriteHeader(http.StatusAccepted)
		_, _ = w.Write([]byte(`{}`))
	case r.Method == http.MethodGet && strings.HasPrefix(path, "/operations/"):
		_ = json.NewEncoder(w).Encode(map[string]any{"operationId": strings.TrimPrefix(path, "/operations/"), "revision": "3", "lifecycle": "suspended"})
	case r.Method == http.MethodPost && path == "/knowledge/searches":
		_, _ = w.Write([]byte(`{"items":[],"noAnswer":true,"indexGeneration":"1"}`))
	case r.Method == http.MethodGet && path == "/mcp/catalog":
		_, _ = w.Write([]byte(`{"descriptors":[]}`))
	default:
		w.WriteHeader(http.StatusNotFound)
	}
}

func (d *apiDouble) stream(w http.ResponseWriter, r *http.Request, id string) {
	d.mu.Lock()
	op := d.ops[id]
	d.mu.Unlock()
	if op == nil {
		w.WriteHeader(http.StatusNotFound)
		return
	}
	after, _ := strconv.Atoi(r.Header.Get("Last-Event-ID"))
	w.Header().Set("Content-Type", "text/event-stream")
	w.WriteHeader(http.StatusOK)
	fl := w.(http.Flusher)
	send := func(seq int, lifecycle string) {
		fmt.Fprintf(w, "id: %d\nevent: operation.changed\ndata: {\"operationId\":%q,\"eventSeq\":\"%d\",\"revision\":\"%d\",\"payload\":{\"lifecycle\":%q}}\n\n", seq, id, seq, seq, lifecycle)
		fl.Flush()
	}
	fmt.Fprint(w, ": keepalive\n\n")
	fl.Flush()
	for seq := after + 1; seq <= 4; seq++ {
		if after > 0 && d.skipSeq && seq == after+1 {
			continue
		}
		select {
		case <-r.Context().Done():
			return
		case <-time.After(d.slow):
		}
		lifecycle := map[int]string{1: "accepted", 2: "running", 3: "running", 4: "succeeded"}[seq]
		if seq == 4 {
			d.mu.Lock()
			if op.canceled {
				lifecycle = "canceled"
			}
			d.mu.Unlock()
		}
		send(seq, lifecycle)
		if d.drainAt > 0 && after == 0 && seq == d.drainAt {
			fmt.Fprintf(w, "event: end\ndata: {\"operationId\":%q,\"reason\":\"server_shutdown\"}\n\n", id)
			fl.Flush()
			return
		}
	}
	<-r.Context().Done()
}

func promDouble(t *testing.T) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query().Get("query")
		values := `[[1,"1"],[2,"3"],[3,"2"]]`
		if strings.Contains(q, "kube_pod_container_status_restarts_total") {
			values = `[[1,"5"],[2,"5"],[3,"5"]]`
		}
		fmt.Fprintf(w, `{"status":"success","data":{"resultType":"matrix","result":[{"metric":{},"values":%s}]}}`, values)
	}))
}

func testProfile(seconds int) Profile {
	return Profile{ProfileID: "test", Classification: ClassDevelopment, DurationSeconds: seconds, DrainSeconds: 10, MaxErrorRatio: 0.01,
		Scenarios: map[string]Scenario{
			"local_check":      {RatePerSecond: 10, MaxInFlight: 50, CancelRatio: 0.3, HoldRatio: 0.3, HoldSeconds: 0, ResumeStream: true, ExtraListeners: 1, DeadlineSeconds: 10},
			"preparation":      {RatePerSecond: 1, MaxInFlight: 5},
			"knowledge_search": {RatePerSecond: 10, MaxInFlight: 5, Query: "q"},
			"mcp_catalog":      {RatePerSecond: 10, MaxInFlight: 5},
		}}
}

func TestProfilesAreValidatedAndTheShippedOnesLoad(t *testing.T) {
	for _, path := range []string{"profiles/development.json", "profiles/soak-development.json"} {
		p, err := loadProfile(path)
		if err != nil {
			t.Fatalf("%s: %v", path, err)
		}
		if p.Classification != ClassDevelopment {
			t.Fatalf("%s: the shipped profiles are development rates", path)
		}
	}
	dir := t.TempDir()
	for name, body := range map[string]string{
		"unknown field":     `{"profileId":"x","classification":"DEVELOPMENT_ONLY","durationSeconds":1,"maxErrorRatio":0,"scenarios":{"mcp_catalog":{"ratePerSecond":1,"maxInFlight":1,"ratePerSecnd":2}}}`,
		"unknown scenario":  `{"profileId":"x","classification":"DEVELOPMENT_ONLY","durationSeconds":1,"maxErrorRatio":0,"scenarios":{"bogus":{"ratePerSecond":1,"maxInFlight":1}}}`,
		"ENV-05 sourceless": `{"profileId":"x","classification":"ENV-05","durationSeconds":1,"maxErrorRatio":0,"scenarios":{"mcp_catalog":{"ratePerSecond":1,"maxInFlight":1}}}`,
		"zero rate":         `{"profileId":"x","classification":"DEVELOPMENT_ONLY","durationSeconds":1,"maxErrorRatio":0,"scenarios":{"mcp_catalog":{"ratePerSecond":0,"maxInFlight":1}}}`,
		"no class":          `{"profileId":"x","durationSeconds":1,"maxErrorRatio":0,"scenarios":{"mcp_catalog":{"ratePerSecond":1,"maxInFlight":1}}}`,
	} {
		path := filepath.Join(dir, strings.ReplaceAll(name, " ", "-")+".json")
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
		if _, err := loadProfile(path); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestPercentilesAreNearestRank(t *testing.T) {
	s := []float64{1, 2, 3, 4, 5, 6, 7, 8, 9, 10}
	d := distribution(s)
	if d.P50 != 5 || d.P95 != 10 || d.Max != 10 || d.Count != 10 {
		t.Fatalf("got %+v", d)
	}
	if got := distribution(nil); got.Count != 0 {
		t.Fatalf("empty: %+v", got)
	}
}

func TestARunAgainstTheDoubleMeasuresEveryLatencyAndNeverLeaksTheToken(t *testing.T) {
	api := httptest.NewServer(newAPIDouble())
	defer api.Close()
	prom := promDouble(t)
	defer prom.Close()
	in := Inputs{SnapshotID: "snap-1", RetrievalProfileID: "rp-1", LocalCheckDigest: localCheckDigest}
	s := execute(context.Background(), newClient(api.URL, testToken), testProfile(2), in, "t1", prom.URL)
	lc := s.Scenarios["local_check"]
	for _, name := range []string{"admission", "firstProgress", "endToEnd", "sseRecovery", "cancel"} {
		if lc.Latencies[name].Count == 0 {
			t.Errorf("local_check %s not measured: %+v", name, lc)
		}
	}
	if lc.Counters["outcome:succeeded"] == 0 || lc.Counters["outcome:canceled"] == 0 {
		t.Errorf("outcomes: %+v", lc.Counters)
	}
	if lc.Counters["holdAccepted"] == 0 || lc.Counters["resumeAccepted"] != lc.Counters["holdAccepted"] || lc.Latencies["resume"].Count == 0 {
		t.Errorf("holds: %+v", lc.Counters)
	}
	for _, bad := range []string{"gapFrames", "duplicateFrames", "timedOut", "listenerMissedSettlement"} {
		if lc.Counters[bad] != 0 {
			t.Errorf("%s = %d", bad, lc.Counters[bad])
		}
	}
	if s.Scenarios["knowledge_search"].Latencies["search"].Count == 0 || s.Scenarios["mcp_catalog"].Latencies["catalog"].Count == 0 {
		t.Errorf("search/catalog not measured")
	}
	if p := s.Scenarios["preparation"]; p.Status != "NOT_EXERCISED" || !strings.Contains(p.Reason, "ANVILKIT_LOAD_PROMPT_TRANSFER_ID") {
		t.Errorf("preparation without its transfer: %+v", p)
	}
	if s.Status != "PARTIAL" || s.ClusterStatus != "MEASURED" {
		t.Errorf("status %s cluster %s reasons %v", s.Status, s.ClusterStatus, s.Reasons)
	}
	if m := s.Cluster["controlGrpcPoolUtilization"]; m.P95 != 3 || m.Max != 3 {
		t.Errorf("pool utilization: %+v", m)
	}
	if m := s.Cluster["appPodRestarts"]; m.Delta != 0 {
		t.Errorf("restarts: %+v", m)
	}
	raw, _ := json.Marshal(s)
	if strings.Contains(string(raw), testToken) {
		t.Fatal("the summary carries the bearer token")
	}
}

func TestFailedRequestsOverTheBoundFailTheRun(t *testing.T) {
	d := newAPIDouble()
	d.fail = true
	api := httptest.NewServer(d)
	defer api.Close()
	p := testProfile(1)
	p.Scenarios = map[string]Scenario{"local_check": p.Scenarios["local_check"]}
	s := execute(context.Background(), newClient(api.URL, testToken), p, Inputs{LocalCheckDigest: localCheckDigest}, "t2", "")
	if s.Status != "FAIL" || s.Scenarios["local_check"].StatusCodes["503"] == 0 {
		t.Fatalf("status %s: %+v", s.Status, s.Scenarios["local_check"])
	}
}

func TestAResumedStreamThatSkipsAnEventIsAGap(t *testing.T) {
	d := newAPIDouble()
	d.skipSeq = true
	api := httptest.NewServer(d)
	defer api.Close()
	p := testProfile(1)
	lc := p.Scenarios["local_check"]
	lc.CancelRatio, lc.ExtraListeners = 0, 0
	p.Scenarios = map[string]Scenario{"local_check": lc}
	s := execute(context.Background(), newClient(api.URL, testToken), p, Inputs{LocalCheckDigest: localCheckDigest}, "t3", "")
	if s.Scenarios["local_check"].Counters["gapFrames"] == 0 {
		t.Fatalf("no gap counted: %+v", s.Scenarios["local_check"].Counters)
	}
}

func TestArrivalsOverMaxInFlightAreDroppedNotQueued(t *testing.T) {
	d := newAPIDouble()
	d.slow = 400 * time.Millisecond
	api := httptest.NewServer(d)
	defer api.Close()
	p := testProfile(1)
	lc := p.Scenarios["local_check"]
	lc.MaxInFlight, lc.ExtraListeners, lc.ResumeStream, lc.CancelRatio = 1, 0, false, 0
	p.Scenarios = map[string]Scenario{"local_check": lc}
	s := execute(context.Background(), newClient(api.URL, testToken), p, Inputs{LocalCheckDigest: localCheckDigest}, "t4", "")
	res := s.Scenarios["local_check"]
	if res.Dropped == 0 || res.Arrivals != 10 {
		t.Fatalf("arrivals %d dropped %d", res.Arrivals, res.Dropped)
	}
}

func TestCancelAndHoldSelectionsHoldTheirSharesAndNeverOverlap(t *testing.T) {
	s := Scenario{CancelRatio: 0.2, HoldRatio: 0.1}
	canceled, held := 0, 0
	for i := range 1000 {
		c, h := cancels(i, s.CancelRatio), holds(i, s)
		if c && h {
			t.Fatalf("arrival %d both canceled and held", i)
		}
		if c {
			canceled++
		}
		if h {
			held++
		}
	}
	if canceled < 190 || canceled > 210 || held < 90 || held > 110 {
		t.Fatalf("shares %d/%d of 1000", canceled, held)
	}
}

func TestTheCommandRefusesWithoutATargetAndWritesItsSummary(t *testing.T) {
	invoke := func(args ...string) int {
		flag.CommandLine = flag.NewFlagSet("load", flag.ContinueOnError)
		os.Args = append([]string{"load"}, args...)
		return run()
	}
	t.Setenv("ANVILKIT_LOAD_API_URL", "")
	t.Setenv("ANVILKIT_LOAD_TOKEN", "")
	if rc := invoke("--profile", "profiles/development.json"); rc != 2 {
		t.Fatalf("no target: rc %d", rc)
	}
	api := httptest.NewServer(newAPIDouble())
	defer api.Close()
	dir := t.TempDir()
	profile := filepath.Join(dir, "p.json")
	if err := os.WriteFile(profile, []byte(`{"profileId":"cli","classification":"DEVELOPMENT_ONLY","durationSeconds":1,"drainSeconds":5,"maxErrorRatio":0.01,
		"scenarios":{"mcp_catalog":{"ratePerSecond":5,"maxInFlight":2}}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("ANVILKIT_LOAD_TOKEN", testToken)
	// No Prometheus: the cluster readout is NOT_MEASURED, so the run is PARTIAL (2).
	if rc := invoke("--api", api.URL, "--profile", profile, "--out", dir, "--run", "cli"); rc != 2 {
		t.Fatalf("rc %d", rc)
	}
	raw, err := os.ReadFile(filepath.Join(dir, "summary.json"))
	if err != nil {
		t.Fatal(err)
	}
	var s Summary
	if err := json.Unmarshal(raw, &s); err != nil || s.Status != "PARTIAL" || s.ClusterStatus != "NOT_MEASURED" || s.Classification != ClassDevelopment {
		t.Fatalf("summary %+v (%v)", s, err)
	}
	if strings.Contains(string(raw), testToken) {
		t.Fatal("token in the summary")
	}
}

func TestAnEndFrameResumesFromTheLastEventWithoutAGap(t *testing.T) {
	d := newAPIDouble()
	d.drainAt = 2
	api := httptest.NewServer(d)
	defer api.Close()
	p := testProfile(1)
	lc := p.Scenarios["local_check"]
	lc.CancelRatio, lc.ExtraListeners, lc.HoldRatio, lc.ResumeStream = 0, 0, 0, false
	p.Scenarios = map[string]Scenario{"local_check": lc}
	s := execute(context.Background(), newClient(api.URL, testToken), p, Inputs{LocalCheckDigest: localCheckDigest}, "t-end", "")
	c := s.Scenarios["local_check"].Counters
	if c["serverEnded"] == 0 || c["gapFrames"] != 0 || c["duplicateFrames"] != 0 || c["streamDropped"] != 0 || c["outcome:succeeded"] == 0 {
		t.Fatalf("an end frame must resume from the last event id with nothing lost or repeated: %+v", c)
	}
}
