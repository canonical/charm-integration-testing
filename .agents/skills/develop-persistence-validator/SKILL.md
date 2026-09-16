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
        # Mask to a backend-safe bit width (e.g. 63 bits for PostgreSQL) rather than using the
        # full value if the identifier becomes part of a length-limited resource name - see the
        # "prepare()" design point below.
        identifier = uuid.uuid4().int & ((1 << 63) - 1)
        # ... create a canary table/object named e.g. f"{self._canary_table_prefix()}{identifier:020d}"
        # - the identifier must be zero-padded to a fixed width (20 digits here, since the 63-bit
        # mask above never produces more than 19) so cleanup()'s discovery regex, which matches an
        # exact "prefix + fixed-width digits" shape (see "Common patterns" below), can find it
        # again. An unpadded/variable-width identifier would never match that regex, leaving the
        # canary resource undiscoverable and orphaned. Then write one marked row/record ...
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

        Only invoked from `test_teardown`, once per test run - the harness does not call cleanup
        mid-session. A relation remove/re-add (e.g. in `test_remove_and_restore_integration`)
        instead invalidates the *tracked* `PersistenceState` for the old relation_id and calls
        `prepare()` again for the new one. Note that this is a real, accepted limitation, not just
        a delay: the discovery namespace (see `_canary_table_prefix`/"Common patterns" below) is
        derived from the *current* relation_id, so `cleanup()` for the new relation cannot find
        canary data left behind under the old relation_id - that old resource remains orphaned
        rather than being swept up by a later cleanup call. If your backend needs a stronger
        guarantee, record prior relation_ids somewhere that outlives the relation itself - e.g. the
        charm's own peer relation databag (if one exists) or `ops.StoredState`, not this relation's
        databag, which is removed along with the relation and so cannot durably track prior IDs -
        so cleanup can enumerate and drop them too. Must still be safe to call when no canary
        resource remains (no-op, not an error) - e.g. if `prepare()` was never reached for a given
        relation.
        """
        self._require_requires_role()
        # ... discover and drop every canary table/object this validator created ...

    def _require_requires_role(self) -> None:
        # Every method above calls this before doing anything else; defined once here rather than
        # repeated inline (see "Role gating" below).
        if self.role != "requires":
            raise PersistenceNotApplicable(f"persistence not applicable on the '{self.role}' side")
```

Key design points (see `PostgreSQLClientPersistenceValidator` in
`validators/postgresql_client/validator.py` for the reference
implementation):

- **`prepare()`** picks its own random, unpredictable identifier (e.g.
  `uuid.uuid4().int`) - never derive it from `relation_id`, which is *not*
  stable across relation remove/re-add (see
  `test_remove_and_restore_integration`), and never use a small random space
  (e.g. a 31-bit int) that could collide across concurrent or repeated runs.
  If the identifier becomes part of a length-limited resource name (e.g. a
  SQL identifier), mask it to a backend-safe bit width rather than using the
  full value - the reference implementation masks to 63 bits
  (`uuid.uuid4().int & ((1 << 63) - 1)`) because a fixed scope-token prefix
  plus a full 128-bit decimal integer can exceed PostgreSQL's 63-byte
  identifier limit. Prefer a *fixed-width* representation (e.g. always
  zero-padded to the same number of digits) over a variable-length one, so
  discovery in `cleanup()` can validate an exact shape instead of a bare
  prefix (see below). The identifier seeds a uniquely named canary resource
  (e.g. `validator_canary_<scope_token>_<identifier>`).
- **`checkpoint()`** takes the `PersistenceState` the harness has been
  tracking, verifies the canary data is still there and has exactly
  `expected.ref` records, then unconditionally writes one more record and
  returns `PersistenceState(id=expected.id, ref=expected.ref + 1)` -
  regardless of whether the check *itself* passed. This lets subsequent
  checkpoints in the same run keep counting correctly even after a single
  check reported `FAIL`. This only covers a returned result, not an
  operational failure: if `checkpoint()` raises instead of returning (e.g.
  the connection itself fails), the runner converts that exception to an
  `ERROR` result and emits no `updated_refs` entry for that relation - the
  harness leaves the *previously tracked* `PersistenceState` untouched
  rather than clearing it (see `ValidatorInjectorExtension.post_persistence`),
  so a later checkpoint still has the old `expected.ref` to retry against; it
  just never advanced past it for this attempt.
  Verify a stable, identifier-derived marker value on each record rather
  than trusting a bare `count(*)` - a resource that was dropped and silently
  recreated from scratch could otherwise coincidentally satisfy a
  row-count-only check (see "Common patterns" below).
- **`cleanup()`** takes no arguments (it runs as a fresh process invocation
  with no state carried over from `prepare`/`checkpoint`), so it must
  discover everything to remove by name pattern rather than by identifier.
  Scope the discovery pattern to *this validator instance* - a token derived
  from both the model UUID and `relation_id`, not just a bare interface-wide
  prefix - or cleanup for one relation/interface will drop canary resources
  belonging to a different relation or interface that happens to share the
  same backend during the same test run (the runner calls `cleanup()` once
  per live relation with a registered persistence validator, so this is not
  just a concern for concurrent external runs). Even with that scoping, a
  prefix-based `LIKE` query only narrows *candidates* - re-validate each
  discovered name against the exact fixed-width shape `prepare()` produces
  (e.g. via a compiled regex `fullmatch`) before dropping it, so a same-
  prefixed but unrelated resource (e.g. a hand-created backup table) isn't
  destroyed. See the escaped, schema-scoped `information_schema.tables`
  query under "Common patterns" below, not a bare
  `LIKE 'validator_canary_%'` (`_` is itself a `LIKE` wildcard and can match
  unrelated tables).
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

Only **one** persistence validator may be registered per interface name. The
runner tracks a single `PersistenceState` per relation ID (see
`validators/runner/runner.py:151-171`); if two packages both registered
`endpoint_persistence_validators` for the same interface, their `prepare()`/
`checkpoint()` calls would overwrite each other's state, and a checkpoint
could end up verifying/advancing the wrong validator's canary data (the
runner logs a warning in this case, but does not prevent it). If an
interface genuinely needs more than one kind of persistence check, combine
them into a single validator class's `prepare()`/`checkpoint()`/`cleanup()`
rather than registering multiple entry points for the same interface.

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
   regardless of which entry-point group(s) the package declares. Either
   way, run `poetry install` (or `poetry update <package>` for a single
   path dependency) after *any* change to a package's `pyproject.toml` made
   in this step - not just a new/changed dependency or `extras` - including
   just adding the `endpoint_persistence_validators` entry-point table
   itself. The runner discovers entry points through installed package
   metadata, so leaving the environment stale after any such change (a new
   package, a new runtime/dev dependency, an `extras` change, or only a new
   entry point) can make the new/changed validator undiscoverable even
   though the source file itself is present. Do this before running the
   tests below.

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
   two charms, then from a shell with access to the model run (matching how
   `ValidatorInjectorExtension._run_persistence_on_unit` invokes it - a bare
   `run_validators` on `$PATH` won't work; it's not installed there):
   ```
   juju exec --unit <unit> -- /var/lib/juju/validators/venv/bin/run_validators --persistence prepare
   ```
   on the unit whose role this validator applies to (per its role gating -
   e.g. the requirer for a requirer-side canary). `juju exec` runs the
   command inside the unit's hook execution context, which is what sets
   `JUJU_CHARM_DIR` (required by the CLI) - invoking the binary directly over
   a plain SSH session instead won't have it set. Then disrupt the other/
   remote side of the relation (restart/scale/migrate) **and wait for it to
   fully settle** (idle/ready again) before checkpointing - checkpointing
   immediately after triggering the disruption (e.g. right after scaling to
   zero, before scaling back up and reaching idle) only proves the backend
   was unreachable mid-disruption and reports `ERROR`, not a real persistence
   check. `test_scale_in_and_scale_out.py` shows the correct ordering: it
   scales back up and waits for every affected model to reach idle
   (`multi_model_idle_for_period`) before checkpointing. Once settled, run:
   ```
   juju exec --unit <unit> -- /var/lib/juju/validators/venv/bin/run_validators --persistence checkpoint --refs '{"<relation_id>": {"id": ..., "ref": 1}}'
   ```
   and confirm a `PASS` result plus an `updated_refs` entry for that
   `relation_id` with `ref` advanced by one (`ref` is not on the
   `ValidationResult` itself - see `ValidatorRunnerResults` in
   `validators/runner/runner.py`), and finally
   ```
   juju exec --unit <unit> -- /var/lib/juju/validators/venv/bin/run_validators --persistence cleanup
   ```
   and confirm the canary resource is gone.

## Common patterns

### Discovering canary resources by name pattern

```python
import hashlib
import re

_CANARY_TABLE_PREFIX = "validator_canary_"

# prepare() masks its identifier to 63 bits (see prepare() above), so a genuine canary identifier
# never exceeds this value. The discovery regex below only checks the candidate's *shape* (prefix
# + 20 digits); this bound lets cleanup() also reject an out-of-range, shape-only look-alike
# (e.g. "..._99999999999999999999") that couldn't possibly have come from prepare().
_MAX_CANARY_IDENTIFIER = (1 << 63) - 1


def _quote_identifier(name: str) -> str:
    """Double any embedded double-quotes - the standard, connection-independent way to escape a
    Postgres identifier. Prefer this (or `psycopg2.sql.Identifier`) over raw string interpolation
    whenever a table/column name comes from a query result rather than a value you generated."""
    return '"' + name.replace('"', '""') + '"'


def _canary_scope_token(self) -> str:
    # A single fixed-width token derived from *both* the model UUID and relation_id: relation_id
    # is assigned per-model, so two different models could otherwise expose the same numeric
    # relation_id against a shared database/schema and collide, and folding relation_id into the
    # hash (rather than appending it as a raw decimal) keeps the token's length independent of
    # how large relation_id gets.
    digest_input = f"{self.charm.model.uuid}:{self.relation_id}".encode()
    return hashlib.sha256(digest_input).hexdigest()[:16]


def _canary_table_prefix(self) -> str:
    return f"{_CANARY_TABLE_PREFIX}{self._canary_scope_token()}_"


def _canary_table_regex(self) -> "re.Pattern[str]":
    # Exact-shape match: prefix + a fixed-width, zero-padded identifier (see prepare()'s masked,
    # zero-padded identifier above). Used to reject a same-prefixed but unrelated resource that a
    # bare prefix LIKE match would otherwise let through - see cleanup() below. Matching this
    # shape alone isn't sufficient though: cleanup() also checks the captured identifier against
    # _MAX_CANARY_IDENTIFIER.
    return re.compile(re.escape(self._canary_table_prefix()) + r"(?P<identifier>[0-9]{20})")


def cleanup(self) -> None:
    self._require_requires_role()
    # cleanup() runs for every relation with a registered persistence validator, including one
    # that never got as far as receiving credentials (e.g. the relation is still being set up).
    # Treat a databag with no usable credentials yet as a no-op instead of raising by opening a
    # connection anyway. Check the *same* fields _open_connection() requires (via the same
    # validate_schema() helper) rather than a narrower heuristic like "uris" alone: a relation
    # that already has "uris" but is still missing "database"/"username"/"password" would
    # otherwise fall through this guard and raise instead of no-op'ing.
    creds = self._resolve_credentials()
    if not self.validate_schema(["uris", "database", "username", "password"], creds).passed:
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
        # The LIKE query above only narrows candidates by *prefix* - it can't cheaply assert an
        # exact suffix shape. Re-check every candidate against the fixed-width regex before
        # dropping it, so a same-prefixed but unrelated table (e.g. a hand-created "..._backup")
        # is skipped instead of destroyed. The regex alone would still accept a shape-only
        # look-alike like "..._99999999999999999999" (20 nines) - larger than any identifier
        # prepare() can produce (masked to 63 bits) - so also check the captured identifier
        # against _MAX_CANARY_IDENTIFIER.
        name_regex = self._canary_table_regex()
        for schema, table_name in tables:
            match = name_regex.fullmatch(table_name)
            if not match or int(match.group("identifier")) > _MAX_CANARY_IDENTIFIER:
                continue
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
