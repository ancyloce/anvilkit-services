#!/usr/bin/env python3
"""An owned NAT network with stable guest addresses; no existing switch is changed."""
import base64,json,pathlib,subprocess
ROOT=pathlib.Path(__file__).resolve().parents[2];STATE=ROOT/'.local/repair-rke2'
PS='/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
def ps(text):
 code="$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'; "+text
 return subprocess.check_output([PS,'-NoProfile','-EncodedCommand',base64.b64encode(code.encode('utf-16le')).decode()],text=True)
nodes=json.loads(ps("Get-VM -Name 'anvilkit-verify-*' | Get-VMNetworkAdapter | Select VMName,IPAddresses | ConvertTo-Json -Depth 4"))
ps("""if (-not (Get-VMSwitch -Name 'AnvilKit Verification' -ErrorAction SilentlyContinue)) {
 if (Get-NetRoute -AddressFamily IPv4 | Where-Object {$_.DestinationPrefix -eq '10.203.0.0/24'}) { throw 'verification CIDR already in use' }
 New-VMSwitch -Name 'AnvilKit Verification' -SwitchType Internal -Notes 'anvilkit-services-repairs-f01-f12' | Out-Null
 New-NetIPAddress -IPAddress 10.203.0.1 -PrefixLength 24 -InterfaceAlias 'vEthernet (AnvilKit Verification)' | Out-Null
 New-NetNat -Name 'AnvilKit Verification' -InternalIPInterfaceAddressPrefix '10.203.0.0/24' | Out-Null
} elseif ((Get-VMSwitch -Name 'AnvilKit Verification').Notes -ne 'anvilkit-services-repairs-f01-f12') { throw 'switch owner mismatch' }""")
for ordinal,role in enumerate(['application','data','sandbox'],11):
 name='anvilkit-verify-'+role
 if ps(f"(Get-VM -Name '{name}').Notes").strip()!='anvilkit-services-repairs-f01-f12': raise SystemExit('VM owner mismatch')
 ip=f'10.203.0.{ordinal}'
 node=next(n for n in nodes if n['VMName']==name)
 old=next(a for a in node['IPAddresses'] if ':' not in a)
 config=f'''network:
  version: 2
  ethernets:
    eth0:
      dhcp4: false
      addresses: [{ip}/24]
      routes: [{{to: default, via: 10.203.0.1}}]
      nameservers: {{addresses: [1.1.1.1, 8.8.8.8]}}
'''
 script="set -eu\nprintf 'network: {config: disabled}\\n' | sudo tee /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg >/dev/null\nsudo tee /etc/netplan/50-cloud-init.yaml >/dev/null <<'YAML'\n"+config+"YAML\nsudo chmod 600 /etc/netplan/50-cloud-init.yaml\nsudo systemd-run --on-active=3 /usr/sbin/netplan apply\n"
 subprocess.run(['/mnt/c/Windows/System32/OpenSSH/ssh.exe','-i',r'D:\AnvilKit\verification\access\id_ed25519','-o','StrictHostKeyChecking=accept-new','-o',r'UserKnownHostsFile=D:\AnvilKit\verification\access\known_hosts','anvilkit@'+old,'bash -s'],input=script.encode(),check=True)
 ps(f"Connect-VMNetworkAdapter -VMName '{name}' -SwitchName 'AnvilKit Verification'")
 print(name+' -> '+ip,flush=True)
(STATE/'network-ownership.json').write_text(json.dumps({'owner':'anvilkit-services-repairs-f01-f12','switch':'AnvilKit Verification','nat':'AnvilKit Verification','cidr':'10.203.0.0/24'},indent=2))
