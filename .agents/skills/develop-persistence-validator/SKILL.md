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
the unit, triggered by `JujuClient.validate_model(persistence=...)` calling
`ValidatorInjectorExtension.post_persistence`, mirroring how
`post_validate`/`validate_model(level=...)` works for functional checks. The
`BasePersistenceValidator`/`PersistenceState`/`PersistenceNotApplicable`
docstrings in `validators/base/validator.py` are the source of truth for the
protocol; see `docs/reference/validators.rst` for the higher-level design
overview.

### The three lifecycle methods

Every persistence validator lives in `validators/<name>/validator.py`
(alongside its functional counterpart when one exists - a persistence-only
package is also valid, see step 1) and extends `BasePersistenceValidator`:

```python
import uuid

from validators.base import (
    BasePersistenceValidator,
    PersistenceNotApplicable,
    PersistenceState,
    ValidationResult,
)

class MyClientPersistenceValidator(BasePersistenceValidator):
    def prepare(self) -> PersistenceState:
        """Seed canary data for this relation. Called once when a relation is first established,
        and again whenever the relation is removed and re-added (which changes relation_id, so
        the previous canary data - if any survived - is no longer reachable under the new state
        key and fresh data is needed). prepare() must not assume the canary resource it creates
        doesn't already exist (e.g. from a previous run's leftover state, or a re-run of a failed
        prepare), and should always return a state usable from a clean slate."""
        self._require_requires_role()
        identifier = uuid.uuid4().int
        # ... create a uniquely-named canary table/object and write one marked row/record ...
        # commit (or use an autocommit connection) before returning - a transactional backend
        # left uncommitted here can roll back the write, so the next checkpoint() sees no data.
        return PersistenceState(id=identifier, ref=1)

    def checkpoint(self, expected: PersistenceState) -> tuple[ValidationResult, PersistenceState]:
        """Verify canary data survived, then extend it. Called after each disruption."""
        self._require_requires_role()
        # ... read back and assert the marked row/record count matches expected.ref (filter on a
        # stable marker value, not a bare row count - see "Common patterns" below) ...
        # ... unconditionally write one more marked row/record, then commit it (or use autocommit)
        # for the same reason as prepare() above ...
        new_state = PersistenceState(id=expected.id, ref=expected.ref + 1)
        return self._make_result(level="deep", checks=[...]), new_state

    def cleanup(self) -> None:
        """Drop all canary data.

        Not guaranteed to run only once at the end of a test run: the harness also invokes
        cleanup on state transitions mid-session (e.g. before a disruptive test in
        `test_remove_and_restore_integration`, which explicitly invalidates the old state after
        the relation is removed and re-added under a new relation ID), and `prepare()` may run
        again afterwards. Must be safe to call when no canary resource remains (no-op, not an
        error).
        """
        self._require_requires_role()
        # ... discover and drop every canary table/object this validator created ...
```

Key design points (see `PostgreSQLClientPersistenceValidator` in
`validators/postgresql_client/validator.py` for the reference
implementation):

- **`prepare()`** picks its own random, unpredictable identifier (e.g.
  `uuid.uuid4().int`) - never derive it from `relation_id`, which is *not*
  stable across relation remove/re-add (see
  `test_remove_and_restore_integration`), and never use a small random space
  (e.g. a 31-bit int) that could collide across concurrent or repeated runs.
  The identifier seeds a uniquely named canary resource (e.g.
  `validator_canary_<identifier>`).
- **`checkpoint()`** takes the `PersistenceState` the harness has been
  tracking, verifies the canary data is still there and has exactly
  `expected.ref` records, then unconditionally writes one more record and
  returns `PersistenceState(id=expected.id, ref=expected.ref + 1)` -
  regardless of whether the check passed. This lets subsequent checkpoints
  in the same run keep counting correctly even after a single failure was
  already reported. Verify a stable, identifier-derived marker value on each
  record rather than trusting a bare `count(*)` - a resource that was
  dropped and silently recreated from scratch could otherwise coincidentally
  satisfy a row-count-only check (see "Common patterns" below).
- **`cleanup()`** takes no arguments (it runs as a fresh process invocation
  with no state carried over from `prepare`/`checkpoint`). Discover
  everything to remove by name pattern - see the escaped, schema-scoped
  `information_schema.tables` query under "Common patterns" below, not a
  bare `LIKE 'validator_canary_%'` (`_` is itself a `LIKE` wildcard and can
  match unrelated tables). Discovery is prefix-based rather than
  identifier-based, so it will also drop canary resources left behind by any
  other concurrent validator run against the same backend that shares the
  prefix - this is an accepted trade-off of the pattern, so avoid running
  multiple persistence validation runs against the same database/model
  concurrently.
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
functional validator, if one already exists - there's no separate
`validators/<name>_persistence/` directory. Add the new class to the
existing `validator.py`, export it from `__init__.py`, and register it as a
**second** entry point alongside the existing `endpoint_validators` one:

```toml
[project.entry-points."endpoint_validators"]
<interface_name> = "validators.<name>:MyClientValidator"

[project.entry-points."endpoint_persistence_validators"]
<interface_name> = "validators.<name>:MyClientPersistenceValidator"
```

A functional validator is not required, though: a package may register only
`endpoint_persistence_validators` (persistence-only) if the interface has no
`endpoint_validators` entry - see step 1. Both entry-point groups key off the
same Juju interface name. The runner (`validators/runner/runner.py`)
discovers `endpoint_persistence_validators` independently of
`endpoint_validators`, so a validator package can implement either, both, or
neither.

## Steps

1. Confirm a package for this interface already exists under
   `validators/<name>/` (with `pyproject.toml`, `__init__.py`, etc.). If it
   doesn't, create the package skeleton and register it in the root
   `pyproject.toml` and `validators/runner/pyproject.toml` (reusing only the
   packaging/dependency-registration steps from the `develop-validator`
   skill), which persistence validators rely on for entry-point discovery
   just as functional validators do. A functional validator
   (`endpoint_validators` entry point) is *not* a hard prerequisite: the
   runner discovers `endpoint_validators` and `endpoint_persistence_validators`
   independently, so a package may register either, both, or neither -
   don't implement a functional validator unless this interface actually
   needs one. In practice, most interfaces already have a functional
   validator, and persistence validators are usually additive to that
   existing package.

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
   package's `pyproject.toml` (see above). If the package already existed
   with a functional validator, the package itself is already registered as
   a Poetry dependency in the root `pyproject.toml` and in
   `validators/runner/pyproject.toml` - the only entry-point registration
   needed is the new table above. This does not mean no other package
   metadata can change: persistence support may need its own runtime
   dependency (e.g. a client library the functional validator doesn't use)
   or dev dependency (e.g. a test double), which must still be added to the
   package's own `pyproject.toml` under `[project].dependencies` /
   `[project.optional-dependencies].dev` (matching the PEP 621 format
   `validators/<name>/pyproject.toml` already uses - not the root project's
   `[tool.poetry.dependencies]` format). A new dev dependency there is only
   installed if the root `pyproject.toml`'s path dependency for this package
   already lists `extras = ["dev"]` (e.g. `validators-postgresql-client = {
   path = "./validators/postgresql_client", develop = true, extras = ["dev",
   ...] }`) - if it's missing `"dev"`, add it. If
   you just created the package from scratch in step 1, make sure the
   root/`validators/runner` registrations are also done (per
   `develop-validator`); they're required for entry-point discovery
   regardless of which entry-point group(s) the package declares.

5. Write unit tests. If the package already has `tests/unit/test_validator.py`
   for its functional validator, extend it (reuse any connection/cursor stubs
   it already defines rather than duplicating them). If the package was just
   created from scratch in step 1 and has no test file yet, create
   `tests/unit/test_validator.py` (and any `tests/unit/__init__.py` needed to
   make it a package) following the layout of an existing validator package.
   Cover, at minimum:
   - Role gating: each of `prepare`/`checkpoint`/`cleanup` raises
     `PersistenceNotApplicable` when `self.role` isn't the applicable side.
   - `prepare()` creates the canary resource and returns a `PersistenceState`
     with a fresh identifier and `ref=1`.
   - `checkpoint()` passes when the check matches `expected.ref`, fails when
     it doesn't, and in both cases returns `PersistenceState(ref=expected.ref
     + 1)` and writes a new marked record. Also cover that a check based only
     on a bare existence/count check would be insufficient - assert it scopes
     on the marker/identifier written by `prepare()`, not just "the resource
     exists" or "the count matches" (for SQL backends this means filtering
     the row count query on the marker column, not a bare `count(*)`; for a
     KV store, bucket, or topic, the equivalent is asserting the read/list
     is scoped to the specific key/object/message identifier).
   - `cleanup()` discovers and drops every matching canary resource, is a
     no-op when none exist, and safely quotes any discovered identifier
     before using it in a DDL statement (where applicable to the backend).

6. Run the package's unit tests and the monorepo-wide checks:
   ```
   poetry run pytest validators/<name>
   poetry run pytest validators/runner    # confirm discovery/wiring still passes
   poetry run mypy --explicit-package-bases validators/*
   poetry run ruff check validators/<name>
   poetry run ruff format --check validators/<name>
   ```
   Fix any issues before continuing.

7. No charm-specific harness changes are needed to wire persistence in:
   `ValidatorInjectorExtension.post_persistence` and
   `JujuClient.validate_model(persistence=...)` are interface-agnostic and
   pick up any registered `endpoint_persistence_validators` entry
   automatically, once `test_deploy` (prepare), the disruptive tests
   (checkpoint), and `test_teardown` (cleanup) run for a model containing
   this interface.

8. Manually verify end-to-end if a live model is available: deploy the
   two charms, run
   `run_validators --persistence prepare` on the unit whose role this
   validator applies to (per its role gating - e.g. the requirer for a
   requirer-side canary), disrupt the other/remote side of the relation
   (restart/scale/migrate), then `run_validators --persistence
   checkpoint --refs '{"<relation_id>": {"id": ..., "ref": 1}}'` and confirm
   a `PASS` result plus an `updated_refs` entry for that `relation_id` with
   `ref` advanced by one (`ref` is not on the `ValidationResult` itself - see
   `ValidatorRunnerResults` in `validators/runner/runner.py`), and finally
   `run_validators --persistence cleanup` and confirm the canary resource is
   gone.

## Common patterns

### Discovering canary resources by name pattern

```python
_CANARY_TABLE_PREFIX = "validator_canary_"


def _quote_identifier(name: str) -> str:
    """Double any embedded double-quotes - the standard, connection-independent way to escape a
    Postgres identifier. Prefer this (or `psycopg2.sql.Identifier`) over raw string interpolation
    whenever a table/column name comes from a query result rather than a value you generated."""
    return '"' + name.replace('"', '""') + '"'


def _canary_table_prefix(self) -> str:
    # Scope discovery to this model *and* relation, not just relation_id: relation_id is
    # assigned per-model, so two different models could otherwise expose the same numeric
    # relation_id against a shared database/schema and collide. A short hash of the model UUID
    # keeps the prefix bounded in length regardless of the UUID's own format.
    model_token = hashlib.sha256(self.charm.model.uuid.encode()).hexdigest()[:8]
    return f"{_CANARY_TABLE_PREFIX}{model_token}_{self.relation_id}_"


def cleanup(self) -> None:
    self._require_requires_role()
    # cleanup() runs for every relation with a registered persistence validator, including one
    # that never got as far as receiving credentials (e.g. the relation is still being set up).
    # Treat a databag with no usable credentials yet as a no-op instead of raising by opening a
    # connection anyway.
    if not self.databag.get("uris") and not self.databag.get("secret-user"):
        return
    # `_` and `%` are LIKE wildcards, so a prefix containing underscores must be escaped or it
    # can match unrelated tables (e.g. "validatorXcanaryY1").
    prefix = self._canary_table_prefix()
    escaped_prefix = prefix.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
    conn = self._open_connection()
    conn.autocommit = True  # or commit explicitly after each DROP - without this the DDL below
    # is rolled back when the connection closes, so cleanup can report success while leaving the
    # canary tables in place.
    try:
        with conn.cursor() as cur:
            # CREATE TABLE elsewhere is unqualified, so it resolves through search_path into
            # current_schema(); restrict discovery (and the DROP below) to that same schema, or
            # a same-named table in another schema could be left behind or wrongly targeted. Also
            # restrict to base tables: a view/foreign table sharing the prefix would make DROP
            # TABLE fail and abort cleanup, leaving any remaining canary tables undropped.
            cur.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "  # nosec B608
                "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' "
                "AND table_name LIKE %s ESCAPE '\\'",
                (f"{escaped_prefix}%",),
            )
            tables = cur.fetchall()
        for schema, table_name in tables:
            with conn.cursor() as cur:
                quoted = f"{_quote_identifier(schema)}.{_quote_identifier(table_name)}"
                cur.execute(f"DROP TABLE IF EXISTS {quoted}")  # nosec B608
    finally:
        # A `with conn:` block only commits/rolls back on exit for psycopg2 - it does not close
        # the connection, so always close explicitly or copied code leaks one connection per call.
        conn.close()
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
    def _connect(self, credentials: dict[str, str]) -> "Connection": ...

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
