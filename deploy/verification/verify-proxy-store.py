#!/usr/bin/env python3
"""Run Proxy tests against the owned RKE2 MinIO using an owned IPv4 forward."""
import json
import os
import pathlib
import socket
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATE = ROOT / '.local/repair-rke2'
K = [str(ROOT / '.local/bin/kubectl'), '--kubeconfig', str(STATE / 'kubeconfig.yaml'), '-n', 'anvilkit-verification']
namespace = json.loads(subprocess.check_output(K + ['get', 'namespace', 'anvilkit-verification', '-o', 'json']))
assert namespace['metadata']['labels']['anvilkit.io/owner'] == 'repairs-f01-f12'
with socket.socket() as reservation:
    reservation.bind(('127.0.0.1', 0))
    port = reservation.getsockname()[1]
forward = subprocess.Popen(K + ['port-forward', '--address', '127.0.0.1', 'svc/minio', f'{port}:9000'],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    # A bind collision must fail; never fall back to an IPv6-only listener
    # while the test contacts a pre-existing service on IPv4.
    assert forward.stdout.readline().strip() == f'Forwarding from 127.0.0.1:{port} -> 9000', 'port-forward did not bind owned endpoint'
    credentials = json.loads((STATE / 'dependencies.json').read_text())
    env = os.environ.copy()
    env.update({'ANVILKIT_DEV_ARTIFACTS_ENDPOINT': f'http://127.0.0.1:{port}',
                'ANVILKIT_DEV_MODEL_PROXY_BUCKET': 'anvilkit-model-proxy',
                'ANVILKIT_DEV_MODEL_PROXY_ACCESS_KEY_ID': 'verification-proxy',
                'ANVILKIT_DEV_MODEL_PROXY_SECRET_ACCESS_KEY': credentials['proxy_password']})
    result = subprocess.run(['pnpm', 'test'], cwd=ROOT / 'services/agent/model-proxy', env=env)
finally:
    forward.terminate()
    forward.wait(timeout=10)
raise SystemExit(result.returncode)
