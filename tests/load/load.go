package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strconv"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

type statusError struct{ code int }

func (e *statusError) Error() string { return "HTTP " + strconv.Itoa(e.code) }

// client calls the public API with one tenant's bearer token. The token is
// never written to the summary or the log.
type client struct {
	base   string
	token  string
	http   *http.Client // bounded requests
	stream *http.Client // SSE: no overall timeout
}

func newClient(base, token string) *client {
	// One transport with room for every concurrent stream: Go's default keeps two
	// idle connections per host, so the streams redialled on every open.
	t := http.DefaultTransport.(*http.Transport).Clone()
	t.MaxIdleConnsPerHost = 256
	return &client{base: base, token: token, http: &http.Client{Timeout: 60 * time.Second, Transport: t}, stream: &http.Client{Transport: t}}
}

// wait sleeps d unless ctx ends first.
func wait(ctx context.Context, d time.Duration) {
	select {
	case <-ctx.Done():
	case <-time.After(d):
	}
}

// transportClass names a request error that carried no status (a counter
// alongside "transport", which stays the failure): where it happened and why.
func transportClass(err error) string {
	where := "conn"
	var op *net.OpError
	if errors.As(err, &op) && op.Op == "dial" {
		where = "dial"
	}
	var ne net.Error
	switch {
	case errors.As(err, &ne) && ne.Timeout():
		return where + ":timeout"
	case errors.Is(err, syscall.ECONNREFUSED):
		return where + ":refused"
	case errors.Is(err, syscall.ECONNRESET), errors.Is(err, syscall.EPIPE):
		return where + ":reset"
	case errors.Is(err, syscall.EADDRNOTAVAIL):
		return where + ":addrNotAvail"
	case errors.Is(err, syscall.EMFILE), errors.Is(err, syscall.ENFILE):
		return where + ":fdLimit"
	case errors.Is(err, io.EOF), errors.Is(err, io.ErrUnexpectedEOF):
		return where + ":eof"
	}
	return where + ":other"
}

// call sends one JSON request and decodes a JSON answer into out (when non-nil).
func (c *client) call(ctx context.Context, method, path string, body, out any) (int, error) {
	var rd io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return 0, err
		}
		rd = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base+path, rd)
	if err != nil {
		return 0, err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return resp.StatusCode, err
	}
	if out != nil && resp.StatusCode/100 == 2 {
		if err := json.Unmarshal(raw, out); err != nil {
			return resp.StatusCode, fmt.Errorf("answer: %w", err)
		}
	}
	return resp.StatusCode, nil
}

// recorder collects one scenario's samples.
type recorder struct {
	mu        sync.Mutex
	latencies map[string][]time.Duration
	counters  map[string]int
	codes     map[string]int
	requests  int
	errors    int
	arrivals  atomic.Int64
	dropped   atomic.Int64
}

func newRecorder() *recorder {
	return &recorder{latencies: map[string][]time.Duration{}, counters: map[string]int{}, codes: map[string]int{}}
}

func (r *recorder) sample(name string, d time.Duration) {
	r.mu.Lock()
	r.latencies[name] = append(r.latencies[name], d)
	r.mu.Unlock()
}

func (r *recorder) count(name string) {
	r.mu.Lock()
	r.counters[name]++
	r.mu.Unlock()
}

// request records one request's answer: a 5xx or a transport error is a
// failed request; 4xx answers are counted by code (a 429 refusal or a 409
// conflict is a designed answer under load, not a failure).
func (r *recorder) request(code int, err error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.requests++
	switch {
	case code == 0 && err != nil:
		r.errors++
		r.codes["transport"]++
		r.counters["transport:"+transportClass(err)]++
	default:
		r.codes[strconv.Itoa(code)]++
		if code >= 500 {
			r.errors++
		}
	}
}

// Latency is the distribution of one measured latency in milliseconds.
type Latency struct {
	Count int     `json:"count"`
	P50   float64 `json:"p50Ms"`
	P95   float64 `json:"p95Ms"`
	P99   float64 `json:"p99Ms"`
	Max   float64 `json:"maxMs"`
}

// percentile is the nearest-rank percentile of sorted samples.
func percentile(sorted []float64, p float64) float64 {
	if len(sorted) == 0 {
		return 0
	}
	rank := int(math.Ceil(p/100*float64(len(sorted)))) - 1
	rank = max(0, min(rank, len(sorted)-1))
	return sorted[rank]
}

func distribution(samples []float64) Latency {
	s := append([]float64(nil), samples...)
	sort.Float64s(s)
	if len(s) == 0 {
		return Latency{}
	}
	return Latency{Count: len(s), P50: percentile(s, 50), P95: percentile(s, 95), P99: percentile(s, 99), Max: s[len(s)-1]}
}

// ScenarioResult is one scenario's part of the summary.
type ScenarioResult struct {
	Status      string             `json:"status"`
	Reason      string             `json:"reason,omitempty"`
	Rate        float64            `json:"ratePerSecond"`
	Arrivals    int64              `json:"arrivals"`
	Dropped     int64              `json:"droppedAtMaxInFlight"`
	Requests    int                `json:"requests"`
	Errors      int                `json:"failedRequests"`
	ErrorRatio  float64            `json:"failedRequestRatio"`
	StatusCodes map[string]int     `json:"statusCodes"`
	Counters    map[string]int     `json:"counters"`
	Latencies   map[string]Latency `json:"latencies"`
}

func (r *recorder) result(rate float64) ScenarioResult {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := ScenarioResult{Status: "EXERCISED", Rate: rate, Arrivals: r.arrivals.Load(), Dropped: r.dropped.Load(), Requests: r.requests,
		Errors: r.errors, StatusCodes: r.codes, Counters: r.counters, Latencies: map[string]Latency{}}
	if r.requests > 0 {
		out.ErrorRatio = float64(r.errors) / float64(r.requests)
	}
	for name, ds := range r.latencies {
		ms := make([]float64, len(ds))
		for i, d := range ds {
			ms[i] = float64(d) / float64(time.Millisecond)
		}
		out.Latencies[name] = distribution(ms)
	}
	return out
}

// Inputs the scenarios need beyond the profile; a scenario whose inputs are
// absent is NOT_EXERCISED with the missing names, never run with placeholders.
type Inputs struct {
	PromptTransferID   string
	PromptDigest       string
	SnapshotID         string
	RetrievalProfileID string
	LocalCheckDigest   string
}

func inputsFromEnv() Inputs {
	return Inputs{
		PromptTransferID:   os.Getenv("ANVILKIT_LOAD_PROMPT_TRANSFER_ID"),
		PromptDigest:       os.Getenv("ANVILKIT_LOAD_PROMPT_DIGEST"),
		SnapshotID:         os.Getenv("ANVILKIT_LOAD_SNAPSHOT_ID"),
		RetrievalProfileID: os.Getenv("ANVILKIT_LOAD_RETRIEVAL_PROFILE_ID"),
		LocalCheckDigest:   envOr("ANVILKIT_LOAD_LOCAL_CHECK_DIGEST", localCheckDigest),
	}
}

func envOr(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}

// localCheckDigest is the fixed subject of the local-check-v1 profile.
const localCheckDigest = "sha256:0dc7fa9db7237a2b5c96f70f59bb00f73bb86a0ca5554e91c312f9ada26e18b3"

func (in Inputs) missing(scenario string) string {
	switch scenario {
	case "preparation":
		if in.PromptTransferID == "" || in.PromptDigest == "" {
			return "ANVILKIT_LOAD_PROMPT_TRANSFER_ID and ANVILKIT_LOAD_PROMPT_DIGEST (a finalized prompt transfer of the tenant) are not set"
		}
	case "knowledge_search":
		if in.SnapshotID == "" || in.RetrievalProfileID == "" {
			return "ANVILKIT_LOAD_SNAPSHOT_ID and ANVILKIT_LOAD_RETRIEVAL_PROFILE_ID (a ready snapshot of the tenant) are not set"
		}
	}
	return ""
}

// runner drives one profile against one API.
type runner struct {
	c       *client
	profile Profile
	in      Inputs
	runID   string
	rec     map[string]*recorder
}

// run schedules every scenario's arrivals open-model (a fixed rate regardless
// of answers, bounded by maxInFlight; an arrival over the bound is dropped and
// counted) for the profile's duration, then drains in-flight work.
func (r *runner) run(ctx context.Context) map[string]ScenarioResult {
	duration := time.Duration(r.profile.DurationSeconds) * time.Second
	drainCtx, cancel := context.WithTimeout(ctx, duration+time.Duration(r.profile.DrainSeconds)*time.Second)
	defer cancel()
	results := map[string]ScenarioResult{}
	var all sync.WaitGroup
	for name, s := range r.profile.Scenarios {
		if why := r.in.missing(name); why != "" {
			results[name] = ScenarioResult{Status: "NOT_EXERCISED", Reason: why, Rate: s.RatePerSecond}
			continue
		}
		rec := newRecorder()
		r.rec[name] = rec
		all.Add(1)
		go func() {
			defer all.Done()
			r.schedule(ctx, drainCtx, name, s, rec, duration)
		}()
	}
	all.Wait()
	for name, rec := range r.rec {
		results[name] = rec.result(r.profile.Scenarios[name].RatePerSecond)
	}
	return results
}

func (r *runner) schedule(arrivalCtx, workCtx context.Context, name string, s Scenario, rec *recorder, duration time.Duration) {
	interval := time.Duration(float64(time.Second) / s.RatePerSecond)
	start := time.Now()
	var inFlight atomic.Int64
	var wg sync.WaitGroup
	for i := 0; ; i++ {
		next := start.Add(time.Duration(i) * interval)
		if next.Sub(start) >= duration {
			break
		}
		select {
		case <-arrivalCtx.Done():
			wg.Wait()
			return
		case <-time.After(time.Until(next)):
		}
		rec.arrivals.Add(1)
		if inFlight.Load() >= int64(s.MaxInFlight) {
			rec.dropped.Add(1)
			continue
		}
		inFlight.Add(1)
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer inFlight.Add(-1)
			r.arrive(workCtx, name, s, rec, i)
		}()
	}
	wg.Wait()
}

func (r *runner) arrive(ctx context.Context, name string, s Scenario, rec *recorder, n int) {
	switch name {
	case "local_check":
		body := map[string]any{"commandId": r.commandID("lc", n), "kind": "local_check",
			"subject": map[string]any{"profileId": "local-check-v1", "subjectDigest": r.in.LocalCheckDigest}}
		r.operation(ctx, s, rec, n, "/api/v1/operations", body, func(f frame) bool { return f.terminal() })
	case "preparation":
		body := map[string]any{"commandId": r.commandID("prep", n),
			"prompt": map[string]any{"transferId": r.in.PromptTransferID, "digest": r.in.PromptDigest}}
		// A Preparation settles when it waits for a clarification answer or ends.
		r.operation(ctx, s, rec, n, "/api/v1/preparations", body, func(f frame) bool { return f.terminal() || f.lifecycle() == "waiting" })
	case "knowledge_search":
		body := map[string]any{"snapshotId": r.in.SnapshotID, "query": s.Query, "retrievalProfileId": r.in.RetrievalProfileID}
		if s.MaxContextItems > 0 {
			body["maxContextItems"] = s.MaxContextItems
		}
		var out struct {
			NoAnswer bool `json:"noAnswer"`
		}
		t0 := time.Now()
		code, err := r.c.call(ctx, http.MethodPost, "/api/v1/knowledge/searches", body, &out)
		rec.request(code, err)
		if code == http.StatusOK {
			rec.sample("search", time.Since(t0))
			if out.NoAnswer {
				rec.count("noAnswer")
			}
		}
	case "mcp_catalog":
		t0 := time.Now()
		code, err := r.c.call(ctx, http.MethodGet, "/api/v1/mcp/catalog?limit=50", nil, nil)
		rec.request(code, err)
		if code == http.StatusOK {
			rec.sample("catalog", time.Since(t0))
		}
	}
}

func (r *runner) commandID(kind string, n int) string {
	return fmt.Sprintf("cmd-load-%s-%s-%d", r.runID, kind, n)
}

// share is arrival n's position in [0,1): a low-discrepancy sequence, so a
// ratio holds for short runs too. The canceled share is [0, cancelRatio), the
// held share [cancelRatio, cancelRatio+holdRatio): never both.
func share(n int) float64 {
	_, frac := math.Modf(float64(n+1) * 0.6180339887498949)
	return frac
}

func cancels(n int, ratio float64) bool { return share(n) < ratio }

func holds(n int, s Scenario) bool {
	return s.HoldRatio > 0 && share(n) >= s.CancelRatio && share(n) < s.CancelRatio+s.HoldRatio
}

type operationView struct {
	OperationID     string `json:"operationId"`
	Revision        string `json:"revision"`
	CoveredEventSeq string `json:"coveredEventSeq"`
	Lifecycle       string `json:"lifecycle"`
}

// operation creates one operation and follows it on API-04 until settled:
//   - admission: the create request until its 202;
//   - firstProgress: the create request until the first frame past "accepted";
//   - endToEnd: the create request until the settling frame;
//   - sseRecovery: a dropped stream's reconnect (Last-Event-ID) until its 200;
//   - counters: outcomes by lifecycle, cancels, stream gaps and duplicates
//     (eventSeq not contiguous across a resume), reset_required, end (a
//     draining replica ended the stream), timed-out
//     operations, and extra listeners that missed the settling frame.
func (r *runner) operation(ctx context.Context, s Scenario, rec *recorder, n int, path string, body any, settled func(frame) bool) {
	deadline := time.Duration(s.DeadlineSeconds) * time.Second
	if deadline <= 0 {
		deadline = 15 * time.Minute
	}
	ctx, cancel := context.WithTimeout(ctx, deadline)
	defer cancel()
	t0 := time.Now()
	var view operationView
	code, err := r.c.call(ctx, http.MethodPost, path, body, &view)
	rec.request(code, err)
	if code != http.StatusAccepted {
		return
	}
	rec.sample("admission", time.Since(t0))
	var listeners sync.WaitGroup
	for range s.ExtraListeners {
		listeners.Add(1)
		go func() {
			defer listeners.Done()
			if !r.listen(ctx, view.OperationID, settled) {
				rec.count("listenerMissedSettlement")
			}
		}()
	}
	defer listeners.Wait()

	var last uint64
	lastID := ""
	progressed, resumed, canceled, held := false, false, false, false
	var holding sync.WaitGroup
	defer holding.Wait()
	var reconnectAt time.Time
	// Like an EventSource, a stream is reopened after the interval it advertised
	// (`retry:`), or 500 ms before it advertised one.
	retry := 500 * time.Millisecond
	for {
		resp, err := r.c.openStream(ctx, view.OperationID, lastID)
		if err != nil {
			var se *statusError
			if errors.As(err, &se) {
				rec.request(se.code, nil)
			} else if ctx.Err() == nil {
				rec.request(0, err)
			}
			if ctx.Err() != nil {
				rec.count("timedOut")
				return
			}
			wait(ctx, retry)
			continue
		}
		rec.request(resp.StatusCode, nil)
		if !reconnectAt.IsZero() {
			rec.sample("sseRecovery", time.Since(reconnectAt))
		}
		done := false
		dropForResume, serverEnded := false, false
		readErr := readFramesRetry(resp.Body, &retry, func(f frame) bool {
			switch f.Event {
			case "reset_required":
				rec.count("resetRequired")
				covered, _ := f.Data["coveredEventSeq"].(string)
				lastID = covered
				last, _ = strconv.ParseUint(covered, 10, 64)
				return false
			case "end":
				// The replica drains (API-04 `end`, reason server_shutdown): the
				// stream resumes from the last event id, to any replica, after
				// the advertised retry interval; nothing is lost.
				rec.count("serverEnded")
				dropForResume, serverEnded = true, true
				return false
			case "operation.changed":
			default:
				return true
			}
			seq, err := strconv.ParseUint(f.ID, 10, 64)
			if err == nil {
				switch {
				case last != 0 && seq <= last:
					rec.count("duplicateFrames")
				case last != 0 && seq != last+1:
					rec.count("gapFrames")
				}
				if seq > last {
					last = seq
				}
			}
			lastID = f.ID
			if !progressed && f.lifecycle() != "" && f.lifecycle() != "accepted" {
				progressed = true
				rec.sample("firstProgress", time.Since(t0))
			}
			if settled(f) {
				rec.sample("endToEnd", time.Since(t0))
				rec.count("outcome:" + f.lifecycle())
				done = true
				return false
			}
			if progressed && !canceled && cancels(n, s.CancelRatio) {
				canceled = true
				r.cancel(ctx, rec, view.OperationID, f, n)
			}
			if progressed && !held && holds(n, s) {
				held = true
				revision, _ := f.Data["revision"].(string)
				holding.Add(1)
				go func() {
					defer holding.Done()
					r.holdAndResume(ctx, rec, view.OperationID, revision, n, time.Duration(s.HoldSeconds)*time.Second)
				}()
			}
			if progressed && s.ResumeStream && !resumed {
				resumed = true
				dropForResume = true
				return false
			}
			return true
		})
		resp.Body.Close()
		if done {
			return
		}
		if ctx.Err() != nil {
			rec.count("timedOut")
			return
		}
		if !dropForResume && readErr != nil {
			// The API ended the stream (a slow consumer or a restart): resume.
			rec.count("streamDropped")
		}
		// A stream the server ended or dropped is reopened after its retry interval;
		// the deliberate resume reconnects at once. sseRecovery measures the reopen.
		if serverEnded || !dropForResume {
			wait(ctx, retry)
		}
		reconnectAt = time.Now()
	}
}

func (r *runner) cancel(ctx context.Context, rec *recorder, operationID string, f frame, n int) {
	revision, _ := f.Data["revision"].(string)
	r.command(ctx, rec, operationID, "cancel", revision, n)
}

// command submits one operator command and records it as <kind> latency and
// <kind>Accepted, or <kind>Conflict when the revision moved on (the
// operation runs on; a designed answer under load).
func (r *runner) command(ctx context.Context, rec *recorder, operationID, kind, revision string, n int) bool {
	t0 := time.Now()
	code, err := r.c.call(ctx, http.MethodPost, "/api/v1/operations/"+url.PathEscape(operationID)+"/commands",
		map[string]any{"commandId": r.commandID(kind, n), "kind": kind, "expectedRevision": revision}, nil)
	rec.request(code, err)
	switch {
	case code/100 == 2:
		rec.sample(kind, time.Since(t0))
		rec.count(kind + "Accepted")
		return true
	case code == http.StatusConflict:
		rec.count(kind + "Conflict")
	}
	return false
}

// holdAndResume holds the operation, waits, and resumes it at the revision
// the hold produced (read back through API-03).
func (r *runner) holdAndResume(ctx context.Context, rec *recorder, operationID, revision string, n int, wait time.Duration) {
	if !r.command(ctx, rec, operationID, "hold", revision, n) {
		return
	}
	select {
	case <-ctx.Done():
		return
	case <-time.After(wait):
	}
	var view operationView
	code, err := r.c.call(ctx, http.MethodGet, "/api/v1/operations/"+url.PathEscape(operationID), nil, &view)
	rec.request(code, err)
	if code != http.StatusOK {
		return
	}
	r.command(ctx, rec, operationID, "resume", view.Revision, n)
}

// listen follows the stream as an extra subscriber; true when it saw the
// settling frame.
func (r *runner) listen(ctx context.Context, operationID string, settled func(frame) bool) bool {
	lastID := ""
	retry := 500 * time.Millisecond
	for ctx.Err() == nil {
		resp, err := r.c.openStream(ctx, operationID, lastID)
		if err != nil {
			wait(ctx, retry)
			continue
		}
		seen := false
		_ = readFramesRetry(resp.Body, &retry, func(f frame) bool {
			if f.Event == "operation.changed" {
				lastID = f.ID
				seen = settled(f)
			}
			return !seen
		})
		resp.Body.Close()
		if seen {
			return true
		}
		wait(ctx, retry)
	}
	return false
}
