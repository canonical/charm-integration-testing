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
| `bin/run.sh` | Launch Copilot in the VM (`--interactive`, `--task develop/test <name>`, or both) |

### VM-side

Run these inside the VM after entering it with `bin/shell.sh` or by the agent.

| Command | Purpose |
|---|---|
| `bin/verify-validator.sh` | Run quality gates plus workload-up/down evidence |
| `bin/dev-validate.py` | Inject validators into a unit and report results |

## Typical flow

1. Run `bin/up.sh` to provision or resume the VM. Use `bin/run.sh --task develop <name>` for fully autonomous development, `bin/run.sh --task develop <name> --interactive` to step through with check-ins, or `bin/run.sh --interactive` for a free-form session.
2. Enter the VM with `bin/shell.sh` (or use the agent) and run `bin/verify-validator.sh --model <model> --app <requirer> --provider <provider> --validator <name>`.
3. Review `summary.txt`, `report.json`, and the per-command `*.out` files in `/tmp/validator-verification-*`.
4. Clean up with `juju destroy-model <model> --destroy-storage --no-prompt`.

## Notes

- `VALIDATOR_VM` overrides the Multipass VM name.
- `COPILOT_MODEL` overrides the Copilot model.
- The project is mounted at `/project`.
- Python dependencies are managed with Poetry.
