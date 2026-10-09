#!/usr/bin/env python3
"""Install the owned local kind secret platform; never print private material."""
import json
import os
import pathlib
import subprocess
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[3]
HERE = pathlib.Path(__file__).resolve().parent
STATE = ROOT / '.local/openbao'
OUT = ROOT / 'outputs/repairs-f01-f12'
CONTEXT = 'kind-anvilkit-dev'
NS = 'anvilkit-secrets'
OWNER = 'repairs-f01-f12'
K = [str(ROOT / '.local/bin/kubectl'), '--context', CONTEXT]
H = [str(ROOT / '.local/bin/helm'), '--kube-context', CONTEXT]
os.umask(0o077)
STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)


def apply(obj):
    data = yaml.safe_dump(obj).encode()
    subprocess.run(K + ['apply', '--dry-run=server', '--validate=strict', '-f', '-'], input=data, check=True)
    subprocess.run(K + ['apply', '-f', '-'], input=data, check=True)


def namespace(name):
    raw = subprocess.check_output(K + ['get', 'namespace', name, '--ignore-not-found', '-o', 'json'])
    if raw and json.loads(raw)['metadata'].get('labels', {}).get('anvilkit.io/owner') != OWNER:
        raise SystemExit('Refusing unowned namespace: ' + name)
    apply({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {
        'name': name, 'labels': {'anvilkit.io/owner': OWNER}}})


namespace(NS)
namespace('anvilkit-verification')
files = [STATE / name for name in ['ca.crt', 'ca.key', 'tls.crt', 'tls.key']]
if any(p.exists() for p in files) and not all(p.exists() for p in files):
    raise SystemExit('Incomplete TLS material: preserve and inspect it before continuing')
if not all(p.exists() for p in files):
    commands = [
        ['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-days', '365', '-subj', '/CN=AnvilKit Local OpenBao CA', '-keyout', 'ca.key', '-out', 'ca.crt'],
        ['openssl', 'req', '-newkey', 'rsa:3072', '-nodes', '-subj', '/CN=openbao.anvilkit-secrets.svc', '-keyout', 'tls.key', '-out', 'tls.csr'],
    ]
    (STATE / 'tls.ext').write_text('subjectAltName=DNS:openbao,DNS:openbao.anvilkit-secrets.svc,DNS:openbao.anvilkit-secrets.svc.cluster.local,DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n')
    commands.append(['openssl', 'x509', '-req', '-in', 'tls.csr', '-CA', 'ca.crt', '-CAkey', 'ca.key', '-CAcreateserial', '-days', '90', '-extfile', 'tls.ext', '-out', 'tls.crt'])
    for command in commands:
        subprocess.run(command, cwd=STATE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
apply({'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque', 'metadata': {
    'name': 'openbao-tls', 'namespace': NS, 'labels': {'anvilkit.io/owner': OWNER}},
    'stringData': {name: (STATE / name).read_text() for name in ['ca.crt', 'tls.crt', 'tls.key']}})
apply({'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'openbao-ca', 'namespace': NS},
       'data': {'ca.crt': (STATE / 'ca.crt').read_text()}})
for name, url in [('openbao', 'https://openbao.github.io/openbao-helm'),
                  ('secrets-store-csi-driver', 'https://kubernetes-sigs.github.io/secrets-store-csi-driver/charts')]:
    subprocess.run(H + ['repo', 'add', name, url], check=True)
for name, chart, version, values in [
    ('secrets-store-csi-driver', 'secrets-store-csi-driver/secrets-store-csi-driver', '1.6.1', 'csi-values.yaml'),
    ('openbao', 'openbao/openbao', '0.29.5', 'values.yaml'),
]:
    args = [name, chart, '--version', version, '-n', NS, '-f', str(HERE / values)]
    rendered = subprocess.check_output(H + ['template'] + args)
    (OUT / (name + '-rendered-final.yaml')).write_bytes(rendered)
    subprocess.run(K + ['apply', '--dry-run=server', '--validate=strict', '-f', '-'], input=rendered, check=True)
    # A new OpenBao is sealed until the separate bootstrap step; do not wait for readiness here.
    subprocess.run(H + ['upgrade', '--install'] + args, check=True)
subprocess.run(K + ['apply', '--dry-run=server', '--validate=strict', '-f', str(HERE / 'workload.yaml')], check=True)
subprocess.run(K + ['apply', '-f', str(HERE / 'workload.yaml')], check=True)
print('Platform installed. Start the documented localhost TLS port-forward, then run bootstrap.py. No provider credential was created.')
