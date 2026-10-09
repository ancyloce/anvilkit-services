#!/usr/bin/env python3
"""Interactive credential entry over verified local TLS. No key in argv or output."""
import getpass, json, os, sys
from bootstrap import STATE, request
if not sys.stdin.isatty():
    raise SystemExit('Run in an interactive terminal; piped credentials are not accepted.')
os.umask(0o077)
account=input('DeepSeek account/project association (your description): ').strip()
if not account: raise SystemExit('An account/project association is required.')
key=getpass.getpass('DeepSeek API key (hidden): ').strip()
if not key or any(c.isspace() for c in key): raise SystemExit('Invalid empty or whitespace-containing key.')
if input('Store for AnvilKit local functional testing? Type yes: ')!='yes':
    raise SystemExit('No credential stored.')
root=json.loads((STATE/'init.json').read_text())['root_token']
token=request('auth/token/create',{'policies':['deepseek-entry'],'no_default_policy':True,'ttl':'5m','num_uses':1},root)['auth']['client_token']
request('kv/data/anvilkit/local/model-proxy/deepseek',{'data':{'api_key':key}},token)
(STATE/'credential-association.json').write_text(json.dumps({'purpose':'AnvilKit local functional testing','association':account}))
print('Credential stored.')
