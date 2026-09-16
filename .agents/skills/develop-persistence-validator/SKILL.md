---
name: develop-persistence-validator
description: Develop a new Juju charm data persistence (canary/integrity) validator from scratch, or add persistence support to an existing endpoint validator. Use when asked to create, write, or build a validator that verifies data survives disruptive operations (pod deletion, scaling, migration, upgrades, etc.).
---

# Task: develop a new data persistence validator

## How persistence validators work

Persistence validators are a second, parallel kind of validator alongside the
functional (`BaseValidator`) ones described in the `develop-validator` skill.
Where a functional validator answers "is this relation healthy right now?", a
persistence validator answers "did the data behind this relation survive a
disruption?" across the lifetime of a test run.

### Architecture

```
test_deploy               (harness)  --persistence prepare    -> seed canary data
      |
      v
disruptive test (pod deletion,        --persistence checkpoint -> verify + advance
scaling, migration, upgrade, ...)                                 canary state
      |
      v
test_teardown              (harness)  --persistence cleanup    -> drop canary data
```

Each of the three lifecycle ops is a separate `run_validators` invocation on
the unit (via `ValidatorInjectorExtension.post_persistence` ->
`JujuClient.validate_model(persistence=...)`), mirroring how
`post_validate`/`validate_model(level=...)` works for functional checks. See
`SQ103 - Data Integrity and Persistence Validation.md` at the repo root for
the full design spec.

### The three lifecycle methods

Every persistence validator lives in `validators/<name>/validator.py`
alongside its functional counterpart and extends `BasePersistenceValidator`:

```python
from validators.base import (
    BasePersistenceValidator,
    PersistenceNotApplicable,
    PersistenceState,
    ValidationResult,
)

class MyClientPersistenceValidator(BasePersistenceValidator):
    def prepare(self) -> PersistenceState:
        """Seed canary data. Called once, before any disruption."""
        self._require_requires_role()
        identifier = secrets.randbits(31)
        # ... create a uniquely-named canary table/object and write one row/record ...
        return PersistenceState(id=identifier, ref=1)

    def checkpoint(self, expected: PersistenceState) -> tuple[ValidationResult, PersistenceState]:
        """Verify canary data survived, then extend it. Called after each disruption."""
        self._require_requires_role()
        # ... read back and assert the row/record count matches expected.ref ...
        # ... unconditionally write one more row/record ...
        new_state = PersistenceState(id=expected.id, ref=expected.ref + 1)
        return self._make_result(level="deep", checks=[...]), new_state

    def cleanup(self) -> None:
        """Drop all canary data. Called once, at the end of the run."""
        self._require_requires_role()
        # ... discover and drop every canary table/object this validator created ...
```

Key design points (see `PostgreSQLClientPersistenceValidator` in
`validators/postgresql_client/validator.py` for the reference
implementation):

- **`prepare()`** picks its own random, unpredictable identifier (e.g.
  `secrets.randbits(31)`) - never derive it from `relation_id`, which is
  *not* stable across relation remove/re-add (see
  `test_remove_and_restore_integration`). The identifier seeds a uniquely
  named canary resource (e.g. `validator_canary_<identifier>`) so concurrent
  or repeated runs never collide.
- **`checkpoint()`** takes the `PersistenceState` the harness has been
  tracking, verifies the canary data is still there and has exactly
  `expected.ref` records, then unconditionally writes one more record and
  returns `PersistenceState(id=expected.id, ref=expected.ref + 1)` -
  regardless of whether the check passed. This lets subsequent checkpoints
  in the same run keep counting correctly even after a single failure was
  already reported.
- **`cleanup()`** takes no arguments (it runs as a fresh process invocation
  with no state carried over from `prepare`/`checkpoint`). Discover
  everything to remove by name pattern (e.g. `information_schema.tables
  WHERE table_name LIKE 'validator_canary_%'`), not by a remembered
  identifier.
- **Role gating.** Most client-server interfaces only make sense to persist
  from the `requires` (client) side. Raise `PersistenceNotApplicable` from
  all three methods when `self.role != "requires"` (or whatever your
  interface's applicable side is) - the runner treats this as a silent skip,
  not a failure. Add a small `_require_requires_role()` helper to avoid
  repeating the check.
- Share connection/credential-resolution logic between the functional and
  persistence validator classes via a private mixin (e.g.
  `_PostgreSQLConnectionMixin`) rather than duplicating it.

### Package structure

Persistence validators live in the **same package** as the interface's
functional validator - there's no separate `validators/<name>_persistence/`
directory. Add the new class to the existing `validator.py`, export it from
`__init__.py`, and register a **second** entry point:

```toml
[project.entry-points."endpoint_validators"]
<interface_name> = "validators.<name>:MyClientValidator"

[project.entry-points."endpoint_persistence_validators"]
<interface_name> = "validators.<name>:MyClientPersistenceValidator"
```

Both entry-point groups key off the same Juju interface name. The runner
(`validators/runner/runner.py`) discovers `endpoint_persistence_validators`
independently of `endpoint_validators`, so a validator package can implement
either, both, or neither.

## Steps

1. Confirm (or build, per `develop-validator`) a functional validator already
   exists for the interface in `validators/<name>/validator.py`. Persistence
   validators are additive to that package.

2. Identify what "canary data" means for this interface: a row in a table, a
   key in a KV store, an object in a bucket, a topic message, etc. It must be:
   - Cheap to create and verify.
   - Uniquely identifiable (via a random identifier chosen at `prepare()` time).
   - Discoverable by name pattern alone at `cleanup()` time, without needing
     the identifier.

3. Add a `PostgreSQLClientPersistenceValidator`-style class implementing
   `prepare()`, `checkpoint()`, `cleanup()`, extending
   `BasePersistenceValidator` and raising `PersistenceNotApplicable` for
   sides where persistence doesn't apply.

4. Register the `endpoint_persistence_validators` entry point in the
   package's `pyproject.toml` (see above). No other pyproject.toml changes
   are needed - persistence validators reuse the same dependency graph as
   the functional validator in the same package.

5. Write unit tests in the existing `tests/unit/test_validator.py`, extending
   any connection/cursor stubs already used by the functional validator's
   tests rather than duplicating them. Cover, at minimum:
   - Role gating: each of `prepare`/`checkpoint`/`cleanup` raises
     `PersistenceNotApplicable` when `self.role` isn't the applicable side.
   - `prepare()` creates the canary resource and returns a `PersistenceState`
     with a fresh identifier and `ref=1`.
   - `checkpoint()` passes when the count matches `expected.ref`, fails when
     it doesn't, and in both cases returns `PersistenceState(ref=expected.ref
     + 1)` and writes a new record.
   - `cleanup()` discovers and drops every matching canary resource, and is a
     no-op when none exist.

6. Run the package's unit tests and the monorepo-wide checks:
   ```
   poetry run pytest validators/<name>
   poetry run pytest validators/runner    # confirm discovery/wiring still passes
   poetry run mypy --explicit-package-bases validators/*
   poetry run ruff check validators/<name>
   poetry run ruff format --check validators/<name>
   ```
   Fix any issues before continuing.

7. If the harness doesn't already wire persistence into the disruptive test
   suite for this interface's typical charms, no charm-specific harness
   changes are needed - `ValidatorInjectorExtension.post_persistence` and
   `JujuClient.validate_model(persistence=...)` are interface-agnostic and
   pick up any registered `endpoint_persistence_validators` entry
   automatically once `test_deploy` (prepare), the disruptive tests
   (checkpoint), and `test_teardown` (cleanup) run for a model containing
   this interface.

8. Manually verify end-to-end if a live model is available: deploy the
   two charms, run
   `run_validators --persistence prepare` on the requirer unit, disrupt the
   provider (restart/scale/migrate), then `run_validators --persistence
   checkpoint --refs '{"<relation_id>": {"id": ..., "ref": 1}}'` and confirm
   a `PASS` result with `ref` advanced by one, and finally
   `run_validators --persistence cleanup` and confirm the canary resource is
   gone.

## Common patterns

### Discovering canary resources by name pattern

```python
_CANARY_TABLE_PREFIX = "validator_canary_"

def cleanup(self) -> None:
    self._require_requires_role()
    with self._open_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name LIKE %s",  # nosec B608
            (f"{_CANARY_TABLE_PREFIX}%",),
        )
        for (table_name,) in cur.fetchall():
            cur.execute(f"DROP TABLE IF EXISTS {table_name}")  # nosec B608
```

### Role gating helper

```python
def _require_requires_role(self) -> None:
    if self.role != "requires":
        raise PersistenceNotApplicable(f"persistence not applicable on the '{self.role}' side")
```

### Sharing connection logic with the functional validator

```python
class _PostgreSQLConnectionMixin:
    def _resolve_credentials(self) -> dict[str, str]: ...
    def _connect(self, ...) -> "Connection": ...

class PostgreSQLClientValidator(_PostgreSQLConnectionMixin, BaseValidator): ...
class PostgreSQLClientPersistenceValidator(_PostgreSQLConnectionMixin, BasePersistenceValidator): ...
```

## Validator-specific notes

- Persistence validators never receive a `level` argument - there's no
  "simple"/"deep" distinction for prepare/checkpoint/cleanup.
- `checkpoint()`'s `ValidationResult` should use `level="deep"` (the closest
  existing fit; there's no dedicated persistence level in `ValidationLevel`).
- The harness (not the validator) is responsible for tracking
  `PersistenceState` across the whole test run, keyed by
  `(controller, model, unit, relation_id)` - see `PersistenceKey` in
  `charm_integration_testing/juju/models.py`. Validators are stateless
  between invocations; everything they need is either passed in
  (`expected: PersistenceState`) or discoverable from the live backend
  (`cleanup()`).
- Do not assume `checkpoint()` is called with contiguous, ever-increasing
  `ref` values only on success - a prior checkpoint's failure still advances
  `ref`, so always trust the `expected` argument passed in rather than
  re-deriving state from what you last wrote.

## Acceptance criteria

- The interface's package (`validators/<name>/validator.py`) has a
  `<Interface>PersistenceValidator` class implementing
  `prepare`/`checkpoint`/`cleanup`, extending `BasePersistenceValidator`.
- `PersistenceNotApplicable` is raised on every side where persistence
  doesn't apply.
- `pyproject.toml` registers the class under
  `[project.entry-points."endpoint_persistence_validators"]` keyed by the
  Juju interface name.
- Unit tests cover role gating, prepare, checkpoint (pass and fail), and
  cleanup (including the no-canary-resources case).
- `poetry run pytest validators/<name> validators/runner` passes.
- `poetry run mypy --explicit-package-bases validators/*` passes.
- `poetry run ruff check` / `ruff format --check` pass for the changed package.
- No hardcoded charm names, model names, or relation ids inside the
  validator code.
