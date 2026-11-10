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

## Configuration

Set options in `development-sandbox/.env` (gitignored). See
[`.env.sample`](.env.sample) for all settings.

| Option | Purpose |
|---|---|
| `SANDBOX_VAR_GITHUB_TOKEN` | Fine-grained PAT; recommended permissions below |
| `SANDBOX_VAR_COPILOT_GITHUB_TOKEN` | Token for Copilot authentication |
| `SANDBOX_VAR_COPILOT_MODEL` | Optional model override; otherwise Copilot CLI uses its default |
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
New sandboxes include Go and uvx; install them manually in older VMs.

The root `.copilotignore` uses path-independent patterns for local credential
files. It does not protect process
environments or the sandbox's temporary MCP config outside the checkout.
Assume an unrestricted agent can read and use any credentials available to its
user, regardless of ignore files.

### GitHub

Built into Copilot CLI; no additional configuration needed.

## Copilot settings

To use a Copilot CLI `settings.json` for a run, set
`SANDBOX_COPILOT_SETTINGS_FILE` in `development-sandbox/.env`:

```
SANDBOX_COPILOT_SETTINGS_FILE=development-sandbox/copilot-settings.json
```

The file must contain a JSON object. It is copied to a temporary `COPILOT_HOME`
for each `sandbox.sh run` invocation, alongside the sandbox's Copilot and MCP
configuration, and removed when the run ends. The regular Copilot home is left
unchanged. `development-sandbox/copilot-settings.json` is gitignored.

For Auto routing with the efficiency profile, set
`SANDBOX_VAR_COPILOT_MODEL=auto` and use this settings file:

```json
{
  "model": "auto",
  "autoTier": "efficiency"
}
```

### Jira

Create a [Jira API token](https://id.atlassian.com/manage-profile/security/api-tokens)
and set `JIRA_USERNAME` to your Atlassian account email.

### Test Observer

Ask SQA for a Test Observer API token. The launcher requires authenticated `gh`
in the VM with read access to the private repository; the other prerequisites
are installed by default.
