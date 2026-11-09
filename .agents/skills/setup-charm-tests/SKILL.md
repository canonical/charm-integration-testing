---
name: setup-charm-tests
description: Install charm testing prerequisites, including Chaos Mesh for Kubernetes sandbox tests, cloud substrates, and logging tools. The test suite handles controller bootstrap.
---

# Skill: setup-charm-tests

## Goal

Install only the prerequisites needed for charm integration testing:

1. **Cloud substrates** (LXD and/or Kubernetes) - only what you need
2. **Logging tools** (always installed for all scenarios)

The test suite automatically bootstraps controllers when you use `--current-state "no_bundle"` or `--current-state "no_controller"`.

Kubernetes and mixed setup also install or check Chaos Mesh through
`setup-k8s.sh`. Do not run the Chaos Mesh script again after successful setup.
For an existing sandbox cluster that only needs Chaos Mesh, follow the
[existing cluster steps](../setup-k8s/SKILL.md#set-up-chaos-mesh-on-an-existing-cluster).
Do not use the manual controller bootstrap steps when the test suite manages it.

---

## Quick Start

### Machine Charm Testing (LXD only)

```bash
/setup-charm-tests --platform machine
```

- ✅ Install and configure LXD
- ✅ Install logging tools
- ✅ Skip Kubernetes setup
- ⏳ Test suite will bootstrap LXD controller when needed

### Kubernetes Charm Testing (K8s only)

```bash
/setup-charm-tests --platform kubernetes
```

- ✅ Install and configure Canonical k8s
- ✅ Install logging tools
- ✅ Skip LXD setup
- ⏳ Test suite will bootstrap K8s controller when needed

### Cross-Model Relation Testing (LXD + K8s)

```bash
/setup-charm-tests --platform mixed
```

- ✅ Install and configure LXD
- ✅ Install and configure Canonical k8s
- ✅ Install logging tools
- ⏳ Test suite will bootstrap both controllers when needed

### Default (Everything)

```bash
/setup-charm-tests
```

- ✅ Installs all substrates and tools (same as `--platform mixed`)

---

## What Gets Installed

### Test prerequisites

```bash
mise run setup -- --group development integration
```

For machine tests, run `/setup-lxd`; for Kubernetes tests, run `/setup-k8s`.
For mixed-cloud tests, run both skills. They install their declared tools and
packages before provisioning the substrates.

---

### Logging & Debugging Tools (Always Installed)

Sandbox startup installs both crashdump tools for machine and Kubernetes logs.
Keep both available even for single-substrate tests, since log collection can
cross cloud boundaries. Their installation sources are declared in the root
`mise.toml` and are selected by the `integration` setup group.
The `kubectl` snap is installed only for Kubernetes or Chaos Mesh setup, not for
LXD-only sandboxes.

### Cloud Substrates (Optional based on `--platform`)

**LXD** (when `--platform machine` or `--platform mixed`)

- Juju cloud name: `localhost`
- Platform: machine (LXD containers)
- Use for: Traditional machine charms

**Canonical Kubernetes** (when `--platform kubernetes` or `--platform mixed`)

- Juju cloud name: `local-k8s`
- Platform: Kubernetes (pod-based)
- Use for: Kubernetes-native charms
- Kubeconfig: `/home/ubuntu/k8s.yaml`

---

## Step-by-Step Setup

Start the VM using the [sandbox guide](../../../development-sandbox/README.md).
Inside the VM, work from `$PROJECT_ROOT`. If tool setup failed or the tool
configuration changed, rerun:

```bash
cd "$PROJECT_ROOT"
mise run setup -- --group sandbox development integration agent
```

Install prerequisites with the relevant setup group, then run the substrate task
to initialize services. Provisioning does not install packages; the test suite
handles controller bootstrap.
To test another Juju client, use the [persistent channel selection](../../../CONTRIBUTING.md#selecting-the-juju-client)
before substrate setup; these tasks preserve that choice.

### Option 1: Machine Charms Only (LXD)

```bash
mise run setup -- --group local-lxd
mise run sandbox:setup-lxd
```

### Option 2: Kubernetes Charms Only

```bash
mise run setup -- --group local-k8s
mise run sandbox:setup-k8s
```

### Option 3: Mixed-Cloud CMR Tests (LXD + K8s)

```bash
mise run setup -- --group local-lxd local-k8s
mise run sandbox:setup-lxd
mise run sandbox:setup-k8s
```

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
