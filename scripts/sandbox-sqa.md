# Solutions QA sandbox

Wraps the [dev sandbox](../development-sandbox/README.md) and sets up the SQA
tooling (Jira and Test Observer MCP) from your shell. No `.env` editing.

## Setup

```bash
export GITHUB_TOKEN=...                      # host only: registers the VM signing key
export SANDBOX_VAR_GITHUB_TOKEN=...          # gh in the VM, owner: canonical
export SANDBOX_VAR_COPILOT_GITHUB_TOKEN=...  # Copilot in the VM, owner: you
export TEST_OBSERVER_API_KEY=...             # ask SQA
export JIRA_API_TOKEN=...                    # JIRA_USERNAME defaults to git user.email

scripts/sandbox-sqa.sh up
```

Same subcommands as `scripts/sandbox.sh`.

## Tokens

Use two separate fine-grained PATs. Copilot permissions only exist on
user-owned tokens; everything else should be owned by `canonical`.

**`SANDBOX_VAR_GITHUB_TOKEN`**: owner `canonical`, all repositories. Write
for day-to-day dev, read-only for everything else.

- Read and write: Actions, Agent tasks, Commit statuses, Contents, Issues,
  Pull requests, Workflows
- Read-only: Artifact metadata, Attestations, Code quality, Code scanning
  alerts, Codespaces, Codespaces lifecycle admin, Codespaces metadata, Copilot
  agent settings, Custom properties, Dependabot alerts, Deployments,
  Discussions, Environments, License compliance alerts, Merge queues, Metadata,
  Pages, Repository security advisories, Secret scanning alerts, Variables,
  Webhooks

**`SANDBOX_VAR_COPILOT_GITHUB_TOKEN`**: owner: you, public repositories.
Account permissions, read-only: Copilot Chat, Copilot Editor Context, Copilot
Requests.

## What it does

- `up`, `run`, `shell`: checks both tokens are fine-grained, different, and
  accepted by GitHub. It can't check permissions; GitHub doesn't expose them.
- `up`, `run`: creates `development-sandbox/mcp.json` if missing.
- `up`, `run`, `shell`: syncs `.env` from your shell (only variables in
  `.env.sample`), so rotated tokens just work.
- Asks before writing either file and shows variable names, never values.

Handy after `git clean -fxd`. Export `SANDBOX_SIGNING_KEY` too, or you'll get a
new VM signing key to register.
