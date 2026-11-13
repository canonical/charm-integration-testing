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

1. Install prerequisites, then run the provisioning task:
   ```
   cd "$PROJECT_ROOT"
   mise run setup -- --group local-k8s
   mise run setup-k8s
   ```
   Mise installs the declared tools and packages. The task provisions the
   substrate with Concierge, writes `$HOME/k8s.yaml`, registers `local-k8s`
   and installs Chaos Mesh. Do not run Chaos Mesh separately afterward.
   Wait for the task to finish successfully before proceeding.
   Run `mise run setup-k8s -- --help` for other options.

2. For manual charm work, bootstrap a controller and create a model only
   if needed. Check `juju controllers` and `juju models` first. When using
   the integration test suite, let the suite handle controller bootstrap
   as described in [setup-charm-tests](../setup-charm-tests/SKILL.md).
   Use a 5 GB controller root disk, matching the Kubernetes bootstrap constraints
   in `static/environments.yaml`.
   ```
   juju bootstrap local-k8s k8s-ctrl --bootstrap-constraints root-disk=5G
   juju add-model testing
   ```

3. For manual charm work, check the working model:
   ```
   juju status -m testing
   ```

## Set up Chaos Mesh on an existing cluster

Use this path when Kubernetes is already ready and only Chaos Mesh setup
or verification is needed. Do not bootstrap another controller or model.

1. Provide a readable Kubernetes configuration. Install Helm 3, jq and kubectl
   without installing or initializing a local cluster:
   ```bash
   cd "$PROJECT_ROOT"
   mise run setup -- --tools aqua:helm/helm aqua:jqlang/jq --packages snap:kubectl
   ```
2. Run:
   ```bash
   cd "$PROJECT_ROOT"
   mise run setup-chaos-mesh -- /home/ubuntu/k8s.yaml
   ```
   The configuration path is optional and defaults to `$HOME/k8s.yaml`.
3. Confirm the task exits successfully. It checks StressChaos and IOChaos
   CRDs and waits for the controller, DNS server, and daemon rollouts.

## Versions and settings

mise pins Helm, jq and Concierge for the setup tasks. Concierge installs the
k8s snap from the channel in `mise.toml` and kubectl from
`stable`. Provisioning needs sudo access without a password prompt.

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
