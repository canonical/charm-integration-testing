---
name: setup-s3
description: Set up a local S3-compatible service in the development sandbox for tests using s3-integrator, without shared QA storage credentials or a new charm.
---

# Set up local S3

Run only inside the disposable development sandbox VM, never on shared CI runners.
This uses the Mise-locked SeaweedFS binary and starts a single-node
systemd service. No charm, Docker daemon, or shared Swift access is needed.

1. Install SeaweedFS, jq and the project environment, then check that `curl`,
   `openssl`, `ip` and passwordless sudo are available:

   ```bash
   mise run setup -- --group local-s3
   ```

2. Run:

   ```bash
   mise run setup-s3
   ```

3. The server configuration file contains one identity limited to harness buckets.
   The script verifies bucket creation, read/write and deletion with
   those credentials, and checks that out-of-scope bucket creation is denied.
   It writes the mode-0600
   `s3.conf` credential file only after that probe succeeds; the server configuration
   is created earlier so SeaweedFS can start. Load the path-only shell file in the test shell:

   ```bash
   source "$HOME/.local/share/charmqa-s3/s3.env"
   ```

   This exports `S3_CONFIG_localhost`, not credentials. For another logical Juju
   cloud, set `S3_CONFIG_<cloud>` to the config path, replacing hyphens with
   underscores in the variable name. Ambient `S3_*` credential variables are not
   used by the suite.

4. Follow [run-charm-tests](../run-charm-tests/SKILL.md) to run the suite.
   Its post-deploy extension finds
   s3-integrator on either substrate and creates a separate bucket per integrator.
   Use the VM address in the config's `S3_ENDPOINT`, not localhost or cluster-only DNS. Confirm
   that both Kubernetes pods and LXD units can reach port 8333.

The service stores data under `$HOME/.local/share/charmqa-s3/`, outside the source
tree. Setup restricts that directory to mode 0700 and existing credential files
to mode 0600 before reading them; symlinked or unowned credential paths are rejected.
Re-running setup keeps existing data and keys. An inactive existing service or
an unsupported older `s3.json` credential format requires inspection rather than
automatic replacement. Use `sudo journalctl -u charmqa-s3` for diagnostics. If the VM address changes, inspect
the service bind address; setup reports an unreachable endpoint instead of
automatically rebinding a service that may have active consumers.

Stop the service with `sudo systemctl stop charmqa-s3`. Do not delete its state
while consumers are using it. This single-node service is not production storage
and does not exercise the shared QA object storage service.
