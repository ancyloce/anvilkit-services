#!/usr/bin/env python3
"""Record final Helm validation and actual owned RKE2 workload topology."""
import json
import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / os.environ.get('ANVILKIT_VERIFICATION_OUTPUT', 'outputs/repairs-f01-f12')
OUT.mkdir(parents=True, exist_ok=True)
K = [str(ROOT / '.local/bin/kubectl'), '--kubeconfig', str(ROOT / '.local/repair-rke2/kubeconfig.yaml')]
H = [str(ROOT / '.local/bin/helm'), '--kubeconfig', str(ROOT / '.local/repair-rke2/kubeconfig.yaml')]
NS = 'anvilkit-verification'
for service in ['api', 'control', 'workflow', 'model-proxy', 'knowledge', 'mcp', 'background-worker']:
    chart = ROOT / 'services/agent' / service / 'deploy/chart'
    values = ROOT / 'deploy/verification/values' / (service + '.yaml')
    with (OUT / (service + '-helm-validation-final.log')).open('wb') as log:
        subprocess.run(H + ['lint', str(chart), '-f', str(values)], check=True, stdout=log, stderr=subprocess.STDOUT)
        rendered = subprocess.check_output(H + ['template', 'anvilkit-agent-' + service, str(chart), '-n', NS,
            '-f', str(values), '--post-renderer', str(ROOT / 'deploy/verification/post-render.sh')])
        (OUT / (service + '-rendered.yaml')).write_bytes(rendered)
        subprocess.run(K + ['apply', '--dry-run=server', '--validate=strict', '-f', '-'], input=rendered,
                       check=True, stdout=log, stderr=subprocess.STDOUT)
    print(service + ': Helm lint, render, strict server-side dry run PASS', flush=True)
for name, args in [('rke2-nodes.json', ['get', 'nodes', '-o', 'json']),
                   ('rke2-pods.json', ['get', 'pods', '-A', '-o', 'json']),
                   ('rke2-hpa.json', ['get', 'hpa', '-n', NS, '-o', 'json']),
                   ('rke2-deployments.json', ['get', 'deploy', '-n', NS, '-o', 'json'])]:
    (OUT / name).write_bytes(subprocess.check_output(K + args))
(OUT / 'rke2-helm-releases.json').write_bytes(subprocess.check_output(H + ['list', '-A', '-o', 'json']))
pods = json.loads((OUT / 'rke2-pods.json').read_text())['items']
assert not json.loads((OUT / 'rke2-hpa.json').read_text())['items'], 'Unexpected HPA'
deployments = {d['metadata']['name']: d for d in json.loads((OUT / 'rke2-deployments.json').read_text())['items']}
for service in ['api', 'control', 'workflow', 'model-proxy', 'knowledge', 'mcp', 'background-worker']:
    deployment = deployments['anvilkit-agent-' + service]
    assert deployment['spec']['replicas'] == 1 and deployment['spec']['strategy']['type'] == 'Recreate'
    assert deployment['status'].get('readyReplicas') == 1 and deployment['status'].get('updatedReplicas') == 1
    assert deployment['status']['observedGeneration'] == deployment['metadata']['generation']
counts = {'business': 0, 'temporary_jobs': 0, 'verification_dependencies': 0, 'cluster_platform': 0}
for pod in pods:
    namespace = pod['metadata']['namespace']
    if any(o['kind'] == 'Job' for o in pod['metadata'].get('ownerReferences', [])):
        counts['temporary_jobs'] += 1
    elif namespace == NS and pod['metadata']['name'].startswith('anvilkit-agent-'):
        counts['business'] += 1
        spec = pod['spec']
        assert len(spec['containers']) == 1 and not spec.get('initContainers') and not spec.get('ephemeralContainers')
        assert all(c['ready'] for c in pod['status']['containerStatuses'])
        container = spec['containers'][0]
        assert '@sha256:' in container['image']
        for probe in ['readinessProbe', 'livenessProbe'] + (['startupProbe'] if 'startupProbe' in container else []):
            config = container[probe]
            if 'httpGet' in config:
                assert config['httpGet']['path'] in ['/healthz', '/readyz'], (pod['metadata']['name'], probe)
            else:
                assert container['name'] == 'control' and config['grpc']['port'] == 9101
        assert pod['status']['phase'] == 'Running'
    elif namespace in [NS, 'anvilkit-verification-platform']:
        counts['verification_dependencies'] += 1
    else:
        counts['cluster_platform'] += 1
assert counts['business'] == 7, counts
(OUT / 'rke2-pod-counts.json').write_text(json.dumps(counts, indent=2))
print(json.dumps(counts))
