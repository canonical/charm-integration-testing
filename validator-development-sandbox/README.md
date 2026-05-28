# Validator Development Sandbox

Quick reference for the VM used to build and verify charm integration validators.

## Commands

### Host-side

Run these from your normal terminal on the host machine.

| Command | Purpose |
|---|---|
| `bin/up.sh` | Create or resume the VM and install deps |
| `bin/down.sh` | Stop the VM |
| `bin/destroy.sh` | Delete the VM |
| `bin/shell.sh` | Open a shell inside the VM |
| `bin/run.sh` | Launch Copilot in the VM (`--interactive` or a freeform prompt) |

Skills (`/develop-validator`, `/test-validator`) are auto-discovered via `.agents/skills/`
(a symlink to `validator-development-sandbox/prompts/`). Use them directly inside any
interactive Copilot session.

### VM-side

Run these inside the VM after entering it with `bin/shell.sh` or by the agent.

| Command | Purpose |
|---|---|
| `bin/verify-validator.sh` | Run quality gates plus workload-up/down evidence |
| `bin/dev-validate.py` | Inject validators into a unit and report results |

## Typical flow

1. Run `bin/up.sh` to provision or resume the VM.
2. Run `bin/run.sh --interactive`, then use `/develop-validator` or `/test-validator` to develop and verify a validator. The skills handle the full workflow including deploying charms and running quality gates.
3. Review results in `validator-development-sandbox/reports/<name>-<timestamp>/` (gitignored, persists on the host).

## Token setup

By default `run.sh` uses your full host `gh auth` token for both Copilot AI features
and `gh` CLI calls inside the VM. That token can push code.

To restrict `gh` CLI to read-only + PR comment access, create a fine-grained PAT and
add it to `validator-development-sandbox/.env` (gitignored):

```sh
# validator-development-sandbox/.env
GITHUB_TOKEN=github_pat_...
```

Fine-grained PAT permissions required:

| Permission | Level | Reason |
|---|---|---|
| Contents | Read | Read repo source files |
| Pull requests | Read and write | Read and reply to Copilot review comments |

Set "Repository access" to "All repositories" to cover every repo you can access.

`run.sh` passes your full host token as `COPILOT_GITHUB_TOKEN` (Copilot AI auth only)
and the fine-grained PAT as `GH_TOKEN`/`GITHUB_TOKEN` (used by `gh` CLI). The Copilot
binary reads `COPILOT_GITHUB_TOKEN` first and ignores `GH_TOKEN`, so the two tokens
are fully isolated.

## Notes

- `VALIDATOR_VM` overrides the Multipass VM name.
- `COPILOT_MODEL` overrides the Copilot model.
- The project is mounted at `/project`.
- Python dependencies are managed with Poetry.
