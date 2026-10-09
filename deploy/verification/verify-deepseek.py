#!/usr/bin/env python3
"""Two serial, bounded real Preparation calls through the deployed Model Proxy.

Reads only the local API test identity, never the provider credential. Prompt,
model output, artifact capabilities and native provider bodies are not logged.
No replacement is sent after UNKNOWN. All paid records are retained.
"""
import contextlib
import datetime
import hashlib
import http.client
import json
import pathlib
import re
import socket
import subprocess
import time
import urllib.parse
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATE = ROOT / '.local/repair-rke2'
OUT = ROOT / 'outputs/repairs-f05-f12-followup'
K = [str(ROOT / '.local/bin/kubectl'), '--kubeconfig', str(STATE / 'kubeconfig.yaml'), '-n', 'anvilkit-verification']


@contextlib.contextmanager
def forward(service, target, container_port=None):
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    process = subprocess.Popen(K + ['port-forward', '--address', '127.0.0.1', 'svc/' + service, f'{port}:{target}'],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert process.stdout.readline().strip() == f'Forwarding from 127.0.0.1:{port} -> {container_port or target}'
        yield port
    finally:
        process.terminate()
        process.wait(timeout=10)


def sql(statement):
    result = subprocess.run(K + ['exec', '-i', 'deploy/postgres', '--', 'sh', '-c',
        'PGPASSWORD="$POSTGRES_PASSWORD" psql -U postgres -d anvilkit_control -v ON_ERROR_STOP=1 -Atq'],
        input=statement, text=True, capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError('Verification database query failed; no credentials or query body logged')
    return result.stdout.strip()


def identifier(value):
    assert re.fullmatch(r'[a-zA-Z0-9_-]+', value)
    return "'" + value + "'"


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    assert datetime.datetime(2026, 9, 19, tzinfo=datetime.timezone.utc) <= now < datetime.datetime(2026, 9, 21, tzinfo=datetime.timezone.utc), 'Refresh reviewed pricing before another window'
    ns = json.loads(subprocess.check_output(K + ['get', 'namespace', 'anvilkit-verification', '-o', 'json']))
    assert ns['metadata']['labels']['anvilkit.io/owner'] == 'repairs-f01-f12'
    assert (ROOT / '.local/openbao/credential-association.json').is_file(), 'Enter the real credential and association locally first'
    token = json.loads((STATE / 'dependencies.json').read_text())['api_token_a']
    run = 'deepseek-' + uuid.uuid4().hex
    evidence = {'run': run, 'calls': [], 'status': 'INCOMPLETE', 'pricingSource': 'https://api-docs.deepseek.com/quick_start/pricing'}
    # Local test grants, not daily spending caps. Control allocates each task
    # normally and reserves every physical send against its task allocation.
    for level, tenant, actor, cap in [('platform', 'NULL', 'NULL', 1000000000), ('tenant', "'tenant_a'", 'NULL', 100000000), ('actor', "'tenant_a'", "'user_a'", 10000000)]:
        sql(f"INSERT INTO budget_pools(pool_id,level,tenant_id,actor_id,period_start,period_end,currency,cap_amount) VALUES('{run}-{level}','{level}',{tenant},{actor},now()-interval '1 second',now()+interval '1 hour','USD',{cap});")
    try:
        with forward('anvilkit-agent-api', 80, 9100) as api_port, forward('minio', 9000) as store_port:
            def api(method, path, body=None, expected=200):
                connection = http.client.HTTPConnection('127.0.0.1', api_port, timeout=30)
                try:
                    connection.request(method, path, None if body is None else json.dumps(body), {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
                    response = connection.getresponse()
                    raw = response.read()
                    assert response.status == expected, f'API {method} returned {response.status}, expected {expected}'
                    return json.loads(raw)
                finally:
                    connection.close()

            prompts = [b'I need a marketing component, but have not chosen its purpose or content yet.', b'A static Hero component for a gardening service. Package @verification/garden-hero. Editable headline, description and booking link. No network requests or form submission. Accessible responsive layout. Headline: Garden care. Call to action: Book a visit.']
            for index, prompt in enumerate(prompts):
                occurrence = run + '-' + str(index)
                digest = 'sha256:' + hashlib.sha256(prompt).hexdigest()
                transfer = api('POST', '/api/v1/artifacts/transfers', {'commandId': occurrence + '-upload', 'class': 'prompt', 'expectedDigest': digest, 'expectedSize': str(len(prompt)), 'mediaType': 'text/plain'}, 201)
                url = urllib.parse.urlsplit(transfer['uploadCapability'])
                assert url.scheme == 'http' and url.netloc == 'minio:9000'
                connection = http.client.HTTPConnection('127.0.0.1', store_port, timeout=30)
                try:
                    headers = dict(transfer['uploadHeaders']); headers['Host'] = url.netloc
                    connection.request('PUT', url.path + '?' + url.query, prompt, headers)
                    response = connection.getresponse(); response.read()
                    assert response.status == 200, f'Artifact PUT returned {response.status}'
                    version = response.getheader('x-amz-version-id')
                    assert version
                finally:
                    connection.close()
                finalized = api('POST', '/api/v1/artifacts/' + transfer['handle'] + '/finalizations', {'commandId': occurrence + '-finalize', 'objectVersion': version})
                body = {'commandId': occurrence, 'prompt': {'transferId': finalized['transferId'], 'digest': digest}, 'brandReferences': []}
                created = api('POST', '/api/v1/preparations', body, 202)
                operation = created['operationId']; op = identifier(operation)
                assert api('POST', '/api/v1/preparations', body, 202)['operationId'] == operation
                item = {'operationId': operation}; evidence['calls'].append(item)
                (OUT / (run + '.json')).write_text(json.dumps(evidence, indent=2))
                deadline = time.monotonic() + 300
                canceled = False
                while time.monotonic() < deadline:
                    view = api('GET', '/api/v1/operations/' + operation)
                    item['lifecycle'] = view['lifecycle']
                    if view['lifecycle'] == 'waiting' and not canceled:
                        api('POST', '/api/v1/operations/' + operation + '/commands', {'commandId': occurrence + '-cancel', 'kind': 'cancel', 'expectedRevision': view['revision']}, 202)
                        canceled = True
                    if view['lifecycle'] in ['succeeded', 'failed', 'canceled']:
                        break
                    if view['lifecycle'] == 'reconciling':
                        raise RuntimeError('Original operation is reconciling; no replacement call will be sent')
                    time.sleep(1)
                else:
                    raise RuntimeError('Original operation exceeded verification wait; query its original identities before any further send')
                dispatches = json.loads(sql(f"SELECT coalesce(json_agg(json_build_object('dispatchId',dispatch_id,'callId',call_id,'state',state,'outcome',outcome,'meterRevision',meter_revision)), '[]'::json) FROM dispatches WHERE operation_id={op};"))
                item['dispatches'] = dispatches
                assert len(dispatches) == 1, 'Expected exactly one physical-send admission in this task'
                dispatch = dispatches[0]
                assert dispatch['state'] == 'observed' and dispatch['outcome'] == 'succeeded', 'Real provider did not succeed; do not submit a replacement'
                did = identifier(dispatch['dispatchId'])
                usage = json.loads(sql(f"SELECT json_build_object('input',max(input_units),'output',max(output_units),'reasoning',max(reasoning_units),'cached',max(cached_input_units),'observations',count(*)) FROM usage_observations WHERE dispatch_id={did};"))
                item['usage'] = usage
                assert usage['input'] > 0 and usage['output'] > 0
                assert 0 <= usage['cached'] <= usage['input'] and 0 <= usage['reasoning'] <= usage['output']
                expected_cost = sum((units * rate + 999999) // 1000000 for units, rate in [(usage['input']-usage['cached'],150000),(usage['output']-usage['reasoning'],600000),(usage['reasoning'],600000),(usage['cached'],3000)])
                cost = json.loads(sql(f"SELECT json_build_object('amount',coalesce(sum(amount),0),'entries',count(*)) FROM cost_entries WHERE dispatch_id={did} AND kind IN ('actual','correction');"))
                assert cost['amount'] == expected_cost and cost['entries'] == 1
                assert sql(f"SELECT count(*) FROM permits WHERE owner_id={op} AND state='active';") == '0'
                # A replay of the original intake cannot create another call,
                # event sequence or charge. Nothing invokes the provider here.
                assert api('POST', '/api/v1/preparations', body, 202)['operationId'] == operation
                time.sleep(2)
                assert sql(f"SELECT count(*) FROM dispatches WHERE operation_id={op};") == '1'
                assert sql(f"SELECT count(*) FROM cost_entries WHERE dispatch_id={did} AND kind IN ('actual','correction');") == '1'
                item.update(costMicroUSD=cost['amount'], expectedCostMicroUSD=expected_cost, result='PASS', canceledAfterClarification=canceled)
                print(json.dumps(item), flush=True)
            evidence['status'] = 'PASS'
    finally:
        # Expire only these grants; keep all operations, native observations,
        # reservations (including UNKNOWN) and costs available for reconciliation.
        sql(f"UPDATE budget_pools SET period_end=period_start+interval '1 second' WHERE pool_id IN ('{run}-platform','{run}-tenant','{run}-actor');")
        (OUT / (run + '.json')).write_text(json.dumps(evidence, indent=2))
    print('PASS: two serial real DeepSeek calls, native usage, exact Go costs, original-intake idempotence; candidate generation not exercised.')


if __name__ == '__main__':
    main()
