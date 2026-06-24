---
name: setup-lxd
description: Set up LXD and register it as a Juju cloud inside the sandbox VM. Use when asked to set up an LXD substrate, prepare for machine charm deployments, or before bootstrapping a Juju LXD controller.
---

# Task: set up LXD substrate

## Goal

Install and configure LXD inside the sandbox VM and register it as a Juju
cloud so that a Juju controller can be bootstrapped against it for machine
charm deployments.

## Steps

1. Run the setup script:
   ```
   /project/development-sandbox/bin/setup-lxd.sh
   ```
   This is idempotent. Each step checks whether it has already been done.
   Wait for it to complete before proceeding.

2. Bootstrap a Juju controller. Use a descriptive controller name:
   ```
   juju bootstrap localhost lxd-ctrl
   ```

3. Create a working model:
   ```
   juju add-model testing
   ```

4. Verify the substrate is ready:
   ```
   juju status -m testing
   ```

## Notes

- The `localhost` Juju cloud refers to the local LXD installation.
- If the lxd group membership message appears, open a new shell before running
  `juju bootstrap` to ensure the ubuntu user has LXD access.
- Machine charms deployed on LXD run in LXD containers, not Kubernetes pods.
  Commands like `juju exec` work normally. `sudo k8s kubectl` is not relevant
  for LXD deployments.
