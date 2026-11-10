---
name: setup-charm-tests
description: Install charm testing prerequisites, including Chaos Mesh for Kubernetes sandbox tests, cloud substrates, and logging tools. The test suite handles controller bootstrap.
---

# Skill: setup-charm-tests

## Goal

Prepare the substrates needed for charm integration tests, with logging tools
available regardless of substrate.

## Setup

Start the VM using the [sandbox guide](../../../development-sandbox/README.md#quick-start)
and work from `$PROJECT_ROOT` inside it. Sandbox startup prepares the toolchain;
rerun `scripts/sandbox.sh up` on the host if tool setup failed or its configuration
changed.

To reproduce another Juju client version, follow
[persistent channel selection](../../../CONTRIBUTING.md#selecting-the-juju-client)
before provisioning.

Choose the requested `--platform` for `/setup-charm-tests`:

- `machine`: follow [LXD setup](../setup-lxd/SKILL.md).
- `kubernetes`: follow [Kubernetes setup](../setup-k8s/SKILL.md).
- `mixed` or omitted: follow both.

Use only the prerequisite-installation and substrate-provisioning steps in those
skills, not their manual controller/model bootstrap steps. Wait for provisioning
to finish successfully. With `--current-state "no_bundle"` or
`--current-state "no_controller"`, the test suite creates the controllers.

Kubernetes setup also installs or checks Chaos Mesh; do not run it a second time.
For an existing sandbox cluster that only needs Chaos Mesh, follow the
[existing-cluster procedure](../setup-k8s/SKILL.md#set-up-chaos-mesh-on-an-existing-cluster).

## Logging prerequisites

Sandbox startup installs both crashdump tools for machine and Kubernetes logs.
Keep both available even for single-substrate tests, since log collection can
cross cloud boundaries. Their installation sources are declared in the root
`mise.toml` and are selected by the `integration` setup group.
The `kubectl` snap is installed only for Kubernetes or Chaos Mesh setup, not for
LXD-only sandboxes.

## Running Tests After Setup

Test controllers in the VM should have a 5 GB root disk on both LXD and
Kubernetes. Pass `--target-controller-bootstrap-constraints '{"root-disk":"5G"}'`
and, for a separate neighbor controller,
`--neighbor-controller-bootstrap-constraints '{"root-disk":"5G"}'`.

For Kubernetes sandbox tests, set
`KUBECONFIG_local_k8s=/home/ubuntu/k8s.yaml` in the test environment.
Tests request experiments through `require_chaos_tool`, which selects a
supporting client. Chaos Mesh CPU and memory experiments require the
StressChaos CRD, while I/O latency requires the IOChaos CRD. If no configured
client supports an experiment, it skips when requested. Missing Kubernetes
configuration and API errors fail rather than skip. CRD presence does not
prove that a chaos experiment will succeed.

Follow [run-charm-tests](../run-charm-tests/SKILL.md) for complete single-cloud
and cross-model test commands.

---

## Verification

### Check Logging Tools

```bash
juju-crashdump --help
juju-k8s-crashdump --help  # Should show help without errors
kubectl version --client
```

Check `kubectl version --client` only for Kubernetes or mixed-platform tests.
The `local-k8s` setup group installs it; it is not required for LXD-only tests.

### Check Cloud Setup (after running tests)

Once you run tests, controllers will be created and you can verify:

```bash
# Check what clouds are registered
juju clouds

# List controllers (will be auto-created by test suite)
juju controllers
```

---

## Troubleshooting

### Problem: "LXD is already installed"

This is fine. `/setup-lxd` is idempotent:

```bash
cd "$PROJECT_ROOT" && mise run sandbox:setup-lxd
# Output: LXD substrate ready (or already configured)
```

### Problem: "Cannot install juju-k8s-crashdump"

Check access to the pinned source repository, then rerun the shared setup:

```bash
git ls-remote https://github.com/canonical/juju-k8s-crashdump.git HEAD
mise run setup -- --group integration
```

### Problem: "kubeconfig not found"

If `/home/ubuntu/k8s.yaml` doesn't exist:

```bash
# Re-run K8s setup
cd "$PROJECT_ROOT" && mise run sandbox:setup-k8s

# Verify location
ls -la /home/ubuntu/k8s.yaml
```

### Problem: "juju-crashdump: command not found"

Rerun the shared setup and verify the mise-managed executable:

```bash
mise run setup -- --group integration
mise exec -- juju-crashdump --help
```

### Problem: "Controller bootstrap timeout"

LXD and K8s bootstrap can take 5-10 minutes on first run. If timeout occurs:

```bash
# Check controller status
juju controllers

# Wait and retry
juju bootstrap localhost lxd-ctrl --bootstrap-constraints root-disk=5G

# Or check logs
juju debug-log -c lxd-ctrl --limit 50
```

---

## Key References

- **charm-testing.yaml**: Official workflow that shows the canonical setup process
- **setup-lxd.sh**: `mise run sandbox:setup-lxd`
- **setup-k8s.sh**: `mise run sandbox:setup-k8s`
- **Issue #693**: Mixed-cloud CMR logging (requires juju-k8s-crashdump)

---

## Environment Details

- **Project**: charm-integration-testing
- **Tool versions and snap channels**: root `mise.toml`, `mise.lock` and `.mise/locks/`
- **System**: Linux (Ubuntu 22.04+)

---

**Skill Version**: 1.0  
**Last Updated**: July 3, 2026
