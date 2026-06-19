---
name: setup-k8s
description: Set up Canonical k8s and register it as a Juju cloud inside the sandbox VM. Use when asked to set up a k8s substrate, prepare for Kubernetes charm deployments, or before bootstrapping a Juju k8s controller.
---

# Task: set up Canonical k8s substrate

## Goal

Install and configure Canonical k8s inside the sandbox VM and register it
as a Juju cloud so that a Juju controller can be bootstrapped against it.

## Steps

1. Run the setup script:
   ```
   /project/development-sandbox/bin/setup-k8s.sh
   ```
   This is idempotent. Each step checks whether it has already been done.
   Wait for it to complete before proceeding.

2. Bootstrap a Juju controller. Use a descriptive controller name:
   ```
   juju bootstrap local-k8s k8s-ctrl
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

- `kubectl` is not available directly; use `sudo k8s kubectl` instead.
- The kubeconfig is at `/home/ubuntu/k8s.yaml`. Set `KUBECONFIG=/home/ubuntu/k8s.yaml`
  if Juju commands need it explicitly.
- The `local-k8s` Juju cloud name is used by convention. Use it when bootstrapping.
- k8s bootstrap and `wait-ready` can take several minutes on first run.
