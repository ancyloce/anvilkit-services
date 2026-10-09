#!/usr/bin/env python3
"""Install a pinned, checksum-verified gVisor release on the owned sandbox VM."""
import importlib.util,pathlib
spec=importlib.util.spec_from_file_location('rke2',pathlib.Path(__file__).with_name('configure-rke2.py'))
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
script='''set -eu
[ "$(hostname)" = anvilkit-verify-sandbox ]
sudo apt-get install -y -qq bzip2
mkdir -p /tmp/anvilkit-gvisor
cd /tmp/anvilkit-gvisor
curl -fLsS --retry 3 https://github.com/google/gvisor/releases/download/release-20260914.0/gvisor-x86_64.tar.bz2 -o gvisor-x86_64.tar.bz2
curl -fLsS --retry 3 https://github.com/google/gvisor/releases/download/release-20260914.0/SHA512SUMS -o SHA512SUMS
sed -n '/gvisor-x86_64.tar.bz2/p' SHA512SUMS | sha512sum -c -
sudo tar -xjf gvisor-x86_64.tar.bz2 -C /usr/local/bin
sudo mkdir -p /var/lib/rancher/rke2/agent/etc/containerd
sudo tee /var/lib/rancher/rke2/agent/etc/containerd/config-v3.toml.tmpl >/dev/null <<'EOF'
{{ template "base" . }}
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runsc.options]
  BinaryName = "/usr/local/bin/runsc"
  SystemdCgroup = true
EOF
sudo systemctl restart rke2-agent
/usr/local/bin/runsc --version
sha512sum gvisor-x86_64.tar.bz2
'''
print(r.ssh('sandbox','bash -s',script.encode()).decode())
