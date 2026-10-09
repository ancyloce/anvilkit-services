#!/usr/bin/env python3
"""A disposable CSI fixture, never a substitute DeepSeek credential."""
import json,subprocess,time,yaml
from bootstrap import ROOT,STATE,request
K=[str(ROOT/'.local/bin/kubectl'),'--context','kind-anvilkit-dev','-n','anvilkit-verification']
root=json.loads((STATE/'init.json').read_text())['root_token']
path='anvilkit/verification/csi-probe'
request('kv/data/'+path,{'data':{'probe':'non-provider-fixture'}},root)
request('sys/policies/acl/csi-probe',{'policy':'path "kv/data/'+path+'" { capabilities = ["read"] }'},root)
request('auth/kubernetes/role/csi-probe',{'bound_service_account_names':['anvilkit-agent-model-proxy'],'bound_service_account_namespaces':['anvilkit-verification'],'audience':'openbao','token_policies':['csi-probe'],'token_ttl':'1m','token_no_default_policy':True},root)
meta={'name':'csi-probe','namespace':'anvilkit-verification','labels':{'anvilkit.io/owner':'repairs-f01-f12'}}
spc={'apiVersion':'secrets-store.csi.x-k8s.io/v1','kind':'SecretProviderClass','metadata':meta,'spec':{'provider':'openbao','parameters':{'roleName':'csi-probe','baoAddress':'https://openbao.anvilkit-secrets.svc:8200','baoCACertPath':'/openbao/tls/ca.crt','audience':'openbao','objects':yaml.safe_dump([{'objectName':'probe','secretPath':'kv/data/'+path,'secretKey':'probe','filePermission':0o444}])}}}
job={'apiVersion':'batch/v1','kind':'Job','metadata':meta,'spec':{'backoffLimit':0,'activeDeadlineSeconds':90,'template':{'metadata':{'labels':meta['labels']},'spec':{'restartPolicy':'Never','serviceAccountName':'anvilkit-agent-model-proxy','automountServiceAccountToken':False,'securityContext':{'runAsUser':65532,'runAsGroup':65532,'fsGroup':65532},'containers':[{'name':'probe','image':'alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b','command':['sh','-c','test -r /credential/probe && test "$(cat /credential/probe)" = non-provider-fixture && ! touch /credential/write 2>/dev/null && echo CSI_READ_ONLY_PASS'],'securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},'resources':{'requests':{'cpu':'10m','memory':'8Mi'},'limits':{'cpu':'100m','memory':'32Mi'}},'volumeMounts':[{'name':'credential','mountPath':'/credential','readOnly':True}]}],'volumes':[{'name':'credential','csi':{'driver':'secrets-store.csi.k8s.io','readOnly':True,'volumeAttributes':{'secretProviderClass':'csi-probe'}}}]}}}}
manifest=yaml.safe_dump_all([spc,job]).encode()
subprocess.run(K+['apply','--dry-run=server','--validate=strict','-f','-'],input=manifest,check=True)
subprocess.run(K+['apply','-f','-'],input=manifest,check=True)
subprocess.run(K+['wait','--for=condition=complete','job/csi-probe','--timeout=100s'],check=True)
subprocess.run(K+['logs','job/csi-probe'],check=True)
print('PASS: non-root Model Proxy identity reads TLS CSI fixture through a read-only mount.')
# Only the explicitly owned test objects are removed. The provider secret is untouched.
for kind in ['job','secretproviderclass']:
 obj=json.loads(subprocess.check_output(K+['get',kind,'csi-probe','-o','json']))
 if obj['metadata']['labels'].get('anvilkit.io/owner')!='repairs-f01-f12': raise SystemExit('ownership mismatch')
 subprocess.run(K+['delete',kind,'csi-probe'],check=True)

for resource in ['kv/metadata/'+path, 'auth/kubernetes/role/csi-probe', 'sys/policies/acl/csi-probe']:
 request(resource, token=root, method='DELETE')
