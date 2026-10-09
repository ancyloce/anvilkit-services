#!/usr/bin/env python3
"""Deploy repaired business images with owned dependencies and one-container topology."""
import json,os,pathlib,secrets,subprocess,sys,yaml
ROOT=pathlib.Path(__file__).resolve().parents[2];STATE=ROOT/'.local/repair-rke2';OUT=ROOT/'outputs/repairs-f01-f12';NS='anvilkit-verification'
os.umask(0o077)
K=[str(ROOT/'.local/bin/kubectl'),'--kubeconfig',str(STATE/'kubeconfig.yaml'),'-n',NS]
H=[str(ROOT/'.local/bin/helm'),'--kubeconfig',str(STATE/'kubeconfig.yaml')]
labels={'anvilkit.io/owner':'repairs-f01-f12'}
ns=json.loads(subprocess.check_output(K+['get','namespace',NS,'-o','json']))
if ns['metadata'].get('labels',{}).get('anvilkit.io/owner')!='repairs-f01-f12':raise SystemExit('unowned namespace')
creds=json.loads((STATE/'dependencies.json').read_text())
if 'inventory_password' not in creds:
 for key in ['inventory_password','proxy_password','api_token_a','api_token_b','proxy_token']:creds[key]=secrets.token_hex(24)
 (STATE/'dependencies.json').write_text(json.dumps(creds))
def apply(obj):
 raw=yaml.safe_dump(obj).encode()
 subprocess.run(K+['apply','--dry-run=server','--validate=strict','-f','-'],input=raw,check=True)
 subprocess.run(K+['apply','-f','-'],input=raw,check=True)
def secret(name,data):apply({'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'namespace':NS,'labels':labels},'stringData':data})
for domain in ['control','knowledge','mcp']:
 for role in ['app','migrator']+(['relay'] if domain!='control' else [])+(['forwarder'] if domain=='knowledge' else []):
  secret(domain+'-'+role,{'url':f"postgres://anvilkit_{domain}_{role}:{creds['database_password']}@postgres:5432/anvilkit_{domain}?sslmode=disable"})
secret('queue',{'url':'redis://valkey:6379'})
secret('artifacts',{'accessKeyId':'verification-root','secretAccessKey':creds['minio_password']})
secret('inventory',{'accessKeyId':'verification-inventory','secretAccessKey':creds['inventory_password']})
secret('proxy-store',{'accessKeyId':'verification-proxy','secretAccessKey':creds['proxy_password']})
secret('api-principals',{'principals.json':json.dumps({creds['api_token_a']:{'tenantId':'tenant_a','actorId':'user_a'},creds['api_token_b']:{'tenantId':'tenant_b','actorId':'user_b'}})})
secret('proxy-principals',{'principals.json':json.dumps({creds['proxy_token']:{'principalId':'verification-workflow','kind':'workflow'}})})
# Setup never echoes secret values and uses only the owned MinIO deployment.
setup=(ROOT/'deploy/dev/minio-setup.sh').read_text().replace('http://minio:9000','http://127.0.0.1:9000')
exports='\n'.join(f"export {name}='{value}'" for name,value in {'ANVILKIT_INVENTORY_ACCESS_KEY_ID':'verification-inventory','ANVILKIT_INVENTORY_SECRET_ACCESS_KEY':creds['inventory_password'],'ANVILKIT_MODEL_PROXY_STORE_ACCESS_KEY_ID':'verification-proxy','ANVILKIT_MODEL_PROXY_STORE_SECRET_ACCESS_KEY':creds['proxy_password']}.items())
subprocess.run(K+['exec','-i','deploy/minio','--','sh'],input=(exports+'\n'+setup).encode(),check=True)
# The existing reviewed stream definitions are loaded by the existing NATS CLI.
stream_data={'nats-setup.sh':(ROOT/'deploy/dev/nats-setup.sh').read_text()}
for path in (ROOT/'deploy/dev/nats').glob('*.json'):stream_data['nats/'+path.name]=path.read_text()
apply({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'nats-setup','namespace':NS},'data':{key.replace('nats/',''):value for key,value in stream_data.items()}})
setup_image=json.loads(subprocess.check_output(['docker','image','inspect','10.203.0.12:30500/platform/nats-box:repairs-f01-f12','--format','{{json .RepoDigests}}']))
setup_image=next(ref for ref in setup_image if ref.startswith('10.203.0.12:30500/'))
apply({'apiVersion':'batch/v1','kind':'Job','metadata':{'name':'nats-setup','namespace':NS,'labels':labels},'spec':{'backoffLimit':0,'activeDeadlineSeconds':120,'template':{'metadata':{'labels':labels},'spec':{'restartPolicy':'Never','automountServiceAccountToken':False,'nodeSelector':{'anvilkit.io/workload':'data'},'containers':[{'name':'setup','image':setup_image,'command':['sh','/setup/nats-setup.sh'],'env':[{'name':'NATS_URL','value':'nats://nats:4222'}],'volumeMounts':[{'name':'setup','mountPath':'/setup','readOnly':True}]}],'volumes':[{'name':'setup','configMap':{'name':'nats-setup','items':[{'key':key.replace('nats/',''),'path':key} for key in stream_data]}}]}}}})
subprocess.run(K+['wait','--for=condition=complete','job/nats-setup','--timeout=130s'],check=True)
# Domain migrations remain temporary Jobs with dedicated migrator identities.
for domain in ['knowledge','mcp']:
 name='migration-'+domain
 job={'apiVersion':'batch/v1','kind':'Job','metadata':{'name':name,'namespace':NS,'labels':labels},'spec':{'backoffLimit':0,'activeDeadlineSeconds':120,'template':{'metadata':{'labels':labels},'spec':{'restartPolicy':'Never','automountServiceAccountToken':False,'nodeSelector':{'anvilkit.io/workload':'data'},'containers':[{'name':'migration','image':'10.203.0.12:30500/anvilkit-migration@sha256:913504a0bd9dde7fca938e8e015a127e8c34622244caba56e34328171d6fa9b3','args':['-domain',domain,'-development'],'env':[{'name':'ANVILKIT_MIGRATION_DSN','valueFrom':{'secretKeyRef':{'name':domain+'-migrator','key':'url'}}}]}]}}}}
 apply(job)
 subprocess.run(K+['wait','--for=condition=complete','job/'+name,'--timeout=130s'],check=True)
 subprocess.run(K+['logs','job/'+name],check=True)
images=json.loads((OUT/'images.json').read_text())
def image(service):
 ref=next(v for v in images[service]['repoDigests'] if v.startswith('10.203.0.12:30500/'))
 repository,digest=ref.split('@');return {'repository':repository,'digest':digest,'pullPolicy':'IfNotPresent'}
valuesdir=ROOT/'deploy/verification/values';valuesdir.mkdir(exist_ok=True)
for service in ['control','api','workflow','model-proxy','knowledge','mcp','background-worker']:
 if len(sys.argv)>1 and service not in sys.argv[1:]:continue
 v={'replicaCount':1,'image':image(service),'podDisruptionBudget':{'enabled':False},'podLabels':labels,'resources':{'requests':{'cpu':'50m','memory':'96Mi'},'limits':{'cpu':'1','memory':'512Mi'}}}
 if service=='control':v.update({'database':{'secret':{'name':'control-app'}},'migration':{'secret':{'name':'control-migrator'}},'temporal':{'address':'temporal:7233'},'inventory':{'s3':{'endpoint':'http://minio:9000','bucket':'anvilkit-inventory','secret':{'name':'inventory'}}},'artifacts':{'enabled':True,'s3':{'endpoint':'http://minio:9000','bucket':'anvilkit-artifacts','secret':{'name':'artifacts'}}}})
 if service=='api':v.update({'control':{'address':'anvilkit-agent-control:9101'},'auth':{'principalsSecret':{'name':'api-principals'}}})
 if service=='workflow':v.update({'control':{'address':'anvilkit-agent-control:9101'},'temporal':{'address':'temporal:7233'},'launcher':{'namespace':'anvilkit-verification-sandbox','backend':'anvilkit-rke2-verification','imageRegistry':'10.203.0.12:30500','sidecarControlAddress':'anvilkit-agent-control.anvilkit-verification.svc:9101'},'config':{'kubernetes':{'enabled_profiles':['local-check-v1']}}})
 if service=='model-proxy':v.update({'control':{'address':'anvilkit-agent-control:9101'},'identity':{'principalsSecret':{'name':'proxy-principals'}},'store':{'s3':{'endpoint':'http://minio:9000','bucket':'anvilkit-model-proxy','secret':{'name':'proxy-store'}}}})
 if service in ['knowledge','mcp']:
  v.update({'database':{'secret':{'name':service+'-app'}},'nats':{'url':'nats://nats:4222'},'control':{'address':'anvilkit-agent-control:9101'},'relay':{'image':image('background-worker'),'database':{'secret':{'name':service+'-relay'}},'queue':{'secret':{'name':'queue'}}}})
  if service=='knowledge':v['forwarder']={'image':image('knowledge'),'database':{'secret':{'name':'knowledge-forwarder'}}}
 if service=='background-worker':v.update({'queue':{'secret':{'name':'queue'}},'owners':{'knowledge':{'address':'anvilkit-agent-knowledge:9105'},'mcp':{'address':'anvilkit-agent-mcp:9106'}},'config':{'worker':{'concurrency':1}}})
 if service=='control':v['config']={'profiles':{'generation':{'capacity':1}}}
 vf=valuesdir/(service+'.yaml');vf.write_text(yaml.safe_dump(v,sort_keys=False));os.chmod(vf,0o644)
 chart=ROOT/'services/agent'/service/'deploy/chart';release='anvilkit-agent-'+service
 args=[release,str(chart),'-n',NS,'-f',str(vf),'--post-renderer',str(ROOT/'deploy/verification/post-render.sh')]
 with (OUT/(service+'-helm-lint.log')).open('wb') as f:subprocess.run(H+['lint',str(chart),'-f',str(vf)],stdout=f,stderr=subprocess.STDOUT,check=True)
 rendered=subprocess.check_output(H+['template']+args);(OUT/(service+'-rendered.yaml')).write_bytes(rendered)
 with (OUT/(service+'-dry-run.log')).open('wb') as f:subprocess.run(K[:-2]+['apply','--dry-run=server','--validate=strict','-f','-'],input=rendered,stdout=f,stderr=subprocess.STDOUT,check=True)
 with (OUT/(service+'-helm-install.log')).open('wb') as f:subprocess.run(H+['upgrade','--install']+args+['--wait','--timeout','4m'],stdout=f,stderr=subprocess.STDOUT,check=True)
 print(service+' deployed',flush=True)
