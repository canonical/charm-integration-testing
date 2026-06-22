---
name: test-validator
description: Test an existing Juju charm integration validator against a deployed charm. Use when asked to test, verify, or run an existing validator.
---

# Task: test an existing validator

## Goal

Deploy a minimal charm bundle that exercises the target validator and confirm
it passes at the requested level. Report any failures with diagnostic detail.

## Steps

1. Identify which validator to test from `validators/<name>/pyproject.toml`
   (look at `[project.entry-points."endpoint_validators"]` for the interface name).

2. Write `/tmp/spec.yaml` with a minimal two-charm deployment that creates
   a relation on that interface. Use a dedicated model name like `<interface>-test`.

3. Create the model, generate, and deploy the bundle:
   ```
   juju add-model <interface>-test
   bundle-builder-x --spec /tmp/spec.yaml --output-bundles /tmp/bundles/
   juju deploy /tmp/bundles/<interface>-test.yaml -m <interface>-test
   juju wait-for application <provider> -m <interface>-test --timeout 10m
   juju wait-for application <requirer> -m <interface>-test --timeout 10m
   ```

4. Run the validator at the **highest level the validator supports** (check
   `validate()` in `validator.py` -- use `deep` if implemented, otherwise `simple`):
   ```
   /project/development-sandbox/bin/dev-validate.py \
     --model <interface>-test \
     --app <requirer> \
     --level <highest-supported-level>
   ```

5. Run automated verification evidence:
   ```
   /project/development-sandbox/bin/verify-validator.sh \
     --model <interface>-test \
     --app <requirer> \
     --provider <provider> \
     --validator <name> \
     --level <highest-supported-level> \
     --output-dir /project/development-sandbox/reports/<name>-$(date +%Y%m%d-%H%M%S)
   ```
   If the backend is a raw Kubernetes deployment (not a Juju app — e.g. MinIO for `s3`),
   the default `juju scale-application` down step won't break connectivity because the
   Juju databag retains credentials even at 0 units. In that case use `--down-cmd` and
   `--restore-cmd` to scale the k8s deployment directly:
   ```
   /project/development-sandbox/bin/verify-validator.sh \
     --model <interface>-test \
     --app <requirer> \
     --provider <provider> \
     --validator <name> \
     --level <highest-supported-level> \
     --output-dir /project/development-sandbox/reports/<name>-$(date +%Y%m%d-%H%M%S) \
     --down-cmd "sudo k8s kubectl scale deployment <backend> -n <interface>-test --replicas=0 && sleep 5" \
     --restore-cmd "sudo k8s kubectl scale deployment <backend> -n <interface>-test --replicas=1 && sleep 15"
   ```
   This must capture workload-up and workload-down behavior. The report persists on
   the host at `development-sandbox/reports/`.

6. Report results. If any check fails:
   - Show the full JSON output.
   - Show `juju debug-log` output if relevant.
   - Describe what the failure indicates about the validator or the charm.
   - Do NOT modify validator code unless instructed.

7. Clean up:
   ```
   juju destroy-model <interface>-test --destroy-storage --no-prompt
   ```

## Output

Summarize:
- PASS / FAIL / ERROR for each check
- Workload-up and workload-down evidence (from `verify-validator.sh` summary/report)
- Whether the failure is in the validator logic or the charm's databag
- Any suggested fixes (but do not apply them unless asked)
