---
name: develop-validator
description: Develop a new Juju charm integration validator from scratch. Use when asked to create, write, or build a new validator for an interface.
---

# Task: develop a new validator

## How validators work

### Architecture

```
CharmHub   →   bundle-builder-x   →   juju deploy
                                           ↓
                                     Juju unit (pod)
                                           ↓
                           ValidatorInjectorExtension
                           (builds wheels, SCP to unit,
                            uv pip install, run_validators)
                                           ↓
                                     JSON results
```

### Validator class structure

Every validator lives in `validators/<name>/validator.py` and extends `BaseValidator`:

```python
from validators.base import BaseValidator, ValidationCheck, ValidationLevel, ValidationResult

class MyValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if level != "simple":
            return self._skipped_result_due_to_level(level)

        checks: list[ValidationCheck] = []
        databag = self.databag  # safe: returns {} if relation.app is absent

        # Check required fields
        missing = [f for f in ("host", "port") if not databag.get(f)]
        checks.append(ValidationCheck(
            name="schema",
            passed=not missing,
            message="OK" if not missing else f"Missing: {', '.join(missing)}",
        ))

        # Resolve Juju secrets if needed (see "Common patterns" below)
        return self._make_result(level=level, checks=checks)
```

### Package structure for a new validator

```
validators/<name>/
  __init__.py         # empty
  validator.py        # the validator class
  pyproject.toml
  tests/
    __init__.py
    unit/
      __init__.py
      test_validator.py
```

Minimal `pyproject.toml`:

```toml
[project]
name = "validators-<name>"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "validators-base",
]

[project.optional-dependencies]
dev = [
    "validators-test-utils",
]

[project.entry-points."endpoint_validators"]
<interface_name> = "validators.<name>:MyValidatorClass"

[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"
```

The entry point key is the **Juju interface name** (e.g. `postgresql`, `mongodb_client`).
The runner discovers validators by this key and matches them to charm relations.

**Add `validators-test-utils` if your unit tests use it.** Most validator tests
import stubs and helpers from `validators.test_utils` (`make_charm_from_relation`,
`ApplicationStub`, `RelationRoleStub`, `RelationStub`, etc.) — when
`tests/unit/test_validator.py` does this, declare `validators-test-utils` under
`[project.optional-dependencies].dev`, and add `extras = ["dev"]` (or extend an
existing `extras` list) to the package's entry in the root
`$PROJECT_ROOT/pyproject.toml` under `[tool.poetry.dependencies]`. Forgetting
this when the tests do use it is a recurring mistake — tests still pass locally
because `validators-test-utils` is already installed elsewhere in the monorepo
venv, masking that the package's own dependency graph is incomplete.

**Naming convention:** the `[project] name` field always uses dashes, even when the
directory or module uses underscores. For example, a validator in
`validators/postgresql_client/` is named `validators-postgresql-client` in
`pyproject.toml`. Replace underscores with dashes when setting the package name.

---

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
   - `validators/<name>/tests/__init__.py`
   - `validators/<name>/tests/unit/__init__.py`
   - `validators/<name>/tests/unit/test_validator.py`

7. Wire the new validator package into project dependencies:
   - Add `validators-<name>` to `validators/runner/pyproject.toml` dependencies.
   - Add `validators-<name> = { path = "./validators/<name>", develop = true }`
     to the root `$PROJECT_ROOT/pyproject.toml` under `[tool.poetry.dependencies]`.
   - Run `poetry install` from `$PROJECT_ROOT` so the new package is available.

8. Run and iterate:
   ```
   $PROJECT_ROOT/development-sandbox/bin/dev-validate.py --model <interface>-test --app <requirer> --reinstall
   ```
   Read the JSON output. Fix checks that fail. Repeat until all PASS.

9. Run code quality checks from `$PROJECT_ROOT` and fix any issues:
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
    - Every `.py` file and `pyproject.toml` begins with the canonical two-line header:
      ```
      # Copyright <year> Canonical Ltd.
      # See LICENSE file for licensing details.
      ```

    **pyproject.toml**
    - `name` is `"validators-<name>"` in kebab-case matching the directory name.
    - `authors` is `[{name = "SQA Team", email = "solutionsqa@canonical.com"}]`.
    - `requires-python = ">=3.10"`.
    - `validators-base` is in `dependencies`.
    - If `tests/unit/test_validator.py` imports from `validators.test_utils`,
      `validators-test-utils` is declared under `[project.optional-dependencies].dev`,
      and the root `pyproject.toml` entry for this package includes `extras = ["dev"]`.
    - The entry-point key under `[project.entry-points."endpoint_validators"]`
      is the exact Juju interface name.

    **validator.py**
    - Class name follows `<Interface>Validator` PascalCase.
    - `validate()` calls `_skipped_result_due_to_level(level)` for unsupported levels.
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

11. Produce verification evidence (workload-up and workload-down). Run at the
    **highest level the validator supports** (check `validate()` in `validator.py`
    -- use `deep` if implemented, otherwise `simple`):
    ```
    $PROJECT_ROOT/development-sandbox/bin/verify-validator.sh \
      --model <interface>-test \
      --app <requirer> \
      --provider <provider> \
      --validator <name> \
      --level <highest-supported-level> \
      --output-dir $PROJECT_ROOT/development-sandbox/reports/<name>-$(date +%Y%m%d-%H%M%S)
    ```
    If the backend is a raw Kubernetes deployment (not a Juju app — e.g. MinIO for `s3`),
    the default `juju scale-application` down step won't break connectivity because the
    Juju databag retains credentials even at 0 units. In that case use `--down-cmd` and
    `--restore-cmd` to scale the k8s deployment directly:
    ```
    $PROJECT_ROOT/development-sandbox/bin/verify-validator.sh \
      --model <interface>-test \
      --app <requirer> \
      --provider <provider> \
      --validator <name> \
      --level <highest-supported-level> \
      --output-dir $PROJECT_ROOT/development-sandbox/reports/<name>-$(date +%Y%m%d-%H%M%S) \
      --down-cmd "sudo k8s kubectl scale deployment <backend> -n <interface>-test --replicas=0 && sleep 5" \
      --restore-cmd "sudo k8s kubectl scale deployment <backend> -n <interface>-test --replicas=1 && sleep 15"
    ```
    The report is written to the `--output-dir` and persists on the host at
    `development-sandbox/reports/`. Include the `summary.txt` and `report.json`
    paths in your completion summary.

12. When done, destroy the dedicated model:
   ```
   juju destroy-model <interface>-test --destroy-storage --no-prompt
   ```

## Common patterns

### Resolving Juju secrets

Many charms expose credentials via Juju secrets instead of plain databag fields.
The base class has a helper:

```python
creds = self.resolve_secret("secret-user", "username", "password")
# Returns {"username": "...", "password": "..."} from secret or databag
```

### Checking connectivity

For database validators, connect with the client library and run a probe query:

```python
import psycopg2  # add to pyproject.toml dependencies as psycopg2-binary

# Derive connection parameters from the relation databag
host = databag.get("host", "")
port = databag.get("port", "5432")
db = databag.get("database", "")

try:
    creds = self.resolve_secret("secret-user", "username", "password")
    conn = psycopg2.connect(
        host=host, port=port, dbname=db,
        user=creds["username"], password=creds["password"],
    )
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
    conn.close()
    checks.append(ValidationCheck(name="connectivity", passed=True, message="OK"))
except Exception as exc:
    checks.append(ValidationCheck(name="connectivity", passed=False, message=str(exc)))
```

### Adding a deep-level check

Return `_skipped_result_due_to_level` for levels you don't support. Only implement what you've tested:

```python
def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
    if level == "uat":
        return self._skipped_result_due_to_level(level)
    if level == "deep":
        # do deeper checks
        ...
    # simple checks always run
```

## Validator-specific notes

- **`dev-validate.py` auto-reexecs via `poetry run`** if invoked outside the Poetry venv, so you can call it directly without any manual prefix. Do not wrap it in `poetry run` yourself.
- If a relation has no remote app (`relation.app is None`), return an `ERROR` result immediately.
- Keep validators focused on a single interface. Do not add cross-interface logic.
- Add client library dependencies (e.g. `psycopg2-binary`) to the validator's `pyproject.toml` `dependencies`.

## Acceptance criteria

- `dev-validate` exits 0 with all checks PASS at the highest supported level.
- The validator package has correct `pyproject.toml` with entry point.
- If the unit tests import from `validators.test_utils`, `validators-test-utils`
  is declared under `[project.optional-dependencies].dev` in the validator's own
  `pyproject.toml`, and the root `pyproject.toml` entry includes `extras = ["dev"]`.
- `validators/runner/pyproject.toml` includes `validators-<name>`.
- Root `$PROJECT_ROOT/pyproject.toml` includes `validators-<name>` as a Poetry
  develop dependency.
- `./scripts/format.sh` exits 0 after all changes.
- `./scripts/lint.sh` exits 0 after all changes.
- Self-review complete: all structure, license, naming, and test coverage criteria met.
- `verify-validator.sh` exits 0.
- Verification evidence includes both workload-up pass and workload-down detection.
- No hardcoded charm names or model names inside the validator code.
