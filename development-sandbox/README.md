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

| Command | Purpose |
|---|---|
| `scripts/sandbox.sh run 'task'` | Run a task autonomously |
| `scripts/sandbox.sh shell` | Open a VM shell |
| `scripts/sandbox.sh down` | Stop the VM, preserving its state |
| `scripts/sandbox.sh up` | Create or resume the VM and install dependencies |
| `scripts/sandbox.sh destroy` | Permanently delete the VM |

## VM setup

`up` installs the sandbox, development, integration, and agent tool groups.

`scripts/sandbox.sh up`, `run`, and `shell` set `UV_PROJECT_ENVIRONMENT` to store
Python dependencies under
`~/.local/share/charm-integration-testing/venvs/<checkout-path-hash>` (or
`$XDG_DATA_HOME/charm-integration-testing/venvs/<checkout-path-hash>` when set).
The hash uses the canonical VM checkout path, so different mounts have separate
environments. Each shell or agent session keeps the environment for its mount;
use the other checkout's `scripts/sandbox.sh shell` or `run` to work on that project.
Host commands continue using the checkout's `.venv`; sandbox setup leaves it
untouched. Run `scripts/sandbox.sh up` again to configure an existing sandbox.

Install substrate prerequisites separately, then run the provisioning task.
Provisioning does not install or refresh packages.

| Command | Purpose |
|---|---|
| `mise run setup -- --group local-k8s` | Install Kubernetes prerequisites |
| `mise run setup -- --group local-lxd` | Install LXD prerequisites |
| `mise run sandbox:setup-k8s` | Provision Canonical k8s and register it as a Juju cloud |
| `mise run sandbox:setup-chaos-mesh` | Install or verify Chaos Mesh on an existing cluster |
| `mise run sandbox:setup-lxd` | Initialize LXD for Juju's `localhost` cloud |

To select another Juju client, run `mise run juju:channel -- 4/stable`.
The selection is saved outside the checkout and respected by substrate setup.
See [Juju client selection](../CONTRIBUTING.md#selecting-the-juju-client)
for reset commands and controller-version cautions.

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
Sandbox setup includes Go and uvx; rerun `scripts/sandbox.sh up` to prepare them
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
