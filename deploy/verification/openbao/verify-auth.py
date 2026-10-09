#!/usr/bin/env python3
"""Prove workload binding without reading or printing the provider credential."""
import json, subprocess
from bootstrap import ROOT,STATE,request
K=[str(ROOT/'.local/bin/kubectl'),'--context','kind-anvilkit-dev','-n','anvilkit-verification']
def jwt(sa):
 return subprocess.check_output(K+['create','token',sa,'--audience=openbao','--duration=10m'],text=True).strip()
root=json.loads((STATE/'init.json').read_text())['root_token']
try:
 request('auth/kubernetes/login',{'role':'model-proxy-deepseek','jwt':jwt('default')})
 raise SystemExit('FAIL: foreign workload identity was accepted')
except RuntimeError as e:
 if 'HTTP 403' not in str(e): raise
print('PASS: default workload identity denied')
auth=request('auth/kubernetes/login',{'role':'model-proxy-deepseek','jwt':jwt('anvilkit-agent-model-proxy')})
assert auth['auth']['policies']==['model-proxy-deepseek'],auth['auth']['policies']
cap=request('sys/capabilities',{'token':auth['auth']['client_token'],'paths':['kv/data/anvilkit/local/model-proxy/deepseek','kv/data/another']},root)
assert cap['kv/data/anvilkit/local/model-proxy/deepseek']==['read']
assert cap['kv/data/another']==['deny']
request('auth/token/revoke',{'token':auth['auth']['client_token']},root)
print('PASS: Model Proxy identity authenticates; exact secret read allowed; unrelated path denied')
