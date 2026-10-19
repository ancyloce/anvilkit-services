//go:build integration

package integration

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"
)

// ragThresholds are the quality bounds of tests/evals/rag/thresholds.json
// (P3.4, EVAL-04): DEVELOPMENT_ONLY regression bounds derived from repeated
// development runs of one corpus digest; the business corpus's thresholds
// are an ENV-05 input.
type ragThresholds struct {
	Status                  string  `json:"status"`
	CorpusDigest            string  `json:"corpusDigest"`
	RecallAtKMin            float64 `json:"recallAtKMin"`
	NDCGAtKMin              float64 `json:"ndcgAtKMin"`
	NoAnswerCorrectRatioMin float64 `json:"noAnswerCorrectRatioMin"`
	FalseNoAnswerRatioMax   float64 `json:"falseNoAnswerRatioMax"`
	UnauthorizedMax         int     `json:"unauthorizedDisclosuresMax"`
	InvalidCitationsMax     int     `json:"invalidCitationsMax"`
}

// ragTotals is what one evaluation run measured over the whole corpus.
type ragTotals struct {
	Recall, NDCG                    *float64
	NoAnswerCorrect, NoAnswerCases  int
	FalseNoAnswer, AnswerableTrials int
	Disclosures, Invalid            int
}

func loadRAGThresholds(t *testing.T) ragThresholds {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(ragRoot(t), "thresholds.json"))
	require.NoError(t, err)
	var th ragThresholds
	require.NoError(t, json.Unmarshal(raw, &th))
	return th
}

// ragThresholdFailures names every bound a run missed; a corpus other than
// the one the bounds were derived from refuses them all.
func ragThresholdFailures(th ragThresholds, corpusDigest string, x ragTotals) []string {
	if corpusDigest != th.CorpusDigest {
		return []string{fmt.Sprintf("the thresholds were derived for corpus %s, this run evaluated %s", th.CorpusDigest, corpusDigest)}
	}
	var out []string
	ratio := func(a, b int) float64 {
		if b == 0 {
			return 0
		}
		return float64(a) / float64(b)
	}
	if x.Recall == nil || *x.Recall < th.RecallAtKMin {
		out = append(out, fmt.Sprintf("recall@k %v below %.3f", deref(x.Recall), th.RecallAtKMin))
	}
	if x.NDCG == nil || *x.NDCG < th.NDCGAtKMin {
		out = append(out, fmt.Sprintf("nDCG@k %v below %.3f", deref(x.NDCG), th.NDCGAtKMin))
	}
	if x.NoAnswerCases > 0 && ratio(x.NoAnswerCorrect, x.NoAnswerCases) < th.NoAnswerCorrectRatioMin {
		out = append(out, fmt.Sprintf("no-answer %d/%d below %.2f", x.NoAnswerCorrect, x.NoAnswerCases, th.NoAnswerCorrectRatioMin))
	}
	if ratio(x.FalseNoAnswer, x.AnswerableTrials) > th.FalseNoAnswerRatioMax {
		out = append(out, fmt.Sprintf("false no-answer %d/%d above %.2f", x.FalseNoAnswer, x.AnswerableTrials, th.FalseNoAnswerRatioMax))
	}
	if x.Disclosures > th.UnauthorizedMax {
		out = append(out, fmt.Sprintf("%d unauthorized disclosures", x.Disclosures))
	}
	if x.Invalid > th.InvalidCitationsMax {
		out = append(out, fmt.Sprintf("%d invalid citations", x.Invalid))
	}
	return out
}

func deref(f *float64) any {
	if f == nil {
		return "none"
	}
	return fmt.Sprintf("%.3f", *f)
}

// TestRAGThresholdsRefuseADegradedRun proves the bounds' check on the
// recorded development measurements and on degraded ones (P3.4 AC6: a
// recall drop below the bound fails); it needs no environment. The live
// degraded-index run belongs to TestRetrieval on the foundation.
func TestRAGThresholdsRefuseADegradedRun(t *testing.T) {
	th := loadRAGThresholds(t)
	recall, ndcg := 0.818, 0.848
	recorded := ragTotals{Recall: &recall, NDCG: &ndcg, NoAnswerCorrect: 15, NoAnswerCases: 15, FalseNoAnswer: 9, AnswerableTrials: 66}
	t.Run("the recorded development measurements meet the bounds", func(t *testing.T) {
		require.Empty(t, ragThresholdFailures(th, th.CorpusDigest, recorded))
	})
	t.Run("a degraded index whose recall drops below the bound fails", func(t *testing.T) {
		low, lowNDCG := 0.6, 0.65
		degraded := recorded
		degraded.Recall, degraded.NDCG = &low, &lowNDCG
		failures := ragThresholdFailures(th, th.CorpusDigest, degraded)
		require.Len(t, failures, 2)
		require.Contains(t, failures[0], "recall@k")
	})
	t.Run("a disclosure or an invalid citation fails whatever the recall", func(t *testing.T) {
		leaky := recorded
		leaky.Disclosures, leaky.Invalid = 1, 1
		require.Len(t, ragThresholdFailures(th, th.CorpusDigest, leaky), 2)
	})
	t.Run("another corpus refuses the bounds", func(t *testing.T) {
		require.Len(t, ragThresholdFailures(th, "sha256:"+fmt.Sprintf("%064d", 0), recorded), 1)
	})
}
