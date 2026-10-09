#!/usr/bin/env python3
"""Exercise packaged owner helpers and the Worker on exclusively owned RKE2 data."""
import json
import pathlib
import subprocess
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
K = [str(ROOT / '.local/bin/kubectl'), '--kubeconfig',
     str(ROOT / '.local/repair-rke2/kubeconfig.yaml'), '-n', 'anvilkit-verification']


def kubectl(*args, stdin=None):
    return subprocess.check_output(K + list(args), input=stdin, text=True)


namespace = json.loads(kubectl('get', 'namespace', 'anvilkit-verification', '-o', 'json'))
assert namespace['metadata']['labels']['anvilkit.io/owner'] == 'repairs-f01-f12'
worker = json.loads(kubectl('get', 'pods', '-l', 'app.kubernetes.io/name=anvilkit-agent-background-worker', '-o', 'json'))['items']
assert len(worker) == 1
worker_uid = worker[0]['metadata']['uid']


def sql(owner, statement):
    return kubectl('exec', '-i', 'deploy/postgres', '--', 'psql', '-U', 'postgres',
                   '-d', 'anvilkit_' + owner, '-At', '-v', 'ON_ERROR_STOP=1', stdin=statement)


def cli(owner, *args):
    command = (['node', '/anvilkit/knowledge/dist/localcheck.js'] if owner == 'knowledge'
               else ['/usr/local/bin/anvilkit-agent-mcp', 'local-check'])
    output = kubectl('exec', 'deploy/anvilkit-agent-' + owner, '--', *command, *args)
    return json.loads(output.splitlines()[-1])


for owner in ['knowledge', 'mcp']:
    identity = 'repair_' + uuid.uuid4().hex
    digest = 'sha256:' + '0' * 64
    if owner == 'knowledge':
        sql(owner, f"INSERT INTO sources (source_id,tenant_id,project_id,kind,locator,command_id,request_digest) VALUES ('{identity}','tenant_a','proj_a','document','file://fixture','cmd_{identity}','{digest}');")
        authorization = ['--source', identity]
    else:
        sql(owner, f"""
        INSERT INTO servers (server_id,tenant_id,canonical_resource,transport) VALUES ('{identity}','tenant_a','https://mcp.example/{identity}','streamable-http');
        INSERT INTO descriptors (server_id,revision,protocol_version,provenance,descriptor_digest,tools,state,command_id,request_digest) VALUES ('{identity}',1,'2025-06-18','test','{digest}','[]','approved','cmd_{identity}','{digest}');
        INSERT INTO grants (grant_id,tenant_id,subject_type,subject_id,server_id,descriptor_revision,descriptor_digest,methods,purpose,cost_cap_currency,cost_cap_amount,state,expires_at,command_id,request_digest) VALUES ('{identity}','tenant_a','actor','user_a','{identity}',1,'{digest}','{{tools/call}}','test','USD',0,'active',now()+interval '1 hour','grant_{identity}','{digest}');
        """)
        authorization = ['-grant', identity]
    for fail in [False, True]:
        task = cli(owner, 'request', *authorization, *(['--fail' if owner == 'knowledge' else '-fail'] if fail else []))
        expected = 'dead' if fail else 'accepted'
        deadline = time.monotonic() + 90
        while True:
            observed = cli(owner, 'get', '--task-id' if owner == 'knowledge' else '-task-id', task['taskId'])
            if observed['state'] == expected:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f'{owner}: expected {expected}, observed {observed}')
            time.sleep(1)
        attempts = sql(owner, f"SELECT count(*) || ':' || count(DISTINCT worker_id) FROM task_attempts WHERE task_id='{task['taskId']}';").strip()
        assert attempts == ('3:3' if fail else '1:1'), attempts
        if not fail:
            assert observed['resultDigest'] == task['expectedResultDigest']
        print(f'PASS: {owner} {expected}; attempts/distinct delivery identities={attempts}', flush=True)
current = json.loads(kubectl('get', 'pods', '-l', 'app.kubernetes.io/name=anvilkit-agent-background-worker', '-o', 'json'))['items']
assert len(current) == 1 and current[0]['metadata']['uid'] == worker_uid
assert all(s['restartCount'] == 0 for s in current[0]['status']['containerStatuses'])
print('PASS: all deliveries used the same live Worker Pod; no shared environment was cleared.')
