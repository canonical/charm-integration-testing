---
name: setup-s3
description: Set up a local S3-compatible service in the development sandbox for tests using s3-integrator, without Swift credentials or a new charm.
---

# Set up local S3

Run only inside the disposable development sandbox VM, never on shared CI runners.
This installs SeaweedFS 4.48 from a SHA-256-verified binary and starts a single-node
systemd service. No charm, Docker daemon, or shared Swift access is needed.

1. Check that `curl`, `tar`, `sha256sum`, `openssl`, `jq`, `ip`, `sudo`,
   `systemctl`, `systemd-run` and Poetry are available, and that `sudo -n true`
   succeeds. Install missing OS prerequisites with apt.
   The project Poetry environment must already be installed.
2. Run:

   ```bash
   bash "$PROJECT_ROOT/development-sandbox/bin/setup-s3.sh"
   ```

3. The server configuration file contains a bucket-management identity and a
   separate test identity limited to harness buckets. The script verifies that
   the test identity can read/write a harness bucket but cannot create a bucket
   or access objects, listings or tags outside that scope. It writes the mode-0600
   `s3.conf` credential file only after that probe succeeds; the server configuration
   is created earlier so SeaweedFS can start. Load the path-only shell file in the test shell:

   ```bash
   source "$HOME/.local/share/charmqa-s3/s3.env"
   ```

   This exports `S3_CONFIG_localhost`, not credentials. For another logical Juju
   cloud, set `S3_CONFIG_<cloud>` to the config path, replacing hyphens with
   underscores in the variable name. Ambient `S3_*` credential variables are not
   used by the suite.

4. Run the suite through `scripts/run-tests.sh`. Its post-deploy extension finds
   s3-integrator on either substrate and creates a separate bucket per integrator.
   Use the VM address in the config's `S3_ENDPOINT`, not localhost or cluster-only DNS. Confirm
   that both Kubernetes pods and LXD units can reach port 8333.

The service stores data under `$HOME/.local/share/charmqa-s3/`, outside the source
tree. Setup restricts that directory to mode 0700 and existing credential files
to mode 0600 before reading them; symlinked or unowned credential paths are rejected.
Re-running setup keeps existing data and keys. An inactive existing service,
an unsupported older `s3.json` credential format, or a mismatched binary version
requires inspection rather than automatic replacement. Use
`sudo journalctl -u charmqa-s3` for diagnostics. If the VM address changes, inspect
the service bind address; setup reports an unreachable endpoint instead of
automatically rebinding a service that may have active consumers.

Normal suite teardown removes its buckets independently of controller cleanup.
To remove buckets left by interrupted local runs:

```bash
poetry run python scripts/cleanup-s3.py \
  --config "$HOME/.local/share/charmqa-s3/s3.conf" --age-minutes 240
```

Stop the service with `sudo systemctl stop charmqa-s3`. Do not delete its state
while consumers are using it. This single-node service is not production storage
and does not exercise Keystone authentication; shared Swift environments do.
