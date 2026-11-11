# Development Sandbox

A Multipass VM with Copilot and development tools for charm integration testing.
Your checkout is mounted into the VM, so edits are visible on the host.

## Quick start

On an amd64 host with [Multipass](https://canonical.com/multipass) and
[GitHub CLI](https://cli.github.com/) installed, run from the repository root:

```bash
cp development-sandbox/.env.sample development-sandbox/.env
# Configure credentials and optional settings in .env.
scripts/sandbox.sh up
scripts/sandbox.sh run --interactive
```

Ask Copilot to develop or test a validator, reproduce a failure, or review a PR.
Skills such as `/setup-k8s`, `/setup-lxd`, `/develop-validator`, and `/test-validator`
handle substrate setup and testing.

With Mise installed on the host, `mise run sandbox -- up` and
`mise run sandbox -- run --interactive` provide the same interface. The direct
launcher does not require Mise on the host.

The `sandbox` task manages the host VM and delegates Copilot execution to the
`agent` task inside it. The agent task can also run directly on a prepared Ubuntu host:

```bash
mise run setup -- --group agent
mise run agent -- "Describe the task"
```

Run `mise run sandbox -- --help` for VM lifecycle and agent options.

## VM setup

`up` prepares the VM toolchain from the repository catalogue.

`mise run sandbox -- up`, `run`, and `shell` set `UV_PROJECT_ENVIRONMENT` to store
Python dependencies under
`~/.local/share/charm-integration-testing/venvs/<checkout-path-hash>` (or
`$XDG_DATA_HOME/charm-integration-testing/venvs/<checkout-path-hash>` when set).
The hash uses the canonical VM checkout path, so different mounts have separate
environments. Each shell or agent session keeps the environment for its mount;
use the other checkout's `mise run sandbox -- shell` or `run` to work on that project.
Host commands continue using the checkout's `.venv`; sandbox setup leaves it
untouched. Run `mise run sandbox -- up` again to configure an existing sandbox.

For substrate prerequisites and provisioning, follow the
[Kubernetes](../.agents/skills/setup-k8s/SKILL.md) or
[LXD](../.agents/skills/setup-lxd/SKILL.md) setup steps. The shared provisioning
tasks use Concierge and can also run on Ubuntu hosts outside the sandbox.
For Chaos Mesh on an existing cluster, follow the
[existing-cluster procedure](../.agents/skills/setup-k8s/SKILL.md#set-up-chaos-mesh-on-an-existing-cluster).

To select another Juju client before provisioning, follow
[Juju client selection](../CONTRIBUTING.md#selecting-the-juju-client).

## Configuration

Set options in `development-sandbox/.env` (gitignored). See
[`.env.sample`](.env.sample) for all settings.

| Option | Purpose |
|---|---|
| `SANDBOX_VAR_GITHUB_TOKEN` | Fine-grained PAT; recommended permissions below |
| `SANDBOX_VAR_COPILOT_GITHUB_TOKEN` | Token for Copilot authentication |
| `SANDBOX_VAR_COPILOT_MODEL` | Override the Copilot model |
| `SANDBOX_CPUS` / `SANDBOX_MEMORY` / `SANDBOX_DISK` | VM sizing at creation (defaults: `8` / `16G` / `80G`) |
| `SANDBOX_VM` / `SANDBOX_MOUNT` | VM name and mount path; use a unique mount path for each checkout sharing a VM |

For read-only tasks, limit `SANDBOX_VAR_GITHUB_TOKEN` to the repositories you need:

| Repository permission | Access |
|---|---|
| Contents | Read-only |
| Issues | Read-only |
| Pull requests | Read-only |
| Actions | Read-only |

Grant write access only for tasks that need it: Pull requests for posting reviews,
Issues for filing bugs, and Contents for pushing changes.

Without dedicated tokens, the script asks before using your host `gh` token in the
VM. That token carries your host permissions; prefer narrowly scoped tokens.
Declining leaves the corresponding tools unauthenticated.

Host-side GitHub API calls use `GITHUB_TOKEN` or the host's `gh` token. Automatic
SSH signing-key registration requires the `write:ssh_signing_key` scope.

## Chaos Mesh

Sandbox Kubernetes setup installs Chaos Mesh components but does not run chaos
experiments. Shared CI clusters are provisioned separately.

## Recommended MCP servers

Copy the optional [sample](mcp.json.sample), replace the credential placeholders,
and remove any servers you do not need:

```bash
cp development-sandbox/mcp.json.sample development-sandbox/mcp.json
```

The sample uses `{"mcpServers": {...}}`; add separately named entries for other
services or environments, such as Test Observer staging, with their own endpoint
and token. Do not commit credentials. Set this in `development-sandbox/.env`:

```
SANDBOX_MCP_CONFIG_FILE=development-sandbox/mcp.json
```

This applies to both interactive and autonomous `run` sessions.
Sandbox setup includes Go and uvx; rerun `mise run sandbox -- up` to prepare them
in older VMs.

Any host environment variable prefixed with `SANDBOX_VAR_` is passed into the VM
with that prefix removed. API overrides must be reachable from inside the VM.

The root `.copilotignore` uses path-independent patterns for local credential
files. It does not protect process
environments or the sandbox's temporary MCP config outside the checkout.
Assume an unrestricted agent can read and use any credentials available to its
user, regardless of ignore files.

### GitHub

Built into Copilot CLI; no additional configuration needed.

### Jira

Create a [Jira API token](https://id.atlassian.com/manage-profile/security/api-tokens)
and set `JIRA_USERNAME` to your Atlassian account email.

### Test Observer

Ask SQA for a Test Observer API token. The launcher requires authenticated `gh`
in the VM with read access to the private repository; the other prerequisites
are installed by default.
