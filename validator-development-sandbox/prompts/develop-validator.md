# Task: develop a new validator

## Goal

Write, deploy, and validate a new Juju charm integration validator for the
interface named in the task. The result should be a working Python package
under `validators/<name>/` with passing `dev-validate` output.

## Steps

1. Determine the Juju interface name (e.g. `postgresql`, `kafka`, `s3`).

2. Search CharmHub for a charm that **provides** the interface and one that
   **requires** it. Prefer widely-used charms on `stable` channels.

3. Write `/tmp/spec.yaml` describing a minimal two-charm deployment. Use a
   dedicated model name like `<interface>-test` (not `testing`) so the
   deployment is isolated and easy to clean up.

4. Create the model and generate the bundle:
   ```
   juju add-model <interface>-test
   bundle-builder-x --spec /tmp/spec.yaml --output-bundles /tmp/bundles/
   ```

5. Deploy:
   ```
   juju deploy /tmp/bundles/<interface>-test.yaml -m <interface>-test
   juju wait-for application <provider> -m <interface>-test --timeout 10m
   juju wait-for application <requirer> -m <interface>-test --timeout 10m
   ```

6. Create the validator package skeleton:
   - `validators/<name>/__init__.py`
   - `validators/<name>/validator.py`  (class extending `BaseValidator`)
   - `validators/<name>/pyproject.toml`  (with correct entry point)

7. Wire the new validator package into project dependencies:
   - Add `validators-<name>` to `validators/runner/pyproject.toml` dependencies.
   - Add `validators-<name> = { path = "./validators/<name>", develop = true }`
     to the root `/project/pyproject.toml` under `[tool.poetry.dependencies]`.
   - Run `poetry install` from `/project` so the new package is available.

8. Run and iterate:
   ```
   /project/validator-development-sandbox/bin/dev-validate.py --model <interface>-test --app <requirer> --reinstall
   ```
   Read the JSON output. Fix checks that fail. Repeat until all PASS.

9. Run code quality checks from `/project` and fix any issues:
   ```
   ./scripts/format.sh
   ./scripts/lint.sh
   ```
   Do not finish while either command fails.

10. **Self-review.** Read every file in `validators/<name>/` and check each
    item below. Fix any issue found, then re-run format/lint if you made changes.

    **Structure**
    - Package root contains exactly: `__init__.py`, `validator.py`, `pyproject.toml`.
    - `tests/__init__.py`, `tests/unit/__init__.py`, and `tests/unit/test_validator.py` all exist.
    - No unexpected files or directories.

    **License header**
    - Every `.py` file and `pyproject.toml` begins with the GPL-3 block
      (`# Copyright (C) 2026 Canonical Ltd` ...) exactly as in
      `validators/postgresql_client/validator.py`.

    **pyproject.toml**
    - `name` is `"validators-<name>"` in kebab-case matching the directory name.
    - `authors` is `[{name = "SQA Team", email = "solutionsqa@canonical.com"}]`.
    - `requires-python = ">=3.10"`.
    - `validators-base` is in `dependencies`.
    - The entry-point key under `[project.entry-points."endpoint_validators"]`
      is the exact Juju interface name.

    **validator.py**
    - Class name follows `<Interface>Validator` PascalCase.
    - `validate()` calls `_skipped_result(level)` for unsupported levels.
    - Uses `self.validate_schema(...)` for required-field checks.
    - Uses `self.resolve_secret(...)` for Juju secret resolution.
    - No hardcoded charm names, model names, or endpoint strings.
    - No `print()` calls. No unused or wildcard imports.

    **tests/unit/test_validator.py**
    - Defines `AppStub`, `RelationStub`, `RelationMetaStub`, `CharmMetaStub`,
      `CharmStub`, and a `_make_validator()` factory using `cast(ops.CharmBase, ...)`
      and `cast(ops.Relation, ...)`.
    - Covers: happy-path PASS, missing-fields FAIL, no-app ERROR, unsupported
      level SKIPPED.
    - All external I/O is mocked with `unittest.mock.patch`.

11. Produce verification evidence (workload-up and workload-down):
    ```
    /project/validator-development-sandbox/bin/verify-validator.sh \
      --model <interface>-test \
      --app <requirer> \
      --provider <provider> \
      --validator <name>
    ```
    If the backend is a raw Kubernetes deployment (not a Juju app — e.g. MinIO for `s3`),
    the default `juju scale-application` down step won't break connectivity because the
    Juju databag retains credentials even at 0 units. In that case use `--down-cmd` and
    `--restore-cmd` to scale the k8s deployment directly:
    ```
    /project/validator-development-sandbox/bin/verify-validator.sh \
      --model <interface>-test \
      --app <requirer> \
      --provider <provider> \
      --validator <name> \
      --down-cmd "sudo k8s kubectl scale deployment <backend> -n <interface>-test --replicas=0 && sleep 5" \
      --restore-cmd "sudo k8s kubectl scale deployment <backend> -n <interface>-test --replicas=1 && sleep 15"
    ```
    Include `summary.txt` and `report.json` paths from the output bundle in your report.

12. When done, destroy the dedicated model:
   ```
   juju destroy-model <interface>-test --destroy-storage --no-prompt
   ```

## Acceptance criteria

- `dev-validate` exits 0 with all checks PASS for at least the `simple` level.
- The validator package has correct `pyproject.toml` with entry point.
- `validators/runner/pyproject.toml` includes `validators-<name>`.
- Root `/project/pyproject.toml` includes `validators-<name>` as a Poetry develop dependency.
- `./scripts/format.sh` exits 0 after all changes.
- `./scripts/lint.sh` exits 0 after all changes.
- Self-review complete: all structure, license, naming, and test coverage criteria met.
- `verify-validator.sh` exits 0.
- Verification evidence includes both workload-up pass and workload-down detection.
- No hardcoded charm names or model names inside the validator code.
