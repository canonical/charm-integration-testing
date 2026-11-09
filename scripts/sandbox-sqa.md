# Solutions QA sandbox

`scripts/sandbox-sqa.sh` starts the [development sandbox](../development-sandbox/README.md)
with the tooling Solutions QA recommends, including the Jira and Test Observer
[MCP servers](../development-sandbox/README.md#recommended-mcp-servers).
No manual configuration is required.

## 1. Export your credentials

| Variable | Purpose |
|---|---|
| `GITHUB_TOKEN` | Used on the host to register the VM's commit signing key with your GitHub account. Needs permission to manage SSH signing keys. |
| `SANDBOX_VAR_GITHUB_TOKEN` | Fine-grained PAT for the `gh` CLI inside the sandbox. Grant only the permissions you want available to you and the agent; see [Configuration](../development-sandbox/README.md#configuration). |
| `SANDBOX_VAR_COPILOT_GITHUB_TOKEN` | Fine-grained PAT with the **Copilot Requests** permission, used by Copilot CLI inside the sandbox. |
| `TEST_OBSERVER_API_KEY` | API key for the Test Observer MCP server. |
| `JIRA_API_TOKEN` | API token for the Jira MCP server. Your Jira username defaults to `git config user.email`; set `JIRA_USERNAME` to override it. |

```bash
export GITHUB_TOKEN=<token>
export SANDBOX_VAR_GITHUB_TOKEN=<token>
export SANDBOX_VAR_COPILOT_GITHUB_TOKEN=<token>
export TEST_OBSERVER_API_KEY=<key>
export JIRA_API_TOKEN=<token>
```

## 2. Start the sandbox

Run from the repository root:

```bash
scripts/sandbox-sqa.sh up
```

Before starting the VM, `up`:

- generates `development-sandbox/mcp.json` from your credentials, if it does
  not already exist, and
- writes the variables listed in
  [`.env.sample`](../development-sandbox/.env.sample) (such as the GitHub
  tokens above) to `development-sandbox/.env`. If a `.env` already exists, it
  also lists the variable names that would be removed or added.

Before writing either file, `up` lists the variable names (never values) it
will inject into the sandbox and asks for your consent. Declining `mcp.json`
stops `up`; declining `.env` leaves it as is.

All other subcommands (`down`, `destroy`, `shell`, `run`) behave as in
`scripts/sandbox.sh`.
