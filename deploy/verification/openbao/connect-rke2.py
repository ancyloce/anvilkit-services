#!/usr/bin/env python3
"""Connect the owned RKE2 CSI platform to the existing initialized kind Bao.

The TLS tunnel must already listen on 10.203.0.1:18200. This installs no
OpenBao server and never initializes, copies or prints a provider credential.
"""
import base64
import json
import os
import pathlib
import subprocess
import time

import yaml

from bootstrap import ROOT, STATE, request

HERE = pathlib.Path(__file__).resolve().parent
OUT = ROOT / 'outputs/repairs-f05-f12-followup'
NS = 'anvilkit-verification-platform'
OWNER = 'repairs-f01-f12'
K = [str(ROOT / '.local/bin/kubectl'), '--kubeconfig', str(ROOT / '.local/repair-rke2/kubeconfig.yaml')]
H = [str(ROOT / '.local/bin/helm'), '--kubeconfig', str(ROOT / '.local/repair-rke2/kubeconfig.yaml')]


def apply(obj):
    metadata = obj['metadata']
    args = K + (['-n', metadata['namespace']] if 'namespace' in metadata else [])
    prior = subprocess.check_output(args + ['get', obj['kind'], metadata['name'], '--ignore-not-found', '-o', 'json'])
    if prior and json.loads(prior)['metadata'].get('labels', {}).get('anvilkit.io/owner') != OWNER:
        raise SystemExit('Unowned object: ' + metadata['name'])
    raw = yaml.safe_dump(obj).encode()
    subprocess.run(K + ['apply', '--dry-run=server', '--validate=strict', '-f', '-'], input=raw, check=True)
    subprocess.run(K + ['apply', '-f', '-'], input=raw, check=True)


def main():
    os.umask(0o077)
    OUT.mkdir(exist_ok=True, parents=True)
    for namespace in [NS, 'anvilkit-verification']:
        obj = json.loads(subprocess.check_output(K + ['get', 'namespace', namespace, '-o', 'json']))
        if obj['metadata'].get('labels', {}).get('anvilkit.io/owner') != OWNER:
            raise SystemExit('Unowned namespace: ' + namespace)
    status = request('sys/seal-status')
    if not status['initialized'] or status['sealed']:
        raise SystemExit('Restore the existing initialized OpenBao before connecting RKE2')
    token = json.loads((STATE / 'init.json').read_text())['root_token']
    reviewer = 'openbao-rke2-reviewer'
    meta = {'name': reviewer, 'namespace': NS, 'labels': {'anvilkit.io/owner': OWNER}}
    apply({'apiVersion': 'v1', 'kind': 'ServiceAccount', 'metadata': meta, 'automountServiceAccountToken': False})
    cluster_meta = {'name': reviewer, 'labels': {'anvilkit.io/owner': OWNER}}
    apply({'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'ClusterRole', 'metadata': cluster_meta,
           'rules': [{'apiGroups': ['authentication.k8s.io'], 'resources': ['tokenreviews'], 'verbs': ['create']}]})
    apply({'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'ClusterRoleBinding', 'metadata': cluster_meta,
           'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': reviewer},
           'subjects': [{'kind': 'ServiceAccount', 'name': reviewer, 'namespace': NS}]})
    # Local cross-cluster reviewer only: a revocable dedicated SA credential,
    # with TokenReview/create as its sole permission. No business Pod mounts it.
    apply({'apiVersion': 'v1', 'kind': 'Secret', 'type': 'kubernetes.io/service-account-token',
           'metadata': {**meta, 'annotations': {'kubernetes.io/service-account.name': reviewer}}})
    for _ in range(30):
        secret = json.loads(subprocess.check_output(K + ['-n', NS, 'get', 'secret', reviewer, '-o', 'json']))
        if secret.get('data', {}).get('token'):
            break
        time.sleep(1)
    else:
        raise SystemExit('Reviewer token not issued')
    mount = 'kubernetes-rke2'
    auths = request('sys/auth', token=token)
    if mount + '/' not in auths:
        request('sys/auth/' + mount, {'type': 'kubernetes', 'description': OWNER}, token)
    elif auths[mount + '/'].get('description') != OWNER:
        raise SystemExit('Unowned OpenBao auth mount')
    request('auth/' + mount + '/config', {
        'kubernetes_host': 'https://10.203.0.11:6443',
        'kubernetes_ca_cert': base64.b64decode(secret['data']['ca.crt']).decode(),
        'token_reviewer_jwt': base64.b64decode(secret['data']['token']).decode(),
        'disable_local_ca_jwt': True,
    }, token)
    request('auth/' + mount + '/role/model-proxy-deepseek', {
        'bound_service_account_names': ['anvilkit-agent-model-proxy'],
        'bound_service_account_namespaces': ['anvilkit-verification'],
        'audience': 'openbao', 'token_policies': ['model-proxy-deepseek'],
        'token_ttl': '5m', 'token_max_ttl': '10m', 'token_no_default_policy': True,
    }, token)
    apply({'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {
        'name': 'openbao-ca', 'namespace': NS, 'labels': {'anvilkit.io/owner': OWNER}},
        'data': {'ca.crt': (STATE / 'ca.crt').read_text()}})
    for release, chart, version, values in [
        ('secrets-store-csi-driver', 'secrets-store-csi-driver/secrets-store-csi-driver', '1.6.1', 'csi-values.yaml'),
        ('openbao-csi', 'openbao/openbao', '0.29.5', 'rke2-provider-values.yaml'),
    ]:
        args = [release, chart, '--version', version, '-n', NS, '-f', str(HERE / values)]
        if release == 'secrets-store-csi-driver':
            args += ['--set', 'linux.nodeSelector.anvilkit\\.io/workload=application']
        rendered = subprocess.check_output(H + ['template'] + args + ['--include-crds'])
        (OUT / (release + '-rke2-rendered.yaml')).write_bytes(rendered)
        subprocess.run(K + ['apply', '--dry-run=server', '--validate=strict', '-f', '-'], input=rendered, check=True)
        subprocess.run(H + ['upgrade', '--install'] + args + ['--wait', '--timeout', '3m'], check=True)
    apply(yaml.safe_load((HERE / 'rke2-workload.yaml').read_text()))
    print('PASS: RKE2 auth and CSI configured against existing TLS OpenBao; provider credential unchanged.')


if __name__ == '__main__':
    main()
