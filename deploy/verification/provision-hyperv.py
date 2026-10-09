#!/usr/bin/env python3
"""Provision only the three owned local verification VMs; never alter existing VMs."""
import base64
import hashlib
import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATE = ROOT / '.local/repair-rke2'
DISKS = pathlib.Path('/mnt/d/AnvilKit/verification')
PS = '/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'

def ps(command):
    code = "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; " + command
    return subprocess.check_output([PS, '-NoProfile', '-NonInteractive', '-EncodedCommand', base64.b64encode(code.encode('utf-16-le')).decode()], text=True).strip()

def run(*args):
    subprocess.run(args, check=True)

image = STATE / 'noble.img'
expected = next(line.split()[0] for line in (STATE / 'SHA256SUMS').read_text().splitlines() if line.endswith('noble-server-cloudimg-amd64.img'))
actual = hashlib.sha256(image.read_bytes()).hexdigest()
if actual != expected:
    raise SystemExit('Ubuntu image checksum mismatch')
key = STATE / 'id_ed25519'
if not key.exists():
    run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'anvilkit-local-verification', '-f', str(key))
public = key.with_suffix('.pub').read_text().strip()
if DISKS.exists() and not (DISKS / 'owner.json').exists():
    raise SystemExit('Refusing unowned VM directory')
DISKS.mkdir(parents=True, exist_ok=True)
owner = {'owner': 'anvilkit-services-repairs-f01-f12', 'ubuntu_sha256': actual}
if (DISKS / 'owner.json').exists() and json.loads((DISKS / 'owner.json').read_text()) != owner:
    raise SystemExit('VM directory ownership/source mismatch')
(DISKS / 'owner.json').write_text(json.dumps(owner, indent=2))
records = []
for role, memory in [('application', 6), ('data', 6), ('sandbox', 4)]:
    name = 'anvilkit-verify-' + role
    if ps(f"if (Get-VM -Name '{name}' -ErrorAction SilentlyContinue) {{ 'exists' }}"):
        raise SystemExit(f'{name} already exists; inspect it, no automatic overwrite')
    directory = DISKS / role
    directory.mkdir(exist_ok=True)
    disk = directory / 'root.vhdx'
    if not disk.exists():
        run('qemu-img', 'convert', '-O', 'vhdx', str(image), str(disk))
    win = f'D:\\AnvilKit\\verification\\{role}'
    ps(f"Resize-VHD -Path '{win}\\root.vhdx' -SizeBytes 40GB")
    user_data = directory / 'user-data'
    user_data.write_text(f'''#cloud-config
hostname: {name}
manage_etc_hosts: true
ssh_pwauth: false
disable_root: true
users:
  - name: anvilkit
    groups: [sudo]
    shell: /bin/bash
    sudo: ['ALL=(ALL) NOPASSWD:ALL']
    lock_passwd: true
    ssh_authorized_keys: [{json.dumps(public)}]
''')
    (directory / 'meta-data').write_text(f'instance-id: {name}\nlocal-hostname: {name}\n')
    (directory / 'network-config').write_text('version: 2\nethernets:\n  primary:\n    match: {name: "e*"}\n    dhcp4: true\n')
    seed = directory / 'seed.iso'
    run('cloud-localds', '--network-config='+str(directory/'network-config'), str(seed), str(user_data), str(directory/'meta-data'))
    win = f'D:\\AnvilKit\\verification\\{role}'
    ps(f"$vm=New-VM -Name '{name}' -Generation 2 -MemoryStartupBytes {memory}GB -VHDPath '{win}\\root.vhdx' -Path '{win}' -SwitchName 'Default Switch'; Set-VMMemory -VMName '{name}' -DynamicMemoryEnabled $false -StartupBytes {memory}GB; Set-VMProcessor -VMName '{name}' -Count 2; Set-VMFirmware -VMName '{name}' -EnableSecureBoot On -SecureBootTemplate MicrosoftUEFICertificateAuthority; Add-VMDvdDrive -VMName '{name}' -Path '{win}\\seed.iso'; Set-VM -VMName '{name}' -Notes 'anvilkit-services-repairs-f01-f12' -AutomaticStartAction Nothing -AutomaticStopAction ShutDown; Start-VM -Name '{name}'")
    records.append({'name': name, 'role': role, 'memory_gib': memory, 'vcpus': 2, 'disk_gib': 40, 'windows_path': win})
    (STATE/'vms.json').write_text(json.dumps({'source':owner,'vms':records},indent=2))
    print(name + ' created', flush=True)
