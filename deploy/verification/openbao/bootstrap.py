#!/usr/bin/env python3
"""Initialize the owned local Bao, keeping bootstrap credentials on protected disk."""
import json, os, pathlib, ssl, urllib.request, urllib.error
ROOT = pathlib.Path(__file__).resolve().parents[3]
STATE = ROOT / '.local/openbao'
URL = 'https://127.0.0.1:18200/v1/'
TLS = ssl.create_default_context(cafile=str(STATE / 'ca.crt'))
def request(path, body=None, token=None, method=None):
    headers = {'Content-Type': 'application/json'}
    if token: headers['X-Vault-Token'] = token
    req = urllib.request.Request(URL+path, data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=TLS, timeout=15) as response:
            data=response.read()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as error:
        # API response bodies could contain submitted values. Never print them.
        raise RuntimeError(f'OpenBao {path}: HTTP {error.code}') from None

def main():
    os.umask(0o077)
    init = STATE / 'init.json'
    if not request('sys/init')['initialized']:
        if init.exists(): raise SystemExit('Refusing to replace existing initialization material')
        init.write_text(json.dumps(request('sys/init', {'secret_shares':1,'secret_threshold':1})))
    if not init.exists(): raise SystemExit('Initialized Bao needs its existing protected unseal material')
    material=json.loads(init.read_text())
    if request('sys/seal-status')['sealed']:
        request('sys/unseal', {'key': material['keys_base64'][0]})
    token=material['root_token']
    mounts=request('sys/mounts', token=token)
    if 'kv/' not in mounts: request('sys/mounts/kv', {'type':'kv','options':{'version':'2'}}, token)
    elif mounts['kv/'].get('options',{}).get('version')!='2': raise SystemExit('Existing kv is not v2')
    if 'kubernetes/' not in request('sys/auth',token=token):
        request('sys/auth/kubernetes',{'type':'kubernetes'},token)
    request('auth/kubernetes/config', {'kubernetes_host':'https://kubernetes.default.svc:443'},token)
    policy='path "kv/data/anvilkit/local/model-proxy/deepseek" { capabilities = ["read"] }'
    request('sys/policies/acl/model-proxy-deepseek',{'policy':policy},token)
    request('auth/kubernetes/role/model-proxy-deepseek',{
        'bound_service_account_names':['anvilkit-agent-model-proxy'],
        'bound_service_account_namespaces':['anvilkit-verification'],
        'audience':'openbao','token_policies':['model-proxy-deepseek'],
        'token_ttl':'5m','token_max_ttl':'10m','token_no_default_policy':True},token)
    request('sys/policies/acl/deepseek-entry',{'policy':'path "kv/data/anvilkit/local/model-proxy/deepseek" { capabilities = ["create", "update"] }'},token)
    print('OpenBao unsealed; KV v2 and restricted Kubernetes role configured; no DeepSeek key created.')
if __name__=='__main__': main()
