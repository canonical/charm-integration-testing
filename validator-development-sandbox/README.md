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

All tokens are optional. Set them in `validator-development-sandbox/.env`
(gitignored, copy from `.env.sample`).

- `GITHUB_TOKEN`: fine-grained PAT passed to `gh` CLI inside the VM. If not set, `gh` CLI uses whatever auth is already present in the VM. Recommended permissions: Contents (read), Pull requests (read and write); Repository access: All repositories.
- `COPILOT_GITHUB_TOKEN`: Copilot AI auth token. Defaults to `gh auth token` on the host.

## Notes

- `VALIDATOR_VM` overrides the Multipass VM name.
- `COPILOT_MODEL` overrides the Copilot model.
- The project is mounted at `/project`.
- Python dependencies are managed with Poetry.
