#!/usr/bin/env python3
"""Reviewed verification topology; business services each get one container."""
import json,os,sys,yaml
for obj in yaml.safe_load_all(sys.stdin):
 if not obj: continue
 if obj['kind'] not in ['Namespace','ClusterRole','ClusterRoleBinding','RuntimeClass']:
  obj['metadata'].setdefault('namespace',os.environ.get('ANVILKIT_VERIFICATION_NAMESPACE','anvilkit-verification'))
 if obj['kind']=='HorizontalPodAutoscaler': continue
 if obj['kind']=='Deployment':
  spec=obj['spec'];spec['replicas']=1;spec['strategy']={'type':'Recreate'}
  pod=spec['template']['spec'];containers=pod['containers']
  component=spec['template']['metadata']['labels'].get('app.kubernetes.io/component')
  if component is None:
   component=spec['template']['metadata']['labels'].get('app.kubernetes.io/name','').removeprefix('anvilkit-agent-')
  pod.setdefault('nodeSelector',{})['anvilkit.io/workload']='application'
  annotations=spec['template']['metadata'].setdefault('annotations',{})
  annotations.update({'sidecar.istio.io/inject':'false','linkerd.io/inject':'disabled','vault.hashicorp.com/agent-inject':'false'})
  proxy_secret=annotations.get('anvilkit.io/model-proxy-token-secret')
  if proxy_secret:
   if component not in ['control','workflow']: raise SystemExit('unexpected Model Proxy client identity')
   containers[0]['env'].append({'name':'ANVILKIT_'+component.upper()+'_MODEL_PROXY_TOKEN','valueFrom':{'secretKeyRef':{'name':proxy_secret,'key':component}}})
  if component in ['knowledge','mcp']:
   main=containers[0];processes=[]
   for c in containers:
    name=c['name'];prefix='ANVILKIT_'+('BACKGROUND_WORKER' if name=='relay' else name.upper())+'_'
    command={'knowledge':['node','/anvilkit/knowledge/dist/main.js'],'mcp':['/usr/local/bin/anvilkit-agent-mcp'],'forwarder':['/usr/local/bin/anvilkit-knowledge-forwarder'],'relay':['node','/anvilkit/background-worker/dist/main.js','relay']}[name]
    health=next(e['value'] for e in c['env'] if e['name']==prefix+'HEALTH_LISTEN').replace('0.0.0.0','127.0.0.1')
    processes.append({'command':command,'prefix':prefix,'health':'http://'+health+'/readyz','cwd':'/anvilkit/'+('knowledge' if name=='knowledge' else 'background-worker')})
    if c is not main:
     main['env'].extend(c['env']);main['volumeMounts'].extend(c['volumeMounts']);main['ports'].extend(c.get('ports',[]))
   main['env'].append({'name':'ANVILKIT_SUPERVISOR_PROCESSES','value':json.dumps(processes)})
   main['env'].append({'name':'ANVILKIT_SUPERVISOR_SHUTDOWN_MS','value':'20000'})
   main['ports'].append({'name':'aggregate','containerPort':9199})
   for probe,path in [('readinessProbe','readyz'),('startupProbe','readyz'),('livenessProbe','healthz')]:
    main[probe]={'httpGet':{'path':'/'+path,'port':'aggregate'},'periodSeconds':2,'failureThreshold':60 if probe=='startupProbe' else 3}
   pod['containers']=[main]
  if len(pod['containers'])!=1: raise SystemExit('business service has multiple containers')
 print(yaml.safe_dump(obj,sort_keys=False)+'---')
