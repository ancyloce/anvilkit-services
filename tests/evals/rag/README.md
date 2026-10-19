# RAG evaluation corpus (P16, DEVELOPMENT_ONLY)

A small labeled corpus and its cases for the hybrid retrieval of Knowledge
([delivery.md P16](../../../docs/architecture/delivery.md), [DD-07 §4](../../../docs/architecture/knowledge.md#knowledge)).
It was written by the implementing agent, not by a business owner. It exercises
the evaluator end to end on the real pipeline and produces comparable figures,
but it is **not** the approved business-labeled corpus that G-08 requires, and
no quality threshold is derived from it.

| Path | Content |
|---|---|
| `corpus/*.md` | Ten Markdown documents: component styling and structure, accessibility, release, one Chinese document, one document carrying a prompt injection, one actor-restricted document and one document of a second tenant |
| `cases.json` | The sources (tenant, trusted ACL) and 27 cases in strata: component styling/structure, accessibility, release, Chinese, unanswerable, malicious source and authorization (a positive control reads the restricted document as its authorized actor) |

Each label names a document and an evidence phrase with a graded gain; a
returned item matches when it comes from that document and contains the phrase.
Per case and trial the evaluator records Recall@k and nDCG@k (k = 5), no-answer
behavior, unauthorized disclosures (a returned source the reader cannot read or a
forbidden marker anywhere in the answer), citations that do not verify against
Knowledge's chunk records and digests, latency, and the failure classes
`missed_evidence`, `false_no_answer`, `answer_without_evidence`,
`injection_ranked`, `disclosure` and `invalid_citation`. Every case runs three
trials; the report keeps every trial (pass on all trials and on any trial, and
whether the citations were identical across trials) instead of the best run.

Run it on the development foundation (`sh deploy/dev/up.sh`, the parser image in
the kind registry, `anvilkit-agent-inference:dev` built, Knowledge and the
Background Worker built with `pnpm build`):

```sh
. .local/dev/env.sh
cd tests/integration && go test -tags integration -count=1 -timeout 60m -run TestRetrieval -v ./...
```

The report is written to `outputs/rag-eval/report-<run>.json` (ignored by Git)
with the corpus digest, the retrieval profile, the model revisions, latency
percentiles and compute per search. Monetary cost is not reported: self-hosted
inference has no ENV-04/ENV-06 price.
