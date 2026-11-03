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
| `SANDBOX_VAR_GITHUB_TOKEN` | Fine-grained PAT with read-only Contents, Issues, Pull requests, and Actions permissions on the repositories you need |
| `SANDBOX_VAR_COPILOT_GITHUB_TOKEN` | Token for Copilot authentication |
| `SANDBOX_VAR_COPILOT_MODEL` | Override the Copilot model |
| `SANDBOX_CPUS` / `SANDBOX_MEMORY` / `SANDBOX_DISK` | VM sizing at creation (defaults: `4` / `8G` / `40G`) |
| `SANDBOX_VM` / `SANDBOX_MOUNT` | VM name and mount path; use a unique mount path for each checkout sharing a VM |

Without dedicated tokens, the script asks before using your host `gh` token in the
VM. That token carries your host permissions; prefer narrowly scoped tokens.
Declining leaves the corresponding tools unauthenticated.

## Recommended MCP servers

Save the configuration below as `development-sandbox/mcp.json` (gitignored), replace
`***` with your credentials, and add this to `development-sandbox/.env`:

```
SANDBOX_MCP_CONFIG_FILE=development-sandbox/mcp.json
```

This applies to both interactive and autonomous `run` sessions. GitHub MCP is
built into Copilot CLI. Go and uvx are included in new sandboxes; the Test Observer
launcher also needs Git and authenticated `gh` in the VM. Do not commit credentials.

```json
{
  "mcpServers": {
    "jira": {
      "command": "uvx",
      "args": ["mcp-atlassian"],
      "env": {
        "JIRA_URL": "https://warthogs.atlassian.net",
        "JIRA_USERNAME": "***",
        "JIRA_API_TOKEN": "***"
      }
    },
    "test-observer-stdio": {
      "type": "stdio",
      "command": "bash",
      "args": [
        "-euc",
        "dir=\"$HOME/.cache/test-observer-mcp\"; mkdir -p \"$(dirname \"$dir\")\"; if [ ! -d \"$dir/.git\" ]; then GIT_CONFIG_GLOBAL=/dev/null git -c credential.helper='!gh auth git-credential' clone https://github.com/canonical/test-observer-mcp.git \"$dir\" >&2; fi; cd \"$dir\"; exec go run ./cmd/server --transport stdio"
      ],
      "env": {
        "TEST_OBSERVER_API_URL": "https://test-observer-api.canonical.com",
        "TEST_OBSERVER_API_KEY": "***"
      },
      "timeout": 120000
    }
  }
}
```
