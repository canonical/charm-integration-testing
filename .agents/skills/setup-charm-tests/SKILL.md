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

## Setup

```bash
mise run setup -- --group integration
/setup-lxd  # machine charms
/setup-k8s  # Kubernetes charms
```

Run both substrate skills for mixed-cloud tests. They use the `local-lxd` and
`local-k8s` Mise groups for declared tools and packages, then provision the
substrates. The integration group installs test and logging tools; the test
suite bootstraps controllers when needed.

---

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
./scripts/run-tests.sh \
  --target-cloud "localhost" \
  --target-charm "postgresql" \
  --target-endpoint "database" \
  --target-application "target" \
  --current-state "no_bundle" \
  --mermaid-output "./bundle.mmd" \
  ...

# The test suite will bootstrap both controllers for CMR
./scripts/run-tests.sh \
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
mise exec -- juju-crashdump --version
mise exec -- juju-k8s-crashdump --help
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
/setup-lxd
# Output: LXD substrate ready (or already configured)
```

### Problem: "kubeconfig not found"

If `/home/ubuntu/k8s.yaml` doesn't exist:

```bash
# Re-run K8s setup
/setup-k8s

# Verify location
ls -la /home/ubuntu/k8s.yaml
```

### Problem: "juju-crashdump: command not found"

If snap installation failed:

```bash
# Reconcile the test tool environment
mise run setup -- --group integration
mise exec -- juju-crashdump --version
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
./scripts/run-tests.sh \
  --target-cloud "localhost" \
  --target-charm "postgresql" \
  --target-endpoint "database" \
  --target-application "target" \
  --mermaid-output "./bundle.mmd" \
  --current-state "no_bundle" \
  --prefix "test-1"

# Test mixed-cloud CMR (LXD + K8s)
./scripts/run-tests.sh \
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
- **setup-lxd**: `/setup-lxd` skill
- **setup-k8s**: `/setup-k8s` skill
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
