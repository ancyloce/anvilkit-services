#!/usr/bin/env python3
"""Configure only the owned Hyper-V VMs through Windows SSH; secrets use stdin."""
import concurrent.futures, json, os, pathlib, subprocess, sys
ROOT=pathlib.Path(__file__).resolve().parents[2]
STATE=ROOT/'.local/repair-rke2'
SSH='/mnt/c/Windows/System32/OpenSSH/ssh.exe'
KEY=r'D:\AnvilKit\verification\access\id_ed25519'
KNOWN=r'D:\AnvilKit\verification\access\known_hosts'
HOSTS={'application':'10.203.0.11','data':'10.203.0.12','sandbox':'10.203.0.13'}
VERSION='v1.36.4+rke2r1'
def ssh(role, command, data=None):
    return subprocess.check_output([SSH,'-i',KEY,'-o','StrictHostKeyChecking=accept-new','-o','UserKnownHostsFile='+KNOWN,'-o','ConnectTimeout=10','anvilkit@'+HOSTS[role],command],input=data)
def prepare(role):
    script=f'''set -eu
[ "$(hostname)" = "anvilkit-verify-{role}" ]
sudo apt-get update -qq
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq apparmor apparmor-utils curl linux-cloud-tools-virtual
curl -fsSL https://raw.githubusercontent.com/rancher/rke2/{VERSION}/install.sh -o /tmp/rke2-install.sh
sudo env INSTALL_RKE2_VERSION='{VERSION}' INSTALL_RKE2_TYPE={'server' if role=='application' else 'agent'} sh /tmp/rke2-install.sh
sudo install -d -m 700 /etc/rancher/rke2
'''
    result=ssh(role,'bash -s',script.encode())
    (ROOT/f'outputs/repairs-f01-f12/rke2-install-{role}.log').write_bytes(result)
    print(role+' installed',flush=True)
def seccomp(role):
    """P0.5: node provisioning places the candidate syscall profile where the
    kubelet resolves localhostProfile anvilkit/candidate.json; the
    environment's job-admission values then set seccomp.install: false."""
    profile=(ROOT/'deploy/policies/chart/files/anvilkit-candidate.json').read_bytes()
    ssh(role,'sudo install -d -m 0755 /var/lib/kubelet/seccomp/anvilkit && sudo tee /var/lib/kubelet/seccomp/anvilkit/candidate.json >/dev/null && sudo chmod 0644 /var/lib/kubelet/seccomp/anvilkit/candidate.json',profile)
    print(role+' seccomp profile placed',flush=True)
if __name__=='__main__':
    os.umask(0o077)
    if "--resume" not in sys.argv:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(prepare,HOSTS))
    config=f'''node-name: anvilkit-verify-application
node-ip: {HOSTS['application']}
cni: cilium
node-label:
  - anvilkit.io/workload=application
write-kubeconfig-mode: "0600"
tls-san:
  - {HOSTS['application']}
  - 127.0.0.1
disable:
  - rke2-ingress-nginx
  - rke2-traefik
'''
    ssh('application','sudo tee /etc/rancher/rke2/config.yaml >/dev/null',config.encode())
    ssh('application','sudo systemctl enable rke2-server && sudo systemctl restart rke2-server')
    token=ssh('application','sudo cat /var/lib/rancher/rke2/server/node-token').decode().strip()
    for role in ['data','sandbox']:
        config=f'''server: https://{HOSTS['application']}:9345
token: {token}
node-name: anvilkit-verify-{role}
node-ip: {HOSTS[role]}
node-label:
  - anvilkit.io/workload={role}
'''
        if role=='sandbox': config+='node-taint:\n  - anvilkit.io/sandbox=true:NoSchedule\n'
        ssh(role,'sudo tee /etc/rancher/rke2/config.yaml >/dev/null',config.encode())
        ssh(role,'sudo systemctl enable --now rke2-agent')
    for role in HOSTS:
        seccomp(role)
    kube=ssh('application','sudo cat /etc/rancher/rke2/rke2.yaml').decode()
    kube=kube.replace('127.0.0.1:6443',HOSTS['application']+':6443').replace('default','anvilkit-rke2-verification')
    (STATE/'kubeconfig.yaml').write_text(kube)
    (STATE/'ssh-inventory.json').write_text(json.dumps({'hosts':HOSTS,'user':'anvilkit','ssh_executable':SSH,'identity_file':KEY,'known_hosts_file':KNOWN,'version':VERSION},indent=2))
    print('RKE2 configured; protected kubeconfig and actual SSH inventory recorded.',flush=True)
