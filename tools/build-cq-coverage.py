#!/usr/bin/env python3
"""Build contracts/qualification/cq-coverage-v1.json from the service views' CQ coverage tables (added 2026-09-10).

Each service view (docs/design/0001–0008) carries a table whose rows are
`| CQ-nn | assertion this design makes testable | artifact or fixture that carries it |`.
This tool harvests those rows into one machine-readable index so that tools/check-contracts.py can
prove that every CQ-01..CQ-26 is covered by at least one substantive row naming an existing artifact.
The index is derived data; the tables in the documents remain the authored source.
"""
from __future__ import annotations
import json, pathlib, re, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS = [f"docs/design/000{i}-anvilkit-{n}-detailed-design-0909-1927.md" for i, n in enumerate(["agent-api","agent-control","agent-workflow","agent-model-proxy","component-codegen","component-validator","component-preview","job-access-proxy"], start=1)]
def main() -> int:
    q = (ROOT / "docs/architecture/plans/qualification.md").read_text(encoding="utf-8")
    known = sorted(set(re.findall(r"\| (CQ-\d\d) \|", q)))
    entries = []
    for doc in DOCS:
        text = (ROOT / doc).read_text(encoding="utf-8")
        for m in re.finditer(r"\|\s*(CQ-\d\d)\s*\|([^\n]*)\|([^\n]*)\|", text):
            cq, assertion, artifact = m.group(1), m.group(2).strip(), m.group(3).strip()
            paths = re.findall(r"`((?:contracts|tools|docs)/[^`]+)`", artifact)
            entries.append({"cq": cq, "document": doc, "assertion": assertion, "artifacts": paths})
    uncovered = [c for c in known if not any(e["cq"] == c for e in entries)]
    out = {"schemaVersion": 1, "recordId": "cq-coverage-2026-09-10", "source": "docs/design/0001–0008 §13 coverage tables; derived by tools/build-cq-coverage.py",
           "knownCq": known, "uncovered": uncovered, "entries": entries}
    (ROOT / "contracts/qualification/cq-coverage-v1.json").write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(entries)} rows; uncovered: {uncovered}")
    return 0
if __name__ == "__main__": sys.exit(main())
