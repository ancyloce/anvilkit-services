#!/bin/sh
# Offline checks of the anvilkit-observability chart (P3.6): the rules render
# (development thresholds), promtool accepts them and its unit tests fire every
# alert on its injected condition; a production render without a reviewed
# thresholds source is refused; amtool routes each severity of the Alertmanager
# configuration of an environment (ANVILKIT_ALERTMANAGER_CONFIG) to a receiver.
set -eu
CHART=$(cd "$(dirname "$0")/.." && pwd)
ROOT=$(cd "$CHART/../../../.." && pwd)
HELM=${ANVILKIT_HELM:-$ROOT/.local/bin/helm}
PY=${ANVILKIT_PYTHON_ENV:-$ROOT/.local/verification-venv/bin/python}
PROMETHEUS=${ANVILKIT_PROMETHEUS_IMAGE:-prom/prometheus@sha256:5ce7540c3c00ef4ab0c9d2c995c6a5b9c421f44b4a115d97a2c7af3b1c21cbb0}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
"$HELM" template anvilkit-observability "$CHART" --set development=true > "$WORK/render.yaml"
"$PY" - "$WORK/render.yaml" "$WORK/rules.yaml" <<'PY'
import sys, yaml
rule = next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d["kind"] == "PrometheusRule")
open(sys.argv[2], "w").write(yaml.safe_dump({"groups": rule["spec"]["groups"]}, sort_keys=False))
PY
cp "$CHART/tests/rules.test.yaml" "$WORK/"
# The Prometheus image runs as nobody: the scratch directory must be readable.
chmod 755 "$WORK" && chmod 644 "$WORK"/*.yaml
docker run --rm -v "$WORK:/w" --entrypoint promtool "$PROMETHEUS" check rules /w/rules.yaml
docker run --rm -v "$WORK:/w" -w /w --entrypoint promtool "$PROMETHEUS" test rules /w/rules.test.yaml
# The routing: the rendered AlertmanagerConfig as Alertmanager configuration,
# then amtool names the receiver each severity reaches.
"$PY" - "$WORK/render.yaml" "$WORK/alertmanager.yml" <<'PY'
import sys, yaml
cfg = next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d["kind"] == "AlertmanagerConfig")["spec"]
route = cfg["route"]
out = {"route": {"receiver": route["receiver"], "group_by": route["groupBy"],
                 "routes": [{"receiver": r["receiver"], "matchers": [f'{m["name"]}{m["matchType"]}"{m["value"]}"' for m in r["matchers"]]} for r in route["routes"]]},
       "receivers": [{"name": r["name"]} for r in cfg["receivers"]]}
open(sys.argv[2], "w").write(yaml.safe_dump(out, sort_keys=False))
PY
chmod 644 "$WORK/alertmanager.yml"
ALERTMANAGER=${ANVILKIT_ALERTMANAGER_IMAGE:-prom/alertmanager:v0.34.1@sha256:e9733bafb1bdef9b00e25a21f8f99dc26a22224bf16641ad754d1649f4c3357a}
for case in "severity=critical anvilkit-development-critical" "severity=warning anvilkit-development"; do
  set -- $case
  got=$(docker run --rm -v "$WORK:/w" --entrypoint amtool "$ALERTMANAGER" config routes test --config.file=/w/alertmanager.yml "$1" alertname=AnvilKitProbe)
  if [ "$got" = "$2" ]; then echo "PASS route $1 -> $got"; else echo "FAIL route $1 -> $got (expected $2)"; exit 1; fi
done
# Provisioning: every anvilkit_ metric a rule or a dashboard reads is one a
# service registers (its name appears in the services' sources), so a renamed
# or missing metric fails here, not as an empty panel.
"$PY" - "$WORK/render.yaml" "$ROOT" <<'PY'
import pathlib, re, sys
text = open(sys.argv[1]).read()
root = pathlib.Path(sys.argv[2])
names = {re.sub(r"_(bucket|sum|count)$", "", m) for m in re.findall(r"\banvilkit_[a-z0-9_]+", text)}
sources = ""
for d in ("services/agent",):
    for f in (root / d).rglob("*"):
        if f.suffix in (".go", ".ts", ".py") and "node_modules" not in f.parts and "dist" not in f.parts and not f.name.endswith("_test.go") and ".test." not in f.name:
            sources += f.read_text(errors="replace")
missing = sorted(n for n in names if n not in sources)
for n in missing:
    print(f"FAIL metric {n} is queried but registered by no service")
print(f"provisioning: {len(names)} metrics queried by the rules and dashboards, {len(missing)} unregistered")
sys.exit(1 if missing else 0)
PY
if "$HELM" template anvilkit-observability "$CHART" > /dev/null 2>"$WORK/refusal.txt"; then
  echo "FAIL a production render without thresholdsSource was accepted"; exit 1
fi
grep -q "thresholdsSource is required" "$WORK/refusal.txt" && echo "PASS production render without a reviewed thresholds source refused"
echo "observability chart checks: PASS"
