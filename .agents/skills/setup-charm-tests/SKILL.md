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

### Logging & Debugging Tools (Always Installed)

Always install these regardless of platform, since log collection needs both tools:

- **juju-crashdump** (mise)
  - Collects logs from machine/LXD controllers
  - Captures unit logs, charm output, and Juju internals

- **juju-k8s-crashdump** (mise)
  - Collects logs from Kubernetes controllers
  - Captures pod logs, statefulset events, and K8s-specific state
  - **Required for reproducing issue #693** (mixed-cloud CMR logging)
  - Installed from the commit pinned in `mise.toml` and `mise.lock`

- **kubectl** (mise)
  - Inspect Kubernetes pods and events
  - Used by juju-k8s-crashdump

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

Install the repository's locked tools before setting up substrates:

```bash
cd "$PROJECT_ROOT"
mise run setup
```

This installs the shared toolchain and project dependencies. The sandbox
automatically selects the development and sandbox additions.

### Option 1: Machine Charms Only (LXD)

```bash
# Setup LXD only
cd "$PROJECT_ROOT" && mise run sandbox:setup-lxd

# That's it! Test suite will bootstrap controller when you run tests with --current-state
```

### Option 2: Kubernetes Charms Only

```bash
# Setup K8s only
cd "$PROJECT_ROOT" && mise run sandbox:setup-k8s

# That's it! Test suite will bootstrap controller when you run tests with --current-state
```

### Option 3: Mixed-Cloud CMR Tests (LXD + K8s)

```bash
# Setup both substrates
cd "$PROJECT_ROOT" && mise run sandbox:setup-lxd
cd "$PROJECT_ROOT" && mise run sandbox:setup-k8s

# That's it! Test suite will bootstrap both controllers when you run tests with --current-state
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

Once setup is complete, run tests with the test suite. The suite handles controller bootstrap based on `--current-state`:

```bash
# The test suite will bootstrap LXD controller automatically
mise run test:suite -- \
  --target-cloud "localhost" \
  --target-charm "postgresql" \
  --target-endpoint "database" \
  --target-application "target" \
  --current-state "no_bundle" \
  --mermaid-output "./bundle.mmd" \
  ...

# The test suite will bootstrap both controllers for CMR
mise run test:suite -- \
  --target-cloud "localhost" \
  --target-charm "glauth-utils" \
  --neighbor-cloud "local-k8s" \
  --neighbor-charm "glauth-k8s" \
  --current-state "no_bundle" \
  ...
```

See the `run-charm-tests` skill for full test execution documentation.

---

## Verification

### Check Logging Tools

```bash
juju-crashdump --help
juju-k8s-crashdump --help  # Should show help without errors
kubectl version --client
```

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

This is fine! The setup scripts are idempotent:

```bash
cd "$PROJECT_ROOT" && mise run sandbox:setup-lxd
# Output: LXD substrate ready (or already configured)
```

### Problem: "Cannot install juju-k8s-crashdump"

Check access to the pinned source repository, then rerun the shared setup:

```bash
git ls-remote https://github.com/canonical/juju-k8s-crashdump.git HEAD
mise run setup
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
mise run setup
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

## Next Steps

After setup completes, you can run charm integration tests:

```bash
# Test a single charm on LXD
mise run test:suite -- \
  --target-cloud "localhost" \
  --target-charm "postgresql" \
  --target-endpoint "database" \
  --target-application "target" \
  --mermaid-output "./bundle.mmd" \
  --current-state "no_bundle" \
  --prefix "test-1"

# Test mixed-cloud CMR (LXD + K8s)
mise run test:suite -- \
  --target-cloud "localhost" \
  --target-charm "glauth-utils" \
  --target-endpoint "glauth-auxiliary" \
  --target-application "target" \
  \
  --neighbor-cloud "local-k8s" \
  --neighbor-charm "glauth-k8s" \
  --neighbor-endpoint "glauth-auxiliary" \
  --neighbor-application "neighbor" \
  \
  --current-state "no_bundle" \
  --mermaid-output "./bundle.mmd" \
  --prefix "test-cmr-1"
```

See the `run-charm-tests` skill for full documentation.

---

## Key References

- **charm-testing.yaml**: Official workflow that shows the canonical setup process
- **setup-lxd.sh**: `mise run sandbox:setup-lxd`
- **setup-k8s.sh**: `mise run sandbox:setup-k8s`
- **Issue #693**: Mixed-cloud CMR logging (requires juju-k8s-crashdump)

---

## Environment Details

- **Project**: charm-integration-testing
- **Python**: 3.12+
- **Juju**: 3/stable (3.6.25+)
- **LXD**: 5.21+
- **Kubernetes**: 1.32+ (Canonical k8s)
- **System**: Linux (Ubuntu 22.04+)

---

**Skill Version**: 1.0  
**Last Updated**: July 3, 2026
