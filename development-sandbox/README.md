# Development Sandbox

A Multipass VM for developing and testing charm integration validators and other charm-integration-testing tasks.

## Commands

### Host-side

Run host operations through the repository's mise tasks:
install the [development toolchain](../CONTRIBUTING.md#general-recommendations)
first.

| Command | Purpose |
|---|---|
| `mise run sandbox -- up` | Create or resume the VM and install deps |
| `mise run sandbox -- down` | Stop the VM |
| `mise run sandbox -- destroy` | Delete the VM |
| `mise run sandbox -- shell` | Open a shell inside the VM |
| `mise run sandbox -- run 'task'` | Launch Copilot autonomously |
| `mise run sandbox -- run --interactive` | Launch Copilot interactively |

Skills (`/develop-validator`, `/test-validator`, `/setup-k8s`, `/setup-lxd`, `/review-pr`) are
auto-discovered via `.agents/skills/` at the repository root.

### VM-side

Run these inside the VM after entering with `mise run sandbox -- shell` or by the agent.

| Command | Purpose |
|---|---|
| `mise run sandbox:setup-k8s` | Install Canonical k8s and register it as a Juju cloud |
| `mise run sandbox:setup-chaos-mesh` | Install or verify Chaos Mesh on an existing sandbox cluster |
| `mise run sandbox:setup-lxd` | Install/initialize LXD for Juju's built-in `localhost` cloud |
| `mise run sandbox:verify-validator --` | Run quality gates and workload-up/down evidence |
| `mise run sandbox:dev-validate --` | Inject validators into a unit and report results |

## Typical flow

1. `mise run sandbox -- up` - provision or resume the VM.
2. `mise run sandbox -- run --interactive` - use `/develop-validator` or `/test-validator` skills. The skills handle deploying charms and running quality gates.
3. Review results in `development-sandbox/reports/<name>-<timestamp>/` (gitignored, persists on the host).

## Chaos Mesh

Sandbox Kubernetes setup includes Chaos Mesh. It installs the components but
does not run chaos experiments. Shared CI clusters are provisioned separately.

See the [setup-k8s skill](../.agents/skills/setup-k8s/SKILL.md) for installation
and checks, and [setup-charm-tests](../.agents/skills/setup-charm-tests/SKILL.md)
for test prerequisites.

## Test controller disks

Test controllers in the sandbox VM should use a 5 GB root disk (`root-disk=5G`),
whether hosted on LXD or Kubernetes. This matches the sizing used by Kubernetes
and PS7 OpenStack environments in [`static/environments.yaml`](../static/environments.yaml).
For suite-managed
controllers, pass `--target-controller-bootstrap-constraints '{"root-disk":"5G"}'`
and, for a separate neighbor controller,
`--neighbor-controller-bootstrap-constraints '{"root-disk":"5G"}'`.
For manual bootstrap:

```bash
juju bootstrap localhost <controller-name> --bootstrap-constraints root-disk=5G
juju bootstrap local-k8s <controller-name> --bootstrap-constraints root-disk=5G
```

## Token setup

Set tokens in `development-sandbox/.env` (gitignored, copy from `.env.sample`).
`SANDBOX_VAR_GITHUB_TOKEN` and `SANDBOX_VAR_COPILOT_GITHUB_TOKEN` are optional. If either is unset and the host has a `gh auth token`, `sandbox.sh` warns about the risk and asks for your consent before passing that token into the VM. If there is no host token, or you decline (including when there is no terminal), the sandbox still starts but `gh` and Copilot in the VM are unauthenticated for the unset tokens. The safer option is to set narrowly scoped fine-grained PATs.

- `GITHUB_TOKEN`: fine-grained PAT for host-side GitHub API calls when provisioning the VM (e.g. registering the VM signing key). Optional - falls back to the host's `gh auth token` if not set. Automatic signing-key verification and registration require the `write:ssh_signing_key` scope (checking existing keys, and registering a new one); if your `gh` login lacks it, `sandbox.sh up` warns and asks for your consent before running `gh auth refresh` to grant it (or add the key manually at <https://github.com/settings/ssh>).
- `SANDBOX_VAR_GITHUB_TOKEN`: fine-grained PAT passed to the `gh` CLI inside the VM (as both `GH_TOKEN` and `GITHUB_TOKEN`). When a token is available, `up`/`run` log `gh` in with it, overwriting any existing VM login; otherwise `gh` auth is skipped. Falls back to the host token only with your consent. Recommended permissions: Contents (read), Pull requests (read and write); Repository access: All repositories.
- `SANDBOX_VAR_COPILOT_GITHUB_TOKEN`: Copilot AI auth token inside the VM. Falls back to the host's `gh auth token` only with your consent.

## Notes

- `SANDBOX_VM` overrides the Multipass VM name (default: `charm-qa-sandbox`).
- `SANDBOX_MOUNT` overrides the VM-side mount path (default: `/project`). Set this to a unique path (e.g. `/project-fork`) when multiple repo clones share the same VM.
- `SANDBOX_CPUS` / `SANDBOX_MEMORY` / `SANDBOX_DISK` override VM resources at creation time (defaults: `4`, `8G`, `40G`). These can be set via environment variables or CLI flags (`--cpus`, `--memory`, `--disk`) and apply only when creating a new VM.
- Inside the VM the project is accessible via `$PROJECT_ROOT` (set automatically by the sandbox tooling to the VM-side mount path).
- mise loads the shared `../mise.toml`, the `../mise.development.toml` overlay,
  and sandbox extras from `mise.toml` in this directory. The VM sets
  `MISE_ENV=development` and
  `MISE_OVERRIDE_CONFIG_FILENAMES=mise.toml:development-sandbox/mise.toml`
  so these extras stay available while working from the project root.
  Poetry manages Python dependencies.
- **Migrating from validator-development-sandbox:** rename `VALIDATOR_VM` to `SANDBOX_VM` and `VALIDATOR_SIGNING_KEY` to `SANDBOX_SIGNING_KEY` in your `.env`.
- `up` launches a plain Ubuntu VM, installs missing bootstrap prerequisites
  (curl, CA certificates, and Git), mounts the project, and runs mise setup.
- Any host env var prefixed `SANDBOX_VAR_` is passed into the VM with the prefix stripped, e.g. `SANDBOX_VAR_CHARMHUB_API_URL` / `SANDBOX_VAR_SNAPCRAFT_API_URL` override `bundle_builder_x`'s API base URLs inside the VM as `CHARMHUB_API_URL` / `SNAPCRAFT_API_URL`, and `SANDBOX_VAR_COPILOT_MODEL` overrides the Copilot model inside the VM. Overrides must be reachable from the VM; no API cache is provisioned. See `.env.sample`.

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
