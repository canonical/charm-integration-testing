---
name: setup-charm-tests
description: Install charm testing prerequisites - cloud substrates and logging tools. The test suite handles controller bootstrap.
---

# Skill: setup-charm-tests

## Goal

Install only the prerequisites needed for charm integration testing:
1. **Cloud substrates** (LXD and/or Kubernetes) - only what you need
2. **Logging tools** (always installed for all scenarios)

The test suite automatically bootstraps controllers when you use `--current-state "no_bundle"` or `--current-state "no_controller"`.

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

- **juju-crashdump** (snap)
  - Collects logs from machine/LXD controllers
  - Captures unit logs, charm output, and Juju internals

- **juju-k8s-crashdump** (git+pip)
  - Collects logs from Kubernetes controllers
  - Captures pod logs, statefulset events, and K8s-specific state
  - **Required for reproducing issue #693** (mixed-cloud CMR logging)

- **kubectl** (snap)
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

### Option 1: Machine Charms Only (LXD)

```bash
# 1. Install logging tools
sudo snap install juju-crashdump --classic
sudo snap install kubectl --classic
pipx install "git+https://github.com/canonical/juju-k8s-crashdump.git@22ef04caaeeb94ad6ed49f8392b6bada65184569#egg=juju-k8s-crashdump"

# 2. Setup LXD only
$PROJECT_ROOT/development-sandbox/bin/setup-lxd.sh

# That's it! Test suite will bootstrap controller when you run tests with --current-state
```

### Option 2: Kubernetes Charms Only

```bash
# 1. Install logging tools
sudo snap install juju-crashdump --classic
sudo snap install kubectl --classic
pipx install "git+https://github.com/canonical/juju-k8s-crashdump.git@22ef04caaeeb94ad6ed49f8392b6bada65184569#egg=juju-k8s-crashdump"

# 2. Setup K8s only
$PROJECT_ROOT/development-sandbox/bin/setup-k8s.sh

# That's it! Test suite will bootstrap controller when you run tests with --current-state
```

### Option 3: Mixed-Cloud CMR Tests (LXD + K8s)

```bash
# 1. Install logging tools
sudo snap install juju-crashdump --classic
sudo snap install kubectl --classic
pipx install "git+https://github.com/canonical/juju-k8s-crashdump.git@22ef04caaeeb94ad6ed49f8392b6bada65184569#egg=juju-k8s-crashdump"

# 2. Setup both substrates
$PROJECT_ROOT/development-sandbox/bin/setup-lxd.sh
$PROJECT_ROOT/development-sandbox/bin/setup-k8s.sh

# That's it! Test suite will bootstrap both controllers when you run tests with --current-state
```

### Installing Logging Tools Details

All three tools should be installed regardless of which clouds you're using, since you might need both for log collection in mixed-cloud scenarios.

```bash
# juju-crashdump: For machine/LXD controllers
sudo snap install juju-crashdump --classic

# kubectl: For Kubernetes inspection and juju-k8s-crashdump support
sudo snap install kubectl --classic

# juju-k8s-crashdump: For Kubernetes controllers
# Using specific commit hash from charm-testing.yaml workflow
pipx install "git+https://github.com/canonical/juju-k8s-crashdump.git@22ef04caaeeb94ad6ed49f8392b6bada65184569#egg=juju-k8s-crashdump"
```

**Note**: If installation fails, see Troubleshooting section below.

---

## Running Tests After Setup

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
juju-crashdump --version
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
$PROJECT_ROOT/development-sandbox/bin/setup-lxd.sh
# Output: LXD substrate ready (or already configured)
```

### Problem: "Cannot install juju-k8s-crashdump"

The tool is in a private repository. If you encounter issues:

```bash
# Try with git+https instead of git+ssh
git clone https://github.com/canonical/juju-k8s-crashdump.git
cd juju-k8s-crashdump
pip install .

# Or use the commit hash from charm-testing.yaml
pip install "git+https://github.com/canonical/juju-k8s-crashdump@22ef04caaeeb94ad6ed49f8392b6bada65184569"
```

### Problem: "kubeconfig not found"

If `/home/ubuntu/k8s.yaml` doesn't exist:

```bash
# Re-run K8s setup
$PROJECT_ROOT/development-sandbox/bin/setup-k8s.sh

# Verify location
ls -la /home/ubuntu/k8s.yaml
```

### Problem: "juju-crashdump: command not found"

If snap installation failed:

```bash
# Try reinstalling
sudo snap remove juju-crashdump
sudo snap install juju-crashdump --classic
juju-crashdump --version
```

### Problem: "Controller bootstrap timeout"

LXD and K8s bootstrap can take 5-10 minutes on first run. If timeout occurs:

```bash
# Check controller status
juju controllers

# Wait and retry
juju bootstrap localhost lxd-ctrl

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
- **setup-lxd.sh**: `$PROJECT_ROOT/development-sandbox/bin/setup-lxd.sh`
- **setup-k8s.sh**: `$PROJECT_ROOT/development-sandbox/bin/setup-k8s.sh`
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
