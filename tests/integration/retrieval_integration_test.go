//go:build integration

package integration

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"google.golang.org/protobuf/types/known/timestamppb"

	knowledgev1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/knowledge/v1"
)

// P16 on the development foundation: the labeled development corpus of
// tests/evals/rag goes through the real pipeline — SourceService, the Docling
// parser Jobs on kind, the Background Worker's knowledge-ingest and
// knowledge-project handlers, BGE-M3 embeddings from the Inference service,
// named dense/sparse points in the foundation's Qdrant, the accepted index
// generation — and is searched through RetrievalService under snapshots and
// the current ACL. The evaluator scores every case over repeated trials and
// writes a report under outputs/rag-eval/ (ignored by Git). Asserted: zero
// unauthorized disclosure, every citation verifiable against Knowledge's
// records, the positive authorization control answered, revocation of a
// frozen snapshot and deletion across the index. Reported, never asserted:
// Recall@k, nDCG@k, no-answer behavior, latency and compute — the corpus is
// DEVELOPMENT_ONLY and no quality threshold is invented.

type ragSource struct {
	Doc    string   `json:"doc"`
	File   string   `json:"file"`
	Tenant string   `json:"tenant"`
	Access []string `json:"access"`
}

type ragLabel struct {
	Doc      string  `json:"doc"`
	Evidence string  `json:"evidence"`
	Gain     float64 `json:"gain"`
}

type ragCase struct {
	ID            string     `json:"id"`
	Stratum       string     `json:"stratum"`
	Query         string     `json:"query"`
	As            string     `json:"as"`
	Labels        []ragLabel `json:"labels"`
	Forbidden     []string   `json:"forbidden"`
	ForbiddenDocs []string   `json:"forbiddenDocs"`
}

type ragCorpus struct {
	Comment       string      `json:"$comment"`
	SchemaVersion int         `json:"schemaVersion"`
	K             int         `json:"k"`
	Trials        int         `json:"trials"`
	Sources       []ragSource `json:"sources"`
	Cases         []ragCase   `json:"cases"`
}

type ragTrial struct {
	Trial         int      `json:"trial"`
	LatencyMs     float64  `json:"latencyMs"`
	NoAnswer      bool     `json:"noAnswer"`
	Citations     []string `json:"citations"`
	Recall        *float64 `json:"recallAtK,omitempty"`
	NDCG          *float64 `json:"ndcgAtK,omitempty"`
	Disclosures   int      `json:"unauthorizedDisclosures"`
	Invalid       int      `json:"invalidCitations"`
	Failures      []string `json:"failureClasses"`
	ResponseBytes string   `json:"responseDigest"`
}

type ragCaseReport struct {
	ID      string     `json:"id"`
	Stratum string     `json:"stratum"`
	Trials  []ragTrial `json:"trials"`
	Stable  bool       `json:"sameCitationsInEveryTrial"`
}

func ragRoot(t *testing.T) string { return filepath.Join(repoRoot(t), "tests", "evals", "rag") }

func loadCorpus(t *testing.T) (ragCorpus, string) {
	raw, err := os.ReadFile(filepath.Join(ragRoot(t), "cases.json"))
	require.NoError(t, err)
	var c ragCorpus
	require.NoError(t, json.Unmarshal(raw, &c))
	h := sha256.New()
	h.Write(raw)
	for _, s := range c.Sources {
		b, err := os.ReadFile(filepath.Join(ragRoot(t), "corpus", s.File))
		require.NoError(t, err)
		fmt.Fprintf(h, "\n%s\n", s.File)
		h.Write(b)
	}
	return c, "sha256:" + hex.EncodeToString(h.Sum(nil))
}

// counters returns the labeled samples of one metric family from a Prometheus text endpoint.
func counters(t *testing.T, health, name string) map[string]float64 {
	resp, err := http.Get("http://" + health + "/metrics")
	require.NoError(t, err)
	defer resp.Body.Close()
	out := map[string]float64{}
	sc := bufio.NewScanner(resp.Body)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		line := sc.Text()
		if !strings.HasPrefix(line, name) || strings.HasPrefix(line, "#") {
			continue
		}
		i := strings.LastIndex(line, " ")
		v, err := strconv.ParseFloat(line[i+1:], 64)
		if err == nil {
			out[line[:i]] = v
		}
	}
	return out
}

func percentile(xs []float64, p float64) float64 {
	if len(xs) == 0 {
		return 0
	}
	s := append([]float64(nil), xs...)
	sort.Float64s(s)
	i := int(math.Ceil(p*float64(len(s)))) - 1
	if i < 0 {
		i = 0
	}
	return s[i]
}

func qdrantGet(t *testing.T, path string) map[string]any {
	req, err := http.NewRequest(http.MethodGet, os.Getenv("ANVILKIT_KNOWLEDGE_QDRANT_URL")+path, nil)
	require.NoError(t, err)
	req.Header.Set("api-key", os.Getenv("ANVILKIT_KNOWLEDGE_QDRANT_API_KEY"))
	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	require.Equal(t, 200, resp.StatusCode, string(body))
	var out map[string]any
	require.NoError(t, json.Unmarshal(body, &out))
	return out
}

func qdrantCount(t *testing.T, collection, key, value string) int {
	body := fmt.Sprintf(`{"exact":true,"filter":{"must":[{"key":%q,"match":{"value":%q}}]}}`, key, value)
	req, err := http.NewRequest(http.MethodPost, os.Getenv("ANVILKIT_KNOWLEDGE_QDRANT_URL")+"/collections/"+collection+"/points/count", strings.NewReader(body))
	require.NoError(t, err)
	req.Header.Set("api-key", os.Getenv("ANVILKIT_KNOWLEDGE_QDRANT_API_KEY"))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()
	var out struct {
		Result struct {
			Count int `json:"count"`
		} `json:"result"`
	}
	require.NoError(t, json.NewDecoder(resp.Body).Decode(&out))
	return out.Result.Count
}

func TestRetrieval(t *testing.T) {
	laneEnv(t, "ANVILKIT_KNOWLEDGE_OBJECTS_ENDPOINT", "ANVILKIT_KNOWLEDGE_OBJECTS_STAGE_ENDPOINT", "ANVILKIT_KNOWLEDGE_OBJECTS_CREDENTIALS_FILE",
		"ANVILKIT_KNOWLEDGE_KUBECONFIG", "ANVILKIT_KNOWLEDGE_IMAGE_REGISTRY", "ANVILKIT_DEV_KNOWLEDGE_MIGRATOR_DSN", "ANVILKIT_DEV_INFERENCE_URL",
		"ANVILKIT_KNOWLEDGE_QDRANT_URL", "ANVILKIT_KNOWLEDGE_QDRANT_API_KEY")
	corpus, corpusDigest := loadCorpus(t)
	infURL := inferenceURL(t)
	// The scenario owns its Qdrant like the lane owns its database (as
	// TestMemory does): the lane's fresh database numbers generations from 1,
	// so the shared node's earlier anvilkit-knowledge-1 points would never let
	// the generation's exact count qualify.
	qdrantURL, qdrantKey := ownQdrant(t)
	t.Setenv("ANVILKIT_KNOWLEDGE_QDRANT_URL", qdrantURL)
	t.Setenv("ANVILKIT_KNOWLEDGE_QDRANT_API_KEY", qdrantKey)
	l := setupLaneWith(t, laneOptions{
		knowledgeReplacements: [][2]string{{`  profile: ""`, "  profile: parser-docling-dev-v1"}, {"  node_pool: parsing", "  node_pool: components"}, inferenceTLS(t)},
		knowledgeEnv: []string{
			"ANVILKIT_KNOWLEDGE_OBJECTS_ENDPOINT=" + os.Getenv("ANVILKIT_KNOWLEDGE_OBJECTS_ENDPOINT"),
			"ANVILKIT_KNOWLEDGE_OBJECTS_STAGE_ENDPOINT=" + os.Getenv("ANVILKIT_KNOWLEDGE_OBJECTS_STAGE_ENDPOINT"),
			"ANVILKIT_KNOWLEDGE_OBJECTS_CREDENTIALS_FILE=" + os.Getenv("ANVILKIT_KNOWLEDGE_OBJECTS_CREDENTIALS_FILE"),
			"ANVILKIT_KNOWLEDGE_KUBECONFIG=" + os.Getenv("ANVILKIT_KNOWLEDGE_KUBECONFIG"),
			"ANVILKIT_KNOWLEDGE_IMAGE_REGISTRY=" + os.Getenv("ANVILKIT_KNOWLEDGE_IMAGE_REGISTRY"),
			"ANVILKIT_KNOWLEDGE_INFERENCE_URL=" + infURL,
			"ANVILKIT_KNOWLEDGE_QDRANT_URL=" + os.Getenv("ANVILKIT_KNOWLEDGE_QDRANT_URL"),
			"ANVILKIT_KNOWLEDGE_QDRANT_API_KEY=" + os.Getenv("ANVILKIT_KNOWLEDGE_QDRANT_API_KEY"),
		},
	})
	conn := dialKnowledgeAs(t, l.knowledgeAddr, principalAPI)
	sources := knowledgev1.NewSourceServiceClient(conn)
	snapshots := knowledgev1.NewSnapshotServiceClient(conn)
	retrieval := knowledgev1.NewRetrievalServiceClient(conn)
	store := newObjectStore(t, l.dir)
	migrator := os.Getenv("ANVILKIT_DEV_KNOWLEDGE_MIGRATOR_DSN")
	ctx := context.Background()

	run := strconv.FormatInt(time.Now().Unix(), 36)
	tenants := map[string]string{"a": "eval-" + run + "-a", "b": "eval-" + run + "-b"}
	const project = "proj_eval"
	scope := func(tenant, actor string) *knowledgev1.Scope {
		return &knowledgev1.Scope{TenantId: tenant, ProjectId: project, ActorId: actor}
	}
	seq := 0
	command := func(tenant, actor string) *knowledgev1.CommandIdentity {
		seq++
		id := fmt.Sprintf("p16-%s-%d", run, seq)
		return &knowledgev1.CommandIdentity{TenantId: tenant, CommandId: id, ActorId: actor, RequestDigest: sha([]byte(id))}
	}
	docOf := map[string]string{}    // source id -> doc
	sourceOf := map[string]string{} // doc -> source id
	readerCan := map[string]bool{}  // doc -> readable by tenant a's reader
	var indexSeconds []float64

	t.Run("the corpus is parsed, indexed as named dense/sparse points and accepted in the index generation", func(t *testing.T) {
		started := map[string]time.Time{}
		for _, s := range corpus.Sources {
			data, err := os.ReadFile(filepath.Join(ragRoot(t), "corpus", s.File))
			require.NoError(t, err)
			tenant := tenants[s.Tenant]
			actor := "reader"
			var access []*knowledgev1.AccessEntry
			for _, a := range s.Access {
				if a == "tenant" {
					access = append(access, &knowledgev1.AccessEntry{PrincipalType: "tenant", PrincipalId: tenant})
				} else {
					actor = strings.TrimPrefix(a, "actor:")
					access = append(access, &knowledgev1.AccessEntry{PrincipalType: "actor", PrincipalId: actor})
				}
			}
			name := run + "-" + s.File
			store.put("uploads/"+tenant+"/"+name, data)
			out, err := sources.RegisterSource(ctx, &knowledgev1.RegisterSourceRequest{
				Command: command(tenant, actor), Scope: scope(tenant, actor), Kind: knowledgev1.SourceKind_SOURCE_KIND_DOCUMENT,
				Locator: "upload:" + name, ContentDigest: sha(data), MediaType: "text/markdown", SizeBytes: strconv.Itoa(len(data)), Access: access,
			})
			require.NoError(t, err, s.Doc)
			id := out.GetSource().GetSourceId()
			docOf[id], sourceOf[s.Doc] = s.Doc, id
			readerCan[s.Doc] = s.Tenant == "a" && len(s.Access) == 1 && s.Access[0] == "tenant"
			started[id] = time.Now()
		}
		for id, begun := range started {
			l.eventually(20*time.Minute, "source "+docOf[id]+" indexed", func() bool {
				rows := l.query(migrator, "SELECT state, failure_code FROM ingest_requests WHERE source_id = $1", id)
				if len(rows) == 1 && rows[0]["state"] == "failed" {
					t.Fatalf("%s failed: %v", docOf[id], rows[0]["failure_code"])
				}
				return len(rows) == 1 && rows[0]["state"] == "indexed"
			})
			indexSeconds = append(indexSeconds, time.Since(begun).Seconds())
		}
		gen := l.query(migrator, "SELECT generation::text AS g, collection_name, model_revision, dimensions FROM index_generations WHERE state = 'accepted'")
		require.Len(t, gen, 1)
		collection := gen[0]["collection_name"].(string)
		info := qdrantGet(t, "/collections/"+collection)["result"].(map[string]any)
		params := info["config"].(map[string]any)["params"].(map[string]any)
		require.Equal(t, float64(1024), params["vectors"].(map[string]any)["dense"].(map[string]any)["size"])
		require.Contains(t, params["sparse_vectors"], "sparse")
		for field := range map[string]bool{"tenant_id": true, "project_id": true, "source_id": true, "source_key": true} {
			require.Contains(t, info["payload_schema"], field)
		}
		aliases := qdrantGet(t, "/aliases")["result"].(map[string]any)["aliases"].([]any)
		require.Contains(t, aliases, map[string]any{"alias_name": "anvilkit-knowledge", "collection_name": collection})
		for doc, id := range sourceOf {
			entry := l.query(migrator, "SELECT e.state, e.point_count, i.chunk_count FROM index_entries e JOIN ingest_requests i ON i.request_id = e.ingest_request_id WHERE e.source_id = $1 AND e.generation = $2", id, gen[0]["g"])
			require.Len(t, entry, 1, doc)
			require.Equal(t, "accepted", entry[0]["state"], doc)
			require.Equal(t, entry[0]["chunk_count"], entry[0]["point_count"], doc)
			require.Equal(t, int(entry[0]["point_count"].(int32)), qdrantCount(t, collection, "source_id", id), doc)
		}
	})

	// The design lead reads every tenant a source and freezes them all; the
	// reader searches that snapshot under the reader's own current ACL.
	var tenantA []string
	for _, s := range corpus.Sources {
		if s.Tenant == "a" {
			tenantA = append(tenantA, sourceOf[s.Doc])
		}
	}
	snap, err := snapshots.CreateSnapshot(ctx, &knowledgev1.CreateSnapshotRequest{Command: command(tenants["a"], "lead"), Scope: scope(tenants["a"], "lead"), SourceIds: tenantA})
	require.NoError(t, err)
	snapshotID := snap.GetSnapshot().GetSnapshotId()
	require.Len(t, snap.GetSnapshot().GetSourceRevisions(), len(tenantA))

	search := func(actor, query string, k int) (*knowledgev1.SearchResponse, time.Duration, error) {
		begun := time.Now()
		r, err := retrieval.Search(ctx, &knowledgev1.SearchRequest{
			Scope: scope(tenants["a"], actor), SnapshotId: snapshotID, Query: query, MaxContextItems: uint32(k),
			RetrievalProfileId: "hybrid-bge-m3-dev-v1", Deadline: timestamppb.New(time.Now().Add(60 * time.Second)),
		})
		return r, time.Since(begun), err
	}
	verifyCitation := func(item *knowledgev1.ContextItem) bool {
		c := item.GetCitation()
		rows := l.query(migrator, "SELECT source_id, source_revision::text AS rev, content_digest, locator FROM chunks WHERE chunk_id = $1", c.GetChunkId())
		return len(rows) == 1 && rows[0]["source_id"] == c.GetSourceId() && rows[0]["rev"] == c.GetSourceRevision() &&
			rows[0]["content_digest"] == c.GetContentDigest() && rows[0]["locator"] == c.GetLocator() && sha([]byte(item.GetText())) == c.GetContentDigest()
	}

	var reports []ragCaseReport
	var latencies []float64
	computeBefore := counters(t, l.knowledgeHealth, "anvilkit_knowledge_retrieval_compute_total")
	searches := 0

	t.Run("the labeled cases are scored over repeated trials", func(t *testing.T) {
		for _, c := range corpus.Cases {
			actor := c.As
			if actor == "" {
				actor = "reader"
			}
			rep := ragCaseReport{ID: c.ID, Stratum: c.Stratum, Stable: true}
			var first []string
			for trial := 1; trial <= corpus.Trials; trial++ {
				r, took, err := search(actor, c.Query, corpus.K)
				require.NoError(t, err, c.ID)
				searches++
				latencies = append(latencies, float64(took.Microseconds())/1000)
				raw, _ := json.Marshal(r)
				tr := ragTrial{Trial: trial, LatencyMs: float64(took.Microseconds()) / 1000, NoAnswer: r.GetNoAnswer(), ResponseBytes: sha(raw)}
				matched := map[int]bool{}
				dcg := 0.0
				for pos, item := range r.GetItems() {
					cit := item.GetCitation()
					doc := docOf[cit.GetSourceId()]
					tr.Citations = append(tr.Citations, cit.GetChunkId())
					if !verifyCitation(item) {
						tr.Invalid++
					}
					if actor == "reader" && !readerCan[doc] {
						tr.Disclosures++
					}
					for _, f := range c.Forbidden {
						if strings.Contains(strings.ToLower(item.GetText()), strings.ToLower(f)) {
							tr.Disclosures++
						}
					}
					for _, fd := range c.ForbiddenDocs {
						if doc == fd && pos < corpus.K {
							tr.Failures = append(tr.Failures, "injection_ranked")
						}
					}
					if pos >= corpus.K {
						continue
					}
					best, bestLabel := 0.0, -1
					for li, lb := range c.Labels {
						if !matched[li] && doc == lb.Doc && strings.Contains(strings.ToLower(item.GetText()), strings.ToLower(lb.Evidence)) && lb.Gain > best {
							best, bestLabel = lb.Gain, li
						}
					}
					if bestLabel >= 0 {
						matched[bestLabel] = true
						dcg += best / math.Log2(float64(pos+2))
					}
				}
				for _, f := range c.Forbidden {
					if strings.Contains(strings.ToLower(string(raw)), strings.ToLower(f)) && tr.Disclosures == 0 {
						tr.Disclosures++
					}
				}
				if len(c.Labels) > 0 {
					recall := float64(len(matched)) / float64(len(c.Labels))
					gains := []float64{}
					for _, lb := range c.Labels {
						gains = append(gains, lb.Gain)
					}
					sort.Sort(sort.Reverse(sort.Float64Slice(gains)))
					idcg := 0.0
					for i, g := range gains {
						if i < corpus.K {
							idcg += g / math.Log2(float64(i+2))
						}
					}
					ndcg := dcg / idcg
					tr.Recall, tr.NDCG = &recall, &ndcg
					if r.GetNoAnswer() {
						tr.Failures = append(tr.Failures, "false_no_answer")
					} else if len(matched) < len(c.Labels) {
						tr.Failures = append(tr.Failures, "missed_evidence")
					}
				} else if !r.GetNoAnswer() {
					tr.Failures = append(tr.Failures, "answer_without_evidence")
				}
				if tr.Disclosures > 0 {
					tr.Failures = append(tr.Failures, "disclosure")
				}
				if tr.Invalid > 0 {
					tr.Failures = append(tr.Failures, "invalid_citation")
				}
				if trial == 1 {
					first = tr.Citations
				} else if strings.Join(first, ",") != strings.Join(tr.Citations, ",") {
					rep.Stable = false
				}
				rep.Trials = append(rep.Trials, tr)
			}
			reports = append(reports, rep)
		}
	})

	computeAfter := counters(t, l.knowledgeHealth, "anvilkit_knowledge_retrieval_compute_total")

	t.Run("report: no unauthorized disclosure, verifiable citations, the positive control answered", func(t *testing.T) {
		type agg struct {
			Cases, Trials                         int
			Recall, NDCG                          float64
			Scored                                int
			NoAnswerCorrect, NoAnswerCases        int
			FalseNoAnswer, AnswerableTrials       int
			Disclosures, Invalid, Unstable        int
			Failures                              map[string]int
			PassAllTrials, PassAnyTrial, Answered int
		}
		byStratum := map[string]*agg{}
		total := &agg{Failures: map[string]int{}}
		for _, rep := range reports {
			a := byStratum[rep.Stratum]
			if a == nil {
				a = &agg{Failures: map[string]int{}}
				byStratum[rep.Stratum] = a
			}
			passed := 0
			for _, x := range []*agg{a, total} {
				x.Cases++
				if !rep.Stable {
					x.Unstable++
				}
			}
			for _, tr := range rep.Trials {
				ok := len(tr.Failures) == 0
				if ok {
					passed++
				}
				for _, x := range []*agg{a, total} {
					x.Trials++
					x.Disclosures += tr.Disclosures
					x.Invalid += tr.Invalid
					for _, f := range tr.Failures {
						x.Failures[f]++
					}
					if tr.Recall != nil {
						x.Recall += *tr.Recall
						x.NDCG += *tr.NDCG
						x.Scored++
						x.AnswerableTrials++
						if tr.NoAnswer {
							x.FalseNoAnswer++
						}
					} else {
						x.NoAnswerCases++
						if tr.NoAnswer {
							x.NoAnswerCorrect++
						}
					}
				}
			}
			for _, x := range []*agg{a, total} {
				if passed == len(rep.Trials) {
					x.PassAllTrials++
				}
				if passed > 0 {
					x.PassAnyTrial++
				}
			}
		}
		mean := func(sum float64, n int) *float64 {
			if n == 0 {
				return nil
			}
			v := sum / float64(n)
			return &v
		}
		summary := func(x *agg) map[string]any {
			return map[string]any{
				"cases": x.Cases, "trials": x.Trials,
				"recallAtK": mean(x.Recall, x.Scored), "ndcgAtK": mean(x.NDCG, x.Scored),
				"noAnswerCorrect": fmt.Sprintf("%d/%d", x.NoAnswerCorrect, x.NoAnswerCases),
				"falseNoAnswer":   fmt.Sprintf("%d/%d", x.FalseNoAnswer, x.AnswerableTrials),
				"passAllTrials":   fmt.Sprintf("%d/%d", x.PassAllTrials, x.Cases), "passAnyTrial": fmt.Sprintf("%d/%d", x.PassAnyTrial, x.Cases),
				"unauthorizedDisclosures": x.Disclosures, "invalidCitations": x.Invalid, "unstableCases": x.Unstable, "failureClasses": x.Failures,
			}
		}
		strata := map[string]any{}
		for k, v := range byStratum {
			strata[k] = summary(v)
		}
		compute := map[string]float64{}
		for k, v := range computeAfter {
			compute[k] = (v - computeBefore[k]) / float64(searches)
		}
		report := map[string]any{
			"schemaVersion": 1, "status": "DEVELOPMENT_ONLY", "run": run, "recordedAt": time.Now().UTC().Format(time.RFC3339),
			"corpusDigest": corpusDigest, "k": corpus.K, "trialsPerCase": corpus.Trials,
			"retrievalProfile": "hybrid-bge-m3-dev-v1", "embedding": "bge-m3-v1@5617a9f61b028005a4858fdac845db406aefb181",
			"reranker": "bge-reranker-v2-m3-v1@953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e", "vectorIndex": "qdrant/qdrant:v1.19.0",
			"total": summary(total), "strata": strata,
			"latencyMs":              map[string]float64{"p50": percentile(latencies, 0.5), "p95": percentile(latencies, 0.95), "max": percentile(latencies, 1)},
			"ingestToIndexedSeconds": map[string]float64{"p50": percentile(indexSeconds, 0.5), "max": percentile(indexSeconds, 1)},
			"computePerSearch":       compute,
			"monetaryCost":           "UNEXECUTED: no ENV-04/ENV-06 price for self-hosted inference",
			"cases":                  reports,
		}
		body, err := json.MarshalIndent(report, "", "  ")
		require.NoError(t, err)
		dir := filepath.Join(repoRoot(t), "outputs", "rag-eval")
		require.NoError(t, os.MkdirAll(dir, 0o755))
		path := filepath.Join(dir, "report-"+run+".json")
		require.NoError(t, os.WriteFile(path, body, 0o644))
		t.Logf("report %s (%s)", path, sha(body))
		s, _ := json.Marshal(summary(total))
		t.Logf("total: %s", s)
		for k, v := range strata {
			b, _ := json.Marshal(v)
			t.Logf("stratum %s: %s", k, b)
		}
		t.Logf("latency ms p50=%.1f p95=%.1f; compute per search %v", percentile(latencies, 0.5), percentile(latencies, 0.95), compute)

		require.Zero(t, total.Disclosures, "no unauthorized content, marker or count is disclosed")
		require.Zero(t, total.Invalid, "every citation verifies against Knowledge's chunk records and digests")
		// P3.4 (EVAL-04): the run meets the corpus's derived quality bounds
		// (DEVELOPMENT_ONLY; the business corpus's are an ENV-05 input).
		require.Empty(t, ragThresholdFailures(loadRAGThresholds(t), corpusDigest, ragTotals{
			Recall: mean(total.Recall, total.Scored), NDCG: mean(total.NDCG, total.Scored),
			NoAnswerCorrect: total.NoAnswerCorrect, NoAnswerCases: total.NoAnswerCases, FalseNoAnswer: total.FalseNoAnswer,
			AnswerableTrials: total.AnswerableTrials, Disclosures: total.Disclosures, Invalid: total.Invalid,
		}), "the retrieval quality bounds of tests/evals/rag/thresholds.json")
		for _, rep := range reports {
			if rep.ID == "auth-lead-positive" {
				for _, tr := range rep.Trials {
					require.False(t, tr.NoAnswer, "the authorized reader finds the private source: the negatives are authorization, not a missing index")
				}
			}
		}
	})

	// SEC-05 (P3.4): a revocation that commits while searches are running.
	// Readability is decided at read time from the current ACL, never from
	// Qdrant's payload, so no search that begins after the revocation's
	// commit returns the source; a search already reading at the commit may.
	t.Run("a revocation concurrent with searches: no search begun after its commit discloses the source", func(t *testing.T) {
		release := sourceOf["release-process"]
		const query = "Which targets does publication write before activation?"
		type observation struct {
			begun, ended time.Time
			disclosed    bool
			err          error
		}
		var mu sync.Mutex
		var seen []observation
		stop := make(chan struct{})
		var searchers sync.WaitGroup
		for range 6 {
			searchers.Add(1)
			go func() {
				defer searchers.Done()
				for {
					select {
					case <-stop:
						return
					default:
					}
					begun := time.Now()
					r, _, err := search("reader", query, 5)
					o := observation{begun: begun, ended: time.Now(), err: err}
					o.disclosed = err == nil && strings.Contains(fmt.Sprint(r.GetItems()), release)
					mu.Lock()
					seen = append(seen, o)
					mu.Unlock()
				}
			}()
		}
		time.Sleep(3 * time.Second)
		// The owner (the registering reader) replaces the ACL (P2.7: owner or
		// manage), which takes the reader's own access away.
		got, err := sources.GetSource(ctx, &knowledgev1.GetSourceRequest{Scope: scope(tenants["a"], "reader"), SourceId: release})
		require.NoError(t, err)
		_, err = sources.UpdateSourceAccess(ctx, &knowledgev1.UpdateSourceAccessRequest{
			Command: command(tenants["a"], "reader"), Scope: scope(tenants["a"], "reader"), SourceId: release,
			ExpectedAclRevision: got.GetSource().GetAclRevision(), Access: []*knowledgev1.AccessEntry{{PrincipalType: "actor", PrincipalId: "lead"}},
		})
		committed := time.Now()
		require.NoError(t, err)
		time.Sleep(3 * time.Second)
		close(stop)
		searchers.Wait()

		// The disclosure verdict over the searches that answered comes first,
		// so that a run with an unanswered search still reports it; every
		// search must answer all the same.
		before, after, unanswered := 0, 0, 0
		for _, o := range seen {
			if o.err != nil {
				unanswered++
				continue
			}
			switch {
			case o.ended.Before(committed) && o.disclosed:
				before++
			case o.begun.After(committed):
				after++
				require.False(t, o.disclosed, "a search begun %s after the revocation committed disclosed the source", o.begun.Sub(committed))
			}
		}
		t.Logf("concurrent searches: %d in total, %d unanswered, %d disclosed before the commit, %d begun after it", len(seen), unanswered, before, after)
		for _, o := range seen {
			require.NoError(t, o.err, "every concurrent search answers")
		}
		require.Positive(t, before, "the positive control: the source was readable before the revocation")
		require.Positive(t, after, "searches ran after the revocation's commit")
	})

	t.Run("a revocation applies to the frozen snapshot and a deletion to every generation", func(t *testing.T) {
		brand := sourceOf["brand-colors"]
		r, _, err := search("reader", "What is the primary brand color?", 5)
		require.NoError(t, err)
		require.Contains(t, fmt.Sprint(r.GetItems()), brand)
		// The owner replaces the ACL (P2.7: replacing it needs the owner or a
		// manage entry); the reader it names no longer reads the source.
		got, err := sources.GetSource(ctx, &knowledgev1.GetSourceRequest{Scope: scope(tenants["a"], "reader"), SourceId: brand})
		require.NoError(t, err)
		_, err = sources.UpdateSourceAccess(ctx, &knowledgev1.UpdateSourceAccessRequest{
			Command: command(tenants["a"], "reader"), Scope: scope(tenants["a"], "reader"), SourceId: brand,
			ExpectedAclRevision: got.GetSource().GetAclRevision(), Access: []*knowledgev1.AccessEntry{{PrincipalType: "actor", PrincipalId: "lead"}},
		})
		require.NoError(t, err)
		r, _, err = search("reader", "What is the primary brand color?", 5)
		require.NoError(t, err)
		require.NotContains(t, fmt.Sprint(r.GetItems()), brand)
		require.NotContains(t, strings.ToLower(fmt.Sprint(r.GetItems())), "#0f766e")

		typo := sourceOf["typography"]
		g := l.query(migrator, "SELECT collection_name FROM index_generations WHERE state <> 'retired' ORDER BY generation")
		before := 0
		for _, row := range g {
			before += qdrantCount(t, row["collection_name"].(string), "source_id", typo)
		}
		require.Positive(t, before)
		_, err = sources.DeleteSource(ctx, &knowledgev1.DeleteSourceRequest{Command: command(tenants["a"], "reader"), Scope: scope(tenants["a"], "reader"), SourceId: typo, ExpectedRevision: "1"})
		require.NoError(t, err)
		r, _, err = search("reader", "Which typefaces are used for body text and headings?", 5)
		require.NoError(t, err)
		require.NotContains(t, fmt.Sprint(r.GetItems()), typo, "readability ends in the deletion transaction")
		l.eventually(2*time.Minute, "deleted source purged from every generation", func() bool {
			n := 0
			for _, row := range g {
				n += qdrantCount(t, row["collection_name"].(string), "source_id", typo)
			}
			rows := l.query(migrator, "SELECT points_purged_at IS NOT NULL AS purged FROM sources WHERE source_id = $1", typo)
			return n == 0 && len(rows) == 1 && rows[0]["purged"] == true
		})
	})
}
