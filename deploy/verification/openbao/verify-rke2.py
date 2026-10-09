#!/usr/bin/env python3
"""Verify RKE2 identity binding and read-only CSI without printing secret bytes."""
import json
import subprocess
import uuid

import yaml

from bootstrap import ROOT, STATE, request

K = [str(ROOT / '.local/bin/kubectl'), '--kubeconfig', str(ROOT / '.local/repair-rke2/kubeconfig.yaml'),
     '-n', 'anvilkit-verification']
MOUNT = 'auth/kubernetes-rke2/'
ROLE = 'model-proxy-deepseek'
SECRET = 'kv/data/anvilkit/local/model-proxy/deepseek'
OWNER = 'repairs-f01-f12'


def jwt(service, audience='openbao'):
    return subprocess.check_output(K + ['create', 'token', service, '--audience=' + audience, '--duration=10m'], text=True).strip()


def main():
    root = json.loads((STATE / 'init.json').read_text())['root_token']
    for service in ['default'] + ['anvilkit-agent-' + name for name in ['api', 'control', 'workflow', 'knowledge', 'mcp', 'background-worker']]:
        try:
            request(MOUNT + 'login', {'role': ROLE, 'jwt': jwt(service)})
        except RuntimeError as error:
            if 'HTTP 403' not in str(error):
                raise
        else:
            raise SystemExit('FAIL: foreign identity accepted: ' + service)
    print('PASS: default and all six other business identities denied')
    try:
        request(MOUNT + 'login', {'role': ROLE, 'jwt': jwt('anvilkit-agent-model-proxy', 'wrong-audience')})
    except RuntimeError as error:
        if 'HTTP 403' not in str(error):
            raise
    else:
        raise SystemExit('FAIL: wrong audience accepted')
    auth = request(MOUNT + 'login', {'role': ROLE, 'jwt': jwt('anvilkit-agent-model-proxy')})['auth']
    try:
        assert auth['policies'] == [ROLE]
        cap = request('sys/capabilities', {'token': auth['client_token'], 'paths': [SECRET, 'kv/data/another']}, root)
        assert cap[SECRET] == ['read'] and cap['kv/data/another'] == ['deny']
        try:
            data = request(SECRET, token=auth['client_token'])
            present = bool(data.get('data', {}).get('data', {}).get('api_key'))
            del data
        except RuntimeError as error:
            if 'HTTP 404' not in str(error):
                raise
            present = False
        print('PASS: Model Proxy authenticates; wrong audience denied; exact read-only path; DeepSeek api_key configured=' + str(present))
    finally:
        request('auth/token/revoke', {'token': auth['client_token']}, root)

    # An isolated fixture proves CSI transport even when the real key is absent.
    name = 'csi-probe-' + uuid.uuid4().hex[:12]
    path = 'anvilkit/verification/' + name
    meta = {'name': name, 'namespace': 'anvilkit-verification', 'labels': {'anvilkit.io/owner': OWNER}}
    objects = []
    try:
        request('kv/data/' + path, {'data': {'probe': 'non-provider-fixture'}}, root)
        request('sys/policies/acl/' + name, {'policy': 'path "kv/data/' + path + '" { capabilities = ["read"] }'}, root)
        request(MOUNT + 'role/' + name, {
            'bound_service_account_names': ['anvilkit-agent-model-proxy'], 'bound_service_account_namespaces': ['anvilkit-verification'],
            'audience': 'openbao', 'token_policies': [name], 'token_ttl': '1m', 'token_no_default_policy': True}, root)
        spc = yaml.safe_load((ROOT / 'deploy/verification/openbao/rke2-workload.yaml').read_text())
        spc['metadata'] = meta
        spc['spec']['parameters'].update({'roleName': name, 'objects': yaml.safe_dump([{
            'objectName': 'probe', 'secretPath': 'kv/data/' + path, 'secretKey': 'probe', 'filePermission': 0o444}])})
        job = {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': meta, 'spec': {
            'backoffLimit': 0, 'activeDeadlineSeconds': 90, 'template': {'metadata': {'labels': meta['labels']}, 'spec': {
                'restartPolicy': 'Never', 'serviceAccountName': 'anvilkit-agent-model-proxy', 'automountServiceAccountToken': False,
                'nodeSelector': {'anvilkit.io/workload': 'application'},
                'securityContext': {'runAsUser': 65532, 'runAsGroup': 65532, 'fsGroup': 65532},
                'containers': [{'name': 'probe', 'image': 'alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b',
                    'command': ['sh', '-c', 'test "$(cat /credential/probe)" = non-provider-fixture && ! touch /credential/write 2>/dev/null && echo CSI_READ_ONLY_PASS'],
                    'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}},
                    'resources': {'requests': {'cpu': '10m', 'memory': '8Mi'}, 'limits': {'cpu': '100m', 'memory': '32Mi'}},
                    'volumeMounts': [{'name': 'credential', 'mountPath': '/credential', 'readOnly': True}]}],
                'volumes': [{'name': 'credential', 'csi': {'driver': 'secrets-store.csi.k8s.io', 'readOnly': True,
                    'volumeAttributes': {'secretProviderClass': name}}}]}}}}
        for obj in [spc, job]:
            raw = yaml.safe_dump(obj).encode()
            subprocess.run(K + ['create', '--dry-run=server', '--validate=strict', '-f', '-'], input=raw, check=True)
            created = json.loads(subprocess.check_output(K + ['create', '-f', '-', '-o', 'json'], input=raw))
            objects.append((obj['kind'], created['metadata']['uid']))
        subprocess.run(K + ['wait', '--for=condition=complete', 'job/' + name, '--timeout=100s'], check=True)
        assert subprocess.check_output(K + ['logs', 'job/' + name], text=True).strip() == 'CSI_READ_ONLY_PASS'
        print('PASS: real RKE2 Pod, Model Proxy identity, trusted TLS and non-root read-only CSI file')
    finally:
        for kind, uid in reversed(objects):
            obj = json.loads(subprocess.check_output(K + ['get', kind, name, '-o', 'json']))
            if obj['metadata']['uid'] != uid or obj['metadata']['labels'].get('anvilkit.io/owner') != OWNER:
                raise RuntimeError('Refusing cleanup: ownership changed')
            subprocess.run(K + ['delete', kind, name, '--wait=true'], check=True)
        for resource in ['kv/metadata/' + path, MOUNT + 'role/' + name, 'sys/policies/acl/' + name]:
            request(resource, token=root, method='DELETE')


if __name__ == '__main__':
    main()
