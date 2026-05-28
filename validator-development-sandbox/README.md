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

1. Run `bin/up.sh` to provision or resume the VM. Run `bin/run.sh --interactive` to start a session, then use `/develop-validator` or `/test-validator` skill commands. For a fully autonomous one-shot run, pass a prompt directly: `bin/run.sh 'Use the /develop-validator skill to develop the s3 validator.'`.
2. Enter the VM with `bin/shell.sh` (or use the agent) and run `bin/verify-validator.sh --model <model> --app <requirer> --provider <provider> --validator <name>`.
3. Review `summary.txt`, `report.json`, and the per-command `*.out` files in
   `validator-development-sandbox/reports/<name>-<timestamp>/` (git-ignored, persists on host).
4. Clean up with `juju destroy-model <model> --destroy-storage --no-prompt`.

## Notes

- `VALIDATOR_VM` overrides the Multipass VM name.
- `COPILOT_MODEL` overrides the Copilot model.
- The project is mounted at `/project`.
- Python dependencies are managed with Poetry.
