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
2. `scripts/sandbox.sh shell` - enter the VM and run `bin/setup-k8s.sh` (or `bin/setup-lxd.sh`) once to prepare the substrate, then bootstrap a Juju controller.
3. `scripts/sandbox.sh run --interactive` - use `/develop-validator` or `/test-validator` skills. The skills handle deploying charms and running quality gates.
4. Review results in `development-sandbox/reports/<name>-<timestamp>/` (gitignored, persists on the host).

## Token setup

All tokens are optional. Set them in `development-sandbox/.env`
(gitignored, copy from `.env.sample`).

- `GITHUB_TOKEN`: fine-grained PAT passed to `gh` CLI inside the VM. If not set, `gh` CLI uses whatever auth is already present in the VM. Recommended permissions: Contents (read), Pull requests (read and write); Repository access: All repositories.
- `COPILOT_GITHUB_TOKEN`: Copilot AI auth token. Defaults to `gh auth token` on the host.

## Notes

- `SANDBOX_VM` overrides the Multipass VM name (default: `charm-qa-sandbox`).
- `SANDBOX_MOUNT` overrides the VM-side mount path (default: `/project`). Set this to a unique path (e.g. `/project-fork`) when multiple repo clones share the same VM.
- `SANDBOX_CPUS` / `SANDBOX_MEMORY` / `SANDBOX_DISK` override VM resources at creation time (defaults: `4`, `8G`, `40G`). These can be set via environment variables or CLI flags (`--cpus`, `--memory`, `--disk`) and apply only when creating a new VM.
- `COPILOT_MODEL` overrides the Copilot model.
- Inside the VM the project is accessible via `$PROJECT_ROOT` (set automatically by the sandbox tooling to the VM-side mount path).
- Python dependencies are managed with Poetry.
- **nginx API cache:** auto-provisioned via `substrate.yaml` cloud-init, listening on `http://localhost:8080` (`/charmhub/`, `/snapcraft/`), caching responses for 4h.
- `CHARMHUB_API_URL` / `SNAPCRAFT_API_URL`: override `bundle_builder_x`'s API base URLs, e.g. point at the nginx cache above. Passed through into the VM by `sandbox.sh`. See `.env.sample`.

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

