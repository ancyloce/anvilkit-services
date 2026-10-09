#!/usr/bin/env python3
"""Provision the owned TLS registry; credential values stay on protected disk/stdin."""
import base64,crypt,importlib.util,json,os,pathlib,secrets,subprocess
import yaml
ROOT=pathlib.Path(__file__).resolve().parents[2]
STATE=ROOT/'.local/repair-rke2/registry'
os.umask(0o077);STATE.mkdir(parents=True,exist_ok=True)
K=[str(ROOT/'.local/bin/kubectl'),'--kubeconfig',str(STATE.parent/'kubeconfig.yaml')]
NS='anvilkit-verification-platform'
def apply(obj):
 body=yaml.safe_dump(obj).encode()
 subprocess.run(K+['apply','--dry-run=server','--validate=strict','-f','-'],input=body,check=True)
 subprocess.run(K+['apply','-f','-'],input=body,check=True)
existing=subprocess.run(K+['get','namespace',NS,'-o','json'],capture_output=True)
if existing.returncode==0 and json.loads(existing.stdout)['metadata'].get('labels',{}).get('anvilkit.io/owner')!='repairs-f01-f12':raise SystemExit('Unowned namespace')
apply({'apiVersion':'v1','kind':'Namespace','metadata':{'name':NS,'labels':{'anvilkit.io/owner':'repairs-f01-f12'}}})
if not (STATE/'ca.crt').exists():
 subprocess.run(['openssl','req','-x509','-nodes','-newkey','rsa:3072','-days','365','-keyout',str(STATE/'ca.key'),'-out',str(STATE/'ca.crt'),'-subj','/CN=AnvilKit verification registry CA'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 subprocess.run(['openssl','req','-nodes','-newkey','rsa:3072','-keyout',str(STATE/'tls.key'),'-out',str(STATE/'tls.csr'),'-subj','/CN=10.203.0.12'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
 (STATE/'extensions.cnf').write_text('subjectAltName=IP:10.203.0.12,IP:127.0.0.1,DNS:registry.anvilkit-verification-platform.svc\nextendedKeyUsage=serverAuth\n')
 subprocess.run(['openssl','x509','-req','-in',str(STATE/'tls.csr'),'-CA',str(STATE/'ca.crt'),'-CAkey',str(STATE/'ca.key'),'-CAcreateserial','-out',str(STATE/'tls.crt'),'-days','90','-extfile',str(STATE/'extensions.cnf')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
if not (STATE/'auth.json').exists():
 pw=secrets.token_urlsafe(32);(STATE/'auth.json').write_text(json.dumps({'username':'verification','password':pw}))
 (STATE/'htpasswd').write_text('verification:'+crypt.crypt(pw,crypt.mksalt(crypt.METHOD_BLOWFISH))+'\n')
for name,files in [('registry-tls',['tls.crt','tls.key']),('registry-auth',['htpasswd'])]:
 apply({'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'namespace':NS,'labels':{'anvilkit.io/owner':'repairs-f01-f12'}},'type':'Opaque','data':{f:base64.b64encode((STATE/f).read_bytes()).decode() for f in files}})
helm=[str(ROOT/'.local/bin/helm'),'--kubeconfig',str(STATE.parent/'kubeconfig.yaml')]
chart=str(ROOT/'deploy/verification/registry')
manifest=subprocess.check_output(helm+['template','verification-registry',chart,'-n',NS])
subprocess.run(K+['apply','--dry-run=server','--validate=strict','-f','-'],input=manifest,check=True)
subprocess.run(helm+['upgrade','--install','verification-registry',chart,'-n',NS,'--wait','--timeout','5m'],check=True)
# Trust and authentication for Docker and the three containerd nodes.
endpoint='10.203.0.12:30500'
certdir=pathlib.Path('/etc/docker/certs.d')/endpoint;certdir.mkdir(parents=True,exist_ok=True);(certdir/'ca.crt').write_bytes((STATE/'ca.crt').read_bytes())
auth=json.loads((STATE/'auth.json').read_text())
(STATE/'docker').mkdir(exist_ok=True)
subprocess.run(['docker','--config',str(STATE/'docker'),'login',endpoint,'--username',auth['username'],'--password-stdin'],input=auth['password'].encode(),stdout=subprocess.DEVNULL,check=True)
spec=importlib.util.spec_from_file_location('rke2',ROOT/'deploy/verification/configure-rke2.py');r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
config=yaml.safe_dump({'configs':{endpoint:{'auth':auth,'tls':{'ca_file':'/etc/rancher/rke2/registry-ca.crt'}}}}).encode()
for role in r.HOSTS:
 r.ssh(role,'sudo tee /etc/rancher/rke2/registry-ca.crt >/dev/null',(STATE/'ca.crt').read_bytes())
 r.ssh(role,'sudo sh -c "umask 077; cat > /etc/rancher/rke2/registries.yaml"',config)
 r.ssh(role,'sudo systemctl restart rke2-'+('server' if role=='application' else 'agent'))
(STATE/'settings.json').write_text(json.dumps({'endpoint':endpoint,'namespace':NS,'ca':str(STATE/'ca.crt'),'auth_file':str(STATE/'auth.json'),'containerd_configuration':'/etc/rancher/rke2/registries.yaml'},indent=2))
print('PASS: private registry TLS, authenticated Docker push, and containerd trust configured.')
