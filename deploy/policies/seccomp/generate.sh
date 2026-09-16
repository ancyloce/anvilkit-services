#!/bin/sh
# Reproduces anvilkit-candidate.json: containerd's default seccomp profile
# (the RuntimeDefault a container receives) for a process whose bounding set
# is exactly the supervisor's SETUID, SETGID and SETPCAP, with its socket
# rules replaced by one rule that admits AF_UNIX only. Every other socket
# family falls to the profile's default action (EPERM), so the container
# that runs the candidate cannot open an AF_INET/AF_INET6 (or netlink,
# packet, vsock) socket at all; a Pod NetworkPolicy would not separate it
# from the sidecar in the same Pod (DD-03 §5). Pinned: containerd v2.3.4
# (the release of the kind node image and the RKE2 line's containerd 2.x)
# and runtime-spec v1.3.0, resolved through the public module proxy.
#
#   sh deploy/policies/seccomp/generate.sh > deploy/policies/seccomp/anvilkit-candidate.json
set -eu
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cat > "$WORK/main.go" <<'GO'
package main

import (
	"encoding/json"
	"os"

	"github.com/containerd/containerd/v2/contrib/seccomp"
	specs "github.com/opencontainers/runtime-spec/specs-go"
)

func main() {
	caps := []string{"CAP_SETUID", "CAP_SETGID", "CAP_SETPCAP"}
	sp := &specs.Spec{Process: &specs.Process{Capabilities: &specs.LinuxCapabilities{Bounding: caps, Effective: caps, Permitted: caps}}}
	profile := seccomp.DefaultProfile(sp)
	kept := profile.Syscalls[:0]
	for _, rule := range profile.Syscalls {
		if len(rule.Names) == 1 && rule.Names[0] == "socket" {
			continue // the family-conditional socket rules of the default
		}
		names := rule.Names[:0]
		for _, n := range rule.Names {
			if n != "socket" && n != "socketcall" {
				names = append(names, n)
			}
		}
		rule.Names = names
		kept = append(kept, rule)
	}
	profile.Syscalls = append(kept, specs.LinuxSyscall{
		Names: []string{"socket"}, Action: specs.ActAllow,
		Args: []specs.LinuxSeccompArg{{Index: 0, Value: 1, Op: specs.OpEqualTo}}, // AF_UNIX only
	})
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	_ = enc.Encode(profile)
}
GO
printf 'module anvilkit-candidate-seccomp\n\ngo 1.26.8\n' > "$WORK/go.mod"
cd "$WORK"
GOWORK=off GOFLAGS=-mod=mod GOPROXY=${GOPROXY:-https://proxy.golang.org,direct} go get github.com/containerd/containerd/v2@v2.3.4 github.com/opencontainers/runtime-spec@v1.3.0 >/dev/null 2>&1
GOWORK=off GOFLAGS=-mod=mod GOPROXY=${GOPROXY:-https://proxy.golang.org,direct} go run .
