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

Skills (`/develop-charm`, `/develop-validator`, `/test-validator`, `/setup-k8s`, `/setup-lxd`, `/review-pr`) are
auto-discovered via `.agents/skills/` at the repository root.

### VM-side

Run these inside the VM after entering with `scripts/sandbox.sh shell` or by the agent.

| Command | Purpose |
|---|---|
| `bin/setup-k8s.sh` | Install Canonical k8s and Chaos Mesh, and register as Juju cloud |
| `bash bin/setup-chaos-mesh.sh` | Install or verify Chaos Mesh on an existing sandbox cluster |
| `bin/setup-lxd.sh` | Install/initialize LXD for Juju's built-in `localhost` cloud |
| `bin/verify-validator.sh` | Run quality gates and workload-up/down evidence |
| `bin/dev-validate.py` | Inject validators into a unit and report results |

## Typical flow

1. `scripts/sandbox.sh up` - provision or resume the VM.
2. `scripts/sandbox.sh run --interactive` - use `/develop-validator` or `/test-validator` for validator work. For a change to a charm project, use `/develop-charm`.
3. Review results in `development-sandbox/reports/<name>-<timestamp>/` (gitignored, persists on the host).

## Working on a charm

For charm maintenance work, start an interactive session and use `/develop-charm`.
The skill helps make a requested change in a charm repository, run its documented
checks, and record the results and evidence. It can clone a remote charm repository
into `development-sandbox/workspaces/` or work with an existing checkout there.
These workspaces are ignored by Git and persist on the host independently of the VM.

Reports belong in `development-sandbox/reports/` and include the charm revision,
the commands and environments tested, results, and links or paths to captured
logs. They describe evidence for the scenarios actually run, not an overall
quality score.

The integration test suite can resolve charm releases from Charmhub or deploy
the exact local `.charm` artifact. A run against a published charm revision
does not verify unmerged local changes. Report local-source integration
coverage only when that exact artifact was deployed; otherwise mark it as not run.

To run the integration suite against a locally built artifact, pack the charm
inside the sandbox, then pass its `.charm` file with `--target-charm-file`
alongside the matching `--target-charm` name. The target uses the artifact's
metadata-selected base; CharmHub neighbors resolve independently using their
own Charmhub defaults. The suite unpacks the artifact into a project-scoped
content-addressed cache.
Snap-installed Juju uses `~/snap/juju/common/charm-artifacts/`, namespaced
by mounted project path; without the Juju snap the cache is under the project's
`.pytest_cache/charm-artifacts/`. Generated bundles are stored separately by
project, controller, and model so concurrent models do not overwrite each
other's bundles and a resumed run can reuse them. Local targets can be tested
against a historical CharmHub revision or a second local artifact.
See the [run-charm-tests skill](../.agents/skills/run-charm-tests/SKILL.md)
for the full test arguments.

## Chaos Mesh

Sandbox Kubernetes setup includes Chaos Mesh. It installs the components but
does not run chaos experiments. Shared CI clusters are provisioned separately.

See the [setup-k8s skill](../.agents/skills/setup-k8s/SKILL.md) for installation
and checks, and [setup-charm-tests](../.agents/skills/setup-charm-tests/SKILL.md)
for test prerequisites.

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
