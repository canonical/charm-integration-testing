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
| `scripts/sandbox.sh run 'task'` | Launch Copilot autonomously |
| `scripts/sandbox.sh run --interactive` | Launch Copilot interactively |
| `scripts/sandbox.sh run --with-test-observer-mcp ...` | Also launch `test-observer-mcp` for this session |

Skills (`/develop-validator`, `/test-validator`, `/setup-k8s`, `/setup-lxd`, `/review-pr`) are
auto-discovered via `.agents/skills/` at the repository root.

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
2. `scripts/sandbox.sh run --interactive` - use `/develop-validator` or `/test-validator` skills. The skills handle deploying charms and running quality gates.
3. Review results in `development-sandbox/reports/<name>-<timestamp>/` (gitignored, persists on the host).

## Token setup

All tokens are optional. Set them in `development-sandbox/.env`
(gitignored, copy from `.env.sample`).

- `GITHUB_TOKEN`: fine-grained PAT for host-side GitHub API calls when provisioning the VM (e.g. registering the VM signing key). Optional - falls back to the host's `gh auth token` if not set. Automatic signing-key verification and registration require the `admin:ssh_signing_key` scope; if your `gh` login lacks it, `sandbox.sh up` warns and asks for your consent before running `gh auth refresh` to grant it (or add the key manually at <https://github.com/settings/ssh>).
- `SANDBOX_VAR_GITHUB_TOKEN`: fine-grained PAT passed to the `gh` CLI inside the VM (as both `GH_TOKEN` and `GITHUB_TOKEN`). If not set, `gh` CLI inside the VM uses whatever auth is already present there. Recommended permissions: Contents (read), Pull requests (read and write); Repository access: All repositories.
- `SANDBOX_VAR_COPILOT_GITHUB_TOKEN`: Copilot AI auth token inside the VM. Defaults to `gh auth token` on the host.

## Notes

- `SANDBOX_VM` overrides the Multipass VM name (default: `charm-qa-sandbox`).
- `SANDBOX_MOUNT` overrides the VM-side mount path (default: `/project`). Set this to a unique path (e.g. `/project-fork`) when multiple repo clones share the same VM.
- `SANDBOX_CPUS` / `SANDBOX_MEMORY` / `SANDBOX_DISK` override VM resources at creation time (defaults: `4`, `8G`, `40G`). These can be set via environment variables or CLI flags (`--cpus`, `--memory`, `--disk`) and apply only when creating a new VM.
- Inside the VM the project is accessible via `$PROJECT_ROOT` (set automatically by the sandbox tooling to the VM-side mount path).
- Python dependencies are managed with Poetry.
- **Migrating from validator-development-sandbox:** rename `VALIDATOR_VM` to `SANDBOX_VM` and `VALIDATOR_SIGNING_KEY` to `SANDBOX_SIGNING_KEY` in your `.env`.
- **nginx API cache:** auto-provisioned via `substrate.yaml` cloud-init, listening on `http://localhost:8080` *inside the VM* (`/charmhub/`, `/snapcraft/`), caching responses for 4h.
- Any host env var prefixed `SANDBOX_VAR_` is passed into the VM with the prefix stripped, e.g. `SANDBOX_VAR_CHARMHUB_API_URL` / `SANDBOX_VAR_SNAPCRAFT_API_URL` override `bundle_builder_x`'s API base URLs inside the VM as `CHARMHUB_API_URL` / `SNAPCRAFT_API_URL` (e.g. point at the nginx cache above), and `SANDBOX_VAR_COPILOT_MODEL` overrides the Copilot model inside the VM. See `.env.sample`.

## MCP servers

To inject custom MCP servers into `sandbox.sh run` sessions, set `SANDBOX_MCP_CONFIG_FILE`
in `development-sandbox/.env` to the path of an MCP server config JSON file on the host:

```
SANDBOX_MCP_CONFIG_FILE=development-sandbox/mcp.json
```

The file is copied into the VM and passed to Copilot via `--additional-mcp-config`.
It is removed from the VM after the session ends.

`development-sandbox/mcp.json` is gitignored, so creating it there is the recommended
convention. The format is the standard Copilot MCP config:

```json
{
  "mcpServers": {
    "my-server": {
      "command": "npx",
      "args": ["-y", "@my/mcp-server"]
    }
  }
}
```

MCP config applies to both `sandbox.sh run 'task'` and `sandbox.sh run --interactive`.
It has no effect on `up`, `shell`, `down`, or `destroy`.

### test-observer-mcp

Pass `--with-test-observer-mcp` to `scripts/sandbox.sh run` to get an ad-hoc, per-session
[`test-observer-mcp`](https://github.com/canonical/test-observer-mcp) server, so Copilot can
query real Test Observer data (test executions, results, artefacts, reports, and juju logs)
during the session. On each `run` invocation this:

1. Clones `canonical/test-observer-mcp` into `.test-observer-mcp/` **on the host**, over SSH,
   using the host's own `ssh-agent`/keys (the VM has no access to the host's SSH identity, and
   `test-observer-mcp` may not be public, so this can't be done with a VM-side `gh`/token
   clone). This directory is project-root-relative and already bind-mounted into the VM via
   `multipass mount`, so no separate transfer step is needed; it is gitignored and wiped before
   each clone.
2. Builds and starts it with `./scripts/run.sh` on `TEST_OBSERVER_MCP_PORT` (default `8090`;
   deliberately not `8080`, which the nginx API cache already uses inside the VM).
3. Waits for its `/health` endpoint, then registers it with Copilot for this session only, via
   the same `--additional-mcp-config` mechanism as `SANDBOX_MCP_CONFIG_FILE`.
4. Stops the server when the session ends (the `.test-observer-mcp/` clone remains on disk and is wiped before the next clone).

Requires a host SSH key registered with GitHub with access to `canonical/test-observer-mcp`,
and `TEST_OBSERVER_API_URL` (and optionally `TEST_OBSERVER_API_KEY` for write operations) in
`development-sandbox/.env`. See `.env.sample`.

```
scripts/sandbox.sh run --with-test-observer-mcp 'investigate why test_deploy keeps failing for pgbouncer-k8s'
scripts/sandbox.sh run --interactive --with-test-observer-mcp
```

