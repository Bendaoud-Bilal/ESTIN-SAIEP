# SAIEP Isolator Layer

Enforces runtime containment. The scheduler decides *what* runs and labels
each pod with `saiep.estin.dz/isolation-tier`; the isolator acts on it.

| Piece | File | Applied by |
|-------|------|------------|
| Tenant network boundary (default deny, intra-namespace allow, DNS + egress proxy + MinIO :9000 + API server only) | `network/networkpolicy.yaml` | Scheduler, at provisioning and again on every job submission; `saiep_cli` redeploy re-renders all tenants (template, `${NAMESPACE}`, live API server endpoints) |
| Egress proxy (Squid, domain allowlist) | `network/egress-proxy.yaml` | `saiep_cli` platform deploy |
| Sandboxed runtime (gVisor) | `runtime/runtimeclass.yaml` | `saiep_cli` platform deploy |
| Runtime detection rules (Falco) | `security/falco.yaml` (Helm values) | `saiep_cli` (optional) |

## Isolation tiers

- `standard` (default): runc + restricted security context injected by the
  admission webhook (non-root per-tenant UID, read-only root FS, all
  capabilities dropped, seccomp RuntimeDefault) + Pod Security `restricted`.
- `sandboxed`: additionally runs under gVisor (`runtimeClassName: gvisor`).
  Requires `runsc` on the node and `SAIEP_SANDBOX_RUNTIME=gvisor` on the
  webhook. Without it, sandboxed requests are **denied**, never downgraded.

## Enabling gVisor on a kubeadm node

```bash
# on the node (Debian/Ubuntu), see https://gvisor.dev/docs/user_guide/install/
sudo apt-get install -y runsc
sudo tee -a /etc/containerd/config.toml >/dev/null <<'EOF'
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
EOF
sudo systemctl restart containerd
kubectl label node <node> saiep.estin.dz/gvisor=true
```

## Notes

- NetworkPolicy needs a CNI that enforces it (Calico, Cilium; kind's kindnet
  enforces it from kind v0.24).
- Change the egress allowlist in `network/egress-proxy.yaml` (GitOps) — the
  webhook injects `HTTP(S)_PROXY` into tenant containers when
  `SAIEP_EGRESS_PROXY` is set.
