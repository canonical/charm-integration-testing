# Development Sandbox

A Multipass VM for developing and testing charm integration validators and other charm-integration-testing tasks.

## Commands

### Host-side

All host operations go through a single entry point:

| Command | Purpose |
|---|---|
| `scripts/sandbox.sh up` | Create or resume the VM and install deps |
| `scripts/sandbox.sh down` | Stop the VM |
| `scripts/sandbox.sh destroy` | Delete the VM |
| `scripts/sandbox.sh shell` | Open a shell inside the VM |
| `scripts/sandbox.sh sync push` | Push local changes to remote host (remote mode) |
| `scripts/sandbox.sh sync pull` | Pull remote changes to local (remote mode) |
| `scripts/sandbox.sh run 'task'` | Launch Copilot autonomously |
| `scripts/sandbox.sh run --interactive` | Launch Copilot interactively |

Skills (`/develop-validator`, `/test-validator`, `/setup-k8s`, `/setup-lxd`) are
auto-discovered via `.agents/skills/` (a symlink to `development-sandbox/prompts/`).
Use them inside any interactive Copilot session.

### VM-side

Run these inside the VM after entering with `scripts/sandbox.sh shell` or by the agent.

| Command | Purpose |
|---|---|
| `bin/setup-k8s.sh` | Install Canonical k8s and register as Juju cloud |
| `bin/setup-lxd.sh` | Install/initialize LXD for Juju's built-in `localhost` cloud |
| `bin/verify-validator.sh` | Run quality gates and workload-up/down evidence |
| `bin/dev-validate.py` | Inject validators into a unit and report results |

## Typical flow

1. `scripts/sandbox.sh up` - provision or resume the VM.
2. `scripts/sandbox.sh shell` - enter the VM and run `bin/setup-k8s.sh` (or `bin/setup-lxd.sh`) once to prepare the substrate, then bootstrap a Juju controller.
3. `scripts/sandbox.sh run --interactive` - use `/develop-validator` or `/test-validator` skills. The skills handle deploying charms and running quality gates.
4. Review results in `development-sandbox/reports/<name>-<timestamp>/` (gitignored, persists on the host).

## Token setup

All tokens are optional. Set them in `development-sandbox/.env`
(gitignored, copy from `.env.sample`).

- `GITHUB_TOKEN`: fine-grained PAT passed to `gh` CLI inside the VM. If not set, `gh` CLI uses whatever auth is already present in the VM. Recommended permissions: Contents (read), Pull requests (read and write); Repository access: All repositories.
- `COPILOT_GITHUB_TOKEN`: Copilot AI auth token. Defaults to `gh auth token` on the host.

## Remote mode

The sandbox VM can be created on a remote machine instead of locally.
This is useful when you want to run the VM on a more powerful host, a
different architecture, or a shared team server.

1. Ensure you have passwordless SSH access to the remote (e.g. `ssh user@remote-box`).
2. Install Multipass on the remote: `sudo snap install multipass`.
3. Add to `development-sandbox/.env`:
   ```
   SANDBOX_HOST=user@remote-box
   # optional: override where the project lands on the remote
   # SANDBOX_HOST_DIR=/tmp/sandbox-project
   ```
4. Run `scripts/sandbox.sh up` as usual — the script syncs the project via
   `rsync`, creates the VM on the remote, and tunnels all commands over SSH.

Use `scripts/sandbox.sh sync push` and `scripts/sandbox.sh sync pull` to
manually transfer changes between local and remote between `up` calls.

## Notes

- `SANDBOX_VM` overrides the Multipass VM name (default: `charm-qa-sandbox`).
- `SANDBOX_MOUNT` overrides the VM-side mount path (default: `/project`). Set this to a unique path (e.g. `/project-fork`) when multiple repo clones share the same VM.
- `SANDBOX_HOST` offloads the VM to a remote host (see Remote mode above).
- `SANDBOX_HOST_DIR` overrides the project sync path on the remote (default: `/tmp/sandbox-project`).
- `COPILOT_MODEL` overrides the Copilot model.
- Inside the VM the project is accessible via `$PROJECT_ROOT` (set automatically by the sandbox tooling to the VM-side mount path).
- Python dependencies are managed with Poetry.
- **Migrating from validator-development-sandbox:** rename `VALIDATOR_VM` to `SANDBOX_VM` and `VALIDATOR_SIGNING_KEY` to `SANDBOX_SIGNING_KEY` in your `.env`.
