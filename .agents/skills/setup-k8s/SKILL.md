---
name: setup-k8s
description: Set up Canonical k8s and Chaos Mesh inside the sandbox VM. Use when preparing Kubernetes charm deployments, bootstrapping a Juju k8s controller, or installing or checking Chaos Mesh on an existing sandbox cluster.
---

# Task: set up Canonical k8s substrate

## Goal

Prepare Canonical k8s, register the local-k8s Juju cloud, and install or
check Chaos Mesh. Run these steps inside the sandbox VM, not on shared CI
clusters. Setup does not create chaos experiments.

## Set up Kubernetes

1. Run the setup script:
   ```
   $PROJECT_ROOT/development-sandbox/bin/setup-k8s.sh
   ```
   This also runs the Chaos Mesh setup. Do not run it separately afterward.
   Wait for the script to finish successfully before proceeding.

2. For manual charm work, bootstrap a controller and create a model only
   if needed. Check `juju controllers` and `juju models` first. When using
   the integration test suite, let the suite handle controller bootstrap
   as described in [setup-charm-tests](../setup-charm-tests/SKILL.md).
   ```
   juju bootstrap local-k8s k8s-ctrl
   juju add-model testing
   ```

3. For manual charm work, check the working model:
   ```
   juju status -m testing
   ```

## Set up Chaos Mesh on an existing cluster

Use this path when Kubernetes is already ready and only Chaos Mesh setup
or verification is needed. Do not bootstrap another controller or model.

1. Ensure Helm 3, jq, and a readable Kubernetes configuration are available.
   The script uses kubectl, or `sudo -n k8s kubectl` if kubectl is absent.
2. Run:
   ```bash
   bash "$PROJECT_ROOT/development-sandbox/bin/setup-chaos-mesh.sh" /home/ubuntu/k8s.yaml
   ```
   The configuration path is optional and defaults to `/home/ubuntu/k8s.yaml`.
3. Confirm the script exits successfully. It checks StressChaos and IOChaos
   CRDs and waits for the controller, DNS server, and daemon rollouts.

## Versions and settings

The scripts are the source of truth for versions. Kubernetes setup installs
Helm 3.21.4 with SHA-256 verification if Helm is absent. It keeps an existing
Helm 3 installation and rejects other major versions. It also installs curl,
jq, and CA certificates if needed. Tool setup and the Canonical k8s fallback
need sudo access without a password prompt.

Chaos Mesh uses chart 2.8.4 from https://charts.chaos-mesh.org, release and
namespace `chaos-mesh`, runtime `containerd`, socket
`/run/containerd/containerd.sock`, and `dashboard.create=false`. It installs
CRDs, RBAC, webhooks, and a privileged daemon. On Cilium, use NetworkPolicy
rather than NetworkChaos for network isolation.

An existing release with the expected version and settings is checked without
reinstalling. If its version, status, or settings differ, stop and inspect it;
do not upgrade or uninstall automatically. Failed installation can leave CRDs
and the namespace behind even with Helm atomic cleanup. Inspect before retrying.

For inspection without running an experiment:

```bash
helm status chaos-mesh --namespace chaos-mesh --kubeconfig /home/ubuntu/k8s.yaml
sudo k8s kubectl --kubeconfig /home/ubuntu/k8s.yaml get deployments,daemonsets,pods -n chaos-mesh
```
