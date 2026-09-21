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
        again whenever the relation is removed and re-added (which changes relation_id, so the
        previous canary data - if any survived - is no longer reachable under the new state key
        and fresh data is needed), and again when a test run resumes from a prior `State.DEPLOYED`
        checkpoint (`seed_persistence_state_for_resumed_run` in conftest.py) - in that last case the
        relation_id is unchanged and canary data from the previous process may already exist in the
        same namespace. prepare() must not assume the canary resource it creates doesn't already
        exist (e.g. from a previous run's leftover state, or a re-run of a failed prepare), and
        should always return a state usable from a clean slate."""
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
        # canary resource undiscoverable and orphaned. Then write one tagged row/record ...
        # Mint a second, independent random value and write it alongside the canary data. This
        # token - not the identifier-derived resource name - is what checkpoint() matches on:
        # the name is reproducible, so a resource dropped and recreated from scratch would
        # otherwise satisfy the check and report a false PASS.
        token = uuid.uuid4().hex
        # commit (or use an autocommit connection) before returning - a transactional backend
        # left uncommitted here can roll back the write, so the next checkpoint() sees no data.
        return PersistenceState(id=identifier, ref=1, token=token)

    def checkpoint(self, expected: PersistenceState) -> tuple[ValidationResult, PersistenceState]:
        """Verify canary data survived, then extend it. Called after each disruption."""
        self._require_requires_role()
        # expected.id and expected.ref both come from --refs, a (possibly restored/malformed)
        # PersistenceState rather than values prepare() just minted - validate expected.id is in
        # the range prepare() could have produced (e.g. 0..(1 << 63) - 1) before interpolating it
        # into any resource name or query. Also validate expected.ref against the range your
        # prepare() declares (e.g. PostgreSQL starts at ref=1, but a KV validator might start at
        # ref=0; see the validator-specific initial state below). An out-of-range id could
        # otherwise produce a truncated/different identifier and silently target the wrong
        # resource; an invalid ref (outside your declared range) could let an empty or partially
        # recreated canary satisfy `actual == expected.ref` and report a false PASS. Also reject
        # an empty expected.token: prepare() always mints one, so an empty value can only come
        # from a state serialised before the token existed, or otherwise restored/malformed -
        # matching on it would count records carrying no token at all. Raise before any
        # read/write if any of these is invalid.
        # ... read back and assert the tagged row/record count matches expected.ref (filter on the
        # random token written by prepare(), not a bare row count - see "Common patterns" below) ...
        result = self._make_result(level="deep", checks=[...])
        # Gate the extra write and the returned state on the *overall* result, not just the one
        # assertion above - if this method later adds more checks (e.g. a schema/connection
        # check), a single "count matches" assertion could still be true while the combined
        # result is FAIL, and writing/advancing here would ignore that. The harness only carries
        # a returned state forward on PASS (see the design point below), so writing/advancing
        # when result.status != "PASS" would grow the backend's actual state past what the
        # harness will ever compare against again, masking the mismatch instead of letting a
        # later checkpoint re-detect it. Commit the write (or use autocommit) for the same reason
        # as prepare() above.
        if result.status == "PASS":
            # ... write one more tagged row/record, carrying the same token forward ...
            new_state = PersistenceState(id=expected.id, ref=expected.ref + 1, token=expected.token)
        else:
            new_state = expected
        return result, new_state

    def cleanup(self) -> None:
        """Drop all canary data.

        Within the test harness, invoked only from the `test_teardown` state transition, never
        from `prepare()`/`checkpoint()` or mid-transition - though a single pytest session can
        execute `test_teardown` more than once (e.g. an injected bridge transition ahead of a
        later redeployment), so this is "once per `test_teardown` invocation," not strictly "once
        per pytest session." (Manually invoking `run_validators --persistence cleanup` directly,
        e.g. for iteration - see "Manually verify" below - bypasses the harness and can call this
        method at any time.) A relation remove/re-add (e.g. in `test_remove_and_restore_integration`)
        instead invalidates the *tracked* `PersistenceState` for the old relation_id and calls
        `prepare()` again for the new one. Note that this is a real, accepted limitation, not just
        a delay: the discovery namespace (see `_canary_table_prefix`/"Common patterns" below) is
        derived from the *current* relation_id, so `cleanup()` for the new relation cannot find
        canary data left behind under the old relation_id - that old resource remains orphaned
        rather than being swept up by a later cleanup call, unless prior relation_ids were
        recorded somewhere durable. Neither this relation's own databag (removed along with the
        relation) nor `ops.StoredState` can provide that durability: `run_validators` constructs
        a fresh in-memory Ops `Framework` (`SQLiteStorage(":memory:")`) on every CLI invocation
        (see `validators/runner/runner.py`), so anything written to `StoredState` during one
        `prepare()`/`checkpoint()`/`cleanup()` call is gone before the next one runs - cleanup
        cannot enumerate prior relation_ids from either store. If your backend needs the stronger
        guarantee, record prior relation_ids somewhere that outlives both the relation and the
        validator process itself - e.g. the charm's own peer relation databag (if one exists),
        which Juju persists independently of any given relation - so cleanup can enumerate and
        drop them too. Must still be safe to call when no canary resource remains (no-op, not an
        error) - e.g. if `prepare()` was never reached for a given relation.
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
  `expected.ref` records, and only on a *passing* check writes one more
  record and returns
  `PersistenceState(id=expected.id, ref=expected.ref + 1, token=expected.token)`.
  This matters because `ValidatorRunner.checkpoint_all()` only carries a
  returned state forward into `updated_refs` when the result is `PASS` - a
  `FAIL`/`ERROR` result leaves the harness's tracked state untouched. If
  `checkpoint()` wrote the extra record and advanced the returned state
  unconditionally, that write would still land in the backend even though
  the harness keeps comparing future checkpoints against the old, frozen
  `expected.ref` - permanently masking the original mismatch behind
  untracked drift (or, worse, letting a later checkpoint coincidentally
  match the stale `expected.ref` and report a false `PASS`). On `FAIL`,
  return `expected` unchanged and skip the write entirely, so a later
  checkpoint re-detects the exact same data loss consistently instead of
  drifting. This only covers a returned result, not an operational failure:
  if `checkpoint()` raises instead of returning (e.g. the connection itself
  fails), the runner converts that exception to an `ERROR` result and emits
  no `updated_refs` entry for that relation either - the harness leaves the
  *previously tracked* `PersistenceState` untouched rather than clearing it
  (see `ValidatorInjectorExtension.post_persistence`), so a later checkpoint
  still has the old `expected.ref` to retry against. That retry is not
  perfectly safe either: if the write itself is durably committed to the
  backend but the connection/response fails before `checkpoint()` can
  return normally (an ambiguous write - see "Common patterns" below), the
  backend now actually has `expected.ref + 1` tagged records while the
  harness still expects `expected.ref`, so the next retry's read-back will
  see one extra record and can report a false `FAIL` (or, if it advances
  again, silently drift the state by one). This response-loss ambiguity is
  not something a single atomic write (e.g. one autocommitted `INSERT`) can
  eliminate - atomicity only guarantees the write itself is all-or-nothing
  on the backend, not that its response reaches `checkpoint()`; the two are
  independent failure modes. When `checkpoint()` raises after a disruption,
  the original data persists but we cannot distinguish "write succeeded but
  response was lost" from "write never executed" - a simple retry cannot
  resolve this ambiguity. Document that operators must reseed (re-run
  `prepare()`) to establish a fresh baseline after a raised exception before
  repeating the disruptive operation or continuing testing. Alternatively,
  mark the scenario inconclusive rather than retrying blindly, since a retry
  cannot disambiguate the two failure modes.
  Verify a random, per-run token value on each record rather than trusting
  a bare `count(*)` - a resource that was dropped and silently recreated
  from scratch could otherwise coincidentally satisfy a row-count-only
  check (see "Common patterns" below). The token must be random, not
  derived from `expected.id`/`expected.ref`: those values are reproducible,
  so a backend that lost the data and recreated it (resetting a sequence,
  for example) would regenerate the same derived marker and pass falsely.
  Generate the token in `prepare()`, write it alongside the canary data,
  return it as `PersistenceState.token`, and match on it in `checkpoint()`;
  reject an empty `expected.token` (it can only come from a state
  serialised before the token existed, or otherwise restored/malformed).
  `expected.id` comes
  from `--refs`, a (possibly restored/malformed) `PersistenceState` rather
  than a value `prepare()` just minted - validate it's within the range
  `prepare()` could have produced (e.g. the reference implementation's
  `_canary_table_name()` rejects anything outside `0..(1 << 63) - 1`) before
  interpolating it into any resource name or query, and raise before any
  read/write if it's out of range; otherwise a truncated/different
  identifier could silently target the wrong resource instead of failing
  safely.
- **`cleanup()`** takes no arguments (it runs as a fresh process invocation
  with no state carried over from `prepare`/`checkpoint`), so it must
  discover everything to remove by name pattern rather than by identifier.
  Scope the discovery pattern to *this validator instance* - a token derived
  from the model UUID, `relation_id` and unit name, not just a bare
  interface-wide prefix - or cleanup for one relation/interface will drop
  canary resources belonging to a different relation or interface that
  happens to share the same backend during the same test run (the runner
  calls `cleanup()` once per live relation with a registered persistence
  validator, so this is not just a concern for concurrent external runs).
  The unit name matters because the runner injects and runs persistence
  validators on *every* unit of the application: two units of the same
  application share both `model.uuid` and `relation_id` while owning
  separate canary resources. This model+relation+unit scoping
  still does not distinguish two *simultaneous* test runs against the same
  relation and backend - the reference implementation has no execution-
  scoped token beyond model UUID, `relation_id` and unit name, so one run's
  `cleanup()` can still drop another concurrent run's canaries for that same
  relation.
  Running more than one test session against the same deployed relation at
  once is not supported by this scoping scheme; if that's a real
  requirement for your backend, add an additional execution-scoped token
  (e.g. a value persisted somewhere that outlives a single `run_validators`
  invocation) rather than relying on model UUID + `relation_id` alone. Even
  with that scoping, a
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
  interface's applicable non-peer side is - only `requires`/`provides` are
  currently supported; the runner's `_iter_persistence_targets()` and
  `_find_relation_by_id()` skip `peer` relations entirely, so a persistence
  validator registered against a peer role would never receive `prepare()`,
  `checkpoint()`, or `cleanup()`) - the runner treats a raised
  `PersistenceNotApplicable` as a silent skip, not a failure. Add a small
  `_require_requires_role()` helper to avoid repeating the check.
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
   just as functional validators do. Whether or not the package is new, make
   sure `validators-base` is declared in its `pyproject.toml` `dependencies`
   (see `develop-validator`'s step 10 self-review checklist) - the generated
   class imports `validators.base` directly, and a persistence-only package
   has no other dependency that would pull it in. A functional validator
   (`endpoint_validators` entry point) is *not* a hard prerequisite: the
   runner discovers `endpoint_validators` and `endpoint_persistence_validators`
   independently, so a package may register the persistence group alone, or
   both groups together - don't implement a functional validator unless this
   interface actually needs one. The interface package created by this
   workflow must still register `endpoint_persistence_validators` (per the
   acceptance criteria below); registering neither group would make it
   undiscoverable and silently deploy no validator at all. In practice, most interfaces already have a
   functional validator, and persistence validators are usually additive to
   that existing package.

2. Identify what "canary data" means for this interface: a row in a table, a
   key in a KV store, an object in a bucket, a topic message, etc. It must be:
   - Cheap to create and verify.
   - Uniquely identifiable via a random identifier chosen at `prepare()` time.
     (Backend-specific naming conventions may format this into a resource name
     or key string, but `PersistenceState.id` returned from `prepare()` and
     passed to `checkpoint()` must be an integer, as declared in
     `validators/base/validator.py`.)
   - Tagged with a second, independent random value (`PersistenceState.token`)
     minted in `prepare()` and matched on in `checkpoint()`. The resource name
     is derived from `id`, which is reproducible across a drop-and-recreate, so
     identity must be established by the token rather than by the name or by a
     value derived from `id`/`ref`.
   - Discoverable at `cleanup()` time via a durable name/key pattern
     derived from this validator instance (e.g. a resource name prefix for a
     SQL table, a key prefix for a KV store, an object key prefix in a
     bucket, or a separately named/keyed tracking record for a topic where
     individual messages aren't independently discoverable), without
     needing the identifier `prepare()` returned.

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
   path = "./validators/postgresql_client", develop = true, extras = ["dev"]
   }` - preserve any other existing extras already listed alongside `"dev"`)
   - if it's missing `"dev"`, add it. If
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
     with a fresh identifier and `ref` set to the initial value your validator
     declares (the reference PostgreSQL implementation starts at `ref=1`, but
     other backends may use a different initial value; see the validator-specific
     initial state pattern below). Also cover calling `prepare()` twice while
     forcing the same identifier both times (e.g. patch/mock the identifier
     source so it returns a fixed value instead of a fresh `uuid.uuid4()`) and
     assert the resulting resource/state is still usable by `checkpoint()`. The
     reference `prepare()` always mints a fresh random identifier, so this
     implementation alone - but a non-idempotent `CREATE`-style
     implementation could still pass a "creates a fresh resource" test while
     failing the second time it targets an identifier that already has a
     canary resource, so cover it explicitly rather than relying on the
     identifier's randomness to avoid ever exercising this path.
   - `checkpoint()` passes when the check matches `expected.ref`, fails when
     it doesn't. On `PASS`, it advances the backend-specific canary state
     (e.g. writing a new tagged row/record for a SQL-style backend;
     overwriting a value, creating a new version, or writing another
     backend-specific marker for a KV store/bucket/topic) and returns
     `PersistenceState(id=expected.id, ref=expected.ref + 1, token=expected.token)`; on `FAIL`, it must not write
     anything and returns `expected` unchanged, so a later retry re-checks
     the same expected count instead of drifting past the failure (see the
     `checkpoint()` design point above). Also cover that a check based only
     on a bare existence/count check would be insufficient - assert it scopes
     on the token written by `prepare()`, not just "the resource
     exists" or "the count matches" (for SQL backends this means filtering
     the row count query on the token column, not a bare `count(*)`; for a
     KV store, bucket, or topic, the equivalent is asserting the read/list
     is scoped to the specific key/object/message identifier). Also cover a
     resource dropped and recreated from scratch: recreating it with the same
     count/ref state reproduces the same observable shape, so only the random
     token distinguishes the original canary from the recreated one - assert
     the recreated case FAILs rather than reporting a false PASS. (For SQL
     backends, a table recreated from scratch also resets any database-generated
     row identity such as a `SERIAL` `id`, so the reference implementation
     additionally asserts the original `id` is still present; treat that as an
     implementation-specific strengthening, not a requirement for backends
     without such an identity.)
     Also cover
     `checkpoint()` rejecting an out-of-range `expected.id` (e.g. negative,
     or one past the maximum your `prepare()` can produce) and rejecting an
     invalid `expected.ref` (e.g. outside the range your validator declares -
     the reference PostgreSQL implementation rejects ref <= 0, but another
     backend might allow ref=0 as valid) before any read/write, the way the
     reference implementation's `test_raises_when_expected_identifier_is_out_of_range`/
     `test_raises_when_expected_identifier_is_negative` do. Also cover
     rejecting an empty `expected.token`, which can only come from a state
     serialised before the token existed or otherwise restored/malformed.
   - `cleanup()` discovers and drops every matching canary resource, is a
     no-op when none exist, and safely quotes any discovered identifier
     before using it in a DDL statement (where applicable to the backend).
     Also cover the destructive-safeguard cases the cleanup() design point
     above requires, not just the happy path: a same-prefixed but unrelated
     resource (e.g. a hand-created backup) must survive cleanup, an
     out-of-range look-alike identifier (larger than any `prepare()` could
     have produced) must be rejected before dropping, and - for SQL
     backends - discovery must search every schema (an unqualified
     `CREATE TABLE` can resolve through `search_path` to a schema other than
     `current_schema()`, and the path can change between invocations), and
     escape any `LIKE` wildcards in the prefix pattern. For PostgreSQL
     specifically, use `information_schema.tables` (all schemas, not just
     `current_schema()`) with identifier quoting as in the reference
     implementation's
     `test_rejects_discovered_tables_that_only_share_the_prefix`/
     `test_rejects_discovered_tables_with_an_out_of_range_identifier`/
     `test_searches_all_schemas_for_discovery`/
     `test_escapes_like_wildcards_in_prefix_pattern` tests. Implementers of
     other SQL dialects (e.g. MySQL) must provide equivalent schema-scoping
     and identifier-escaping safeguards appropriate to that backend's
     capabilities and syntax. A prefix-only implementation could otherwise
     pass a "drops matching resources" test while still deleting unrelated
     data.

6. Run the package's unit tests and the monorepo-wide checks:
   ```
   poetry run pytest validators/<name>
   poetry run pytest validators/runner    # confirm discovery/wiring still passes
   ./scripts/format.sh
   ./scripts/lint.sh
   ```
   `scripts/lint.sh` is the same check the repository CI/reviewers require -
   beyond ruff/mypy it also runs bandit, yamlfix, and markdownlint-cli2, so
   running only a subset (e.g. just `ruff check`/`mypy`) can pass locally
   while still failing the established repository checks. Fix any issues
   before continuing.

7. No charm-specific harness changes are needed to wire persistence in:
   `ValidatorInjectorExtension.post_persistence` and
   `JujuClient.validate_model(persistence=...)` are interface-agnostic and
   pick up any registered `endpoint_persistence_validators` entry
   automatically, once `test_deploy` (prepare), the disruptive tests
   (checkpoint), and `test_teardown` (cleanup) run for a model containing
   this interface.

8. Manually verify end-to-end if a live model is available: deploy the
   two charms, then run the full test suite against that model at least
   through `test_deploy` (e.g. `pytest ... --current-state=empty_model
   -k "test_deploy and not test_deploy_target_old_revision"`, or an exact
   node id like `charm_integration_testing/test_suite/test_deploy.py::test_deploy` -
   a bare `-k test_deploy` also matches `test_deploy_target_old_revision`,
   which performs an unrelated old-revision transition instead of stopping
   at the initial deployment) -
   this is what actually injects the validators venv onto each unit via
   `ValidatorInjectorExtension._inject_validators()`
   (`/var/lib/juju/validators/venv/bin/run_validators` does not exist on a
   plain `juju deploy`; only the harness creates it). Once injected, from a
   shell with access to the model you can also invoke `run_validators`
   directly to iterate faster (matching how
   `ValidatorInjectorExtension._run_persistence_on_unit` invokes it - a bare
   `run_validators` on `$PATH` won't work; it's not installed there). On a
   Kubernetes charm running under Juju older than 4.0, add `--operator` so
   the command reaches the operator (charm) container rather than the
   workload container, where the validators venv doesn't exist (see
   `ValidatorInjectorExtension`'s `operator=is_k8s` usage); omit it on
   machine charms, and omit it on Juju 4+ regardless of substrate, since the
   flag was removed there and passing it either has no effect or errors
   depending on your Juju client version - check with `juju version`
   first:
   ```
   juju exec -m <model> --unit <unit> [--operator] -- /var/lib/juju/validators/venv/bin/run_validators --persistence prepare
   ```
   on the unit whose role this validator applies to (per its role gating -
   e.g. the requirer for a requirer-side canary). `juju exec` runs the
   command inside the unit's hook execution context, which is what sets
   `JUJU_CHARM_DIR` (required by the CLI) - invoking the binary directly over
   a plain SSH session instead won't have it set. Then disrupt the component under test (restart/scale/
   migrate) **and wait for it to fully settle** (idle/ready again) before
   checkpointing - checkpointing immediately after triggering the disruption
   (e.g. right after scaling to zero, before scaling back up and reaching
   idle) may produce `ERROR` (if no units are available for the validator to
   run on), a skipped result (if the validator finds no applicable units), or
   other failures (if units on the targeted side are affected). This does not
   constitute a valid persistence check - wait for full settlement. Which side
   to disrupt depends on the scenario: most tests (e.g. `test_scale_in_and_scale_out.py`)
   disrupt `target_application`, whose role (provider or requirer) varies by
   integration, and then checkpoint every model that holds applicable state
   (the target model, plus the neighbor model in a CMR where the target is the
   provider), while some (e.g. `test_pod_deletion.py`) disrupt the application
   the persistence validator itself runs on - checkpoint whichever unit you ran
   `prepare()` on above, regardless of which side was disrupted.
   `test_scale_in_and_scale_out.py` shows the correct ordering: it
   scales back up and waits for every affected model to reach idle
   (`multi_model_idle_for_period`) before checkpointing. Once settled, run:
   ```
   juju exec -m <model> --unit <unit> [--operator] -- /var/lib/juju/validators/venv/bin/run_validators --persistence checkpoint --refs '{"4": {"id": 123, "ref": 1, "token": "9f3c..."}}'
   ```
   (`-m <model>` specifies which Juju model the unit belongs to; `--refs`
   must be valid JSON - `run_validators` parses/rejects it before
   `checkpoint()` ever runs, so a placeholder like `...` is not usable here;
   substitute the real relation ID and the `id`/`ref`/`token` values from the
   `PersistenceState` you're resuming, e.g. as printed by a prior
   `prepare()`/`checkpoint()` run's `updated_refs`. Pass the state through
   verbatim: `token` is required and non-empty, and it is the token - not the
   identifier - that `checkpoint()` matches the canary records on, so
   reconstructing the state without it is rejected outright)
   and confirm a `PASS` result plus an `updated_refs` entry for that
   `relation_id` with `ref` advanced by one (`ref` is not on the
   `ValidationResult` itself - see `ValidatorRunnerResults` in
   `validators/runner/runner.py`), and finally
   ```
   juju exec -m <model> --unit <unit> [--operator] -- /var/lib/juju/validators/venv/bin/run_validators --persistence cleanup
   ```
   (`-m <model>` specifies which Juju model the unit belongs to; this is
   required for consistency with the prior `prepare()` and `checkpoint()`
   commands) and confirm the canary resource is gone.

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
    # A single fixed-width token derived from the model UUID, relation_id *and* unit name:
    # relation_id is assigned per-model, so two different models could otherwise expose the same
    # numeric relation_id against a shared database/schema and collide; the unit name is needed
    # because the runner injects and runs persistence validators on every unit of the
    # application, and two units of the same application share both model UUID and relation_id
    # while owning separate canary tables. Folding these into the hash (rather than appending
    # them as raw values) keeps the token's length independent of how large relation_id gets.
    unit_name = self.charm.model.unit.name
    digest_input = f"{self.charm.model.uuid}:{self.relation_id}:{unit_name}".encode()
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
            # CREATE TABLE elsewhere is unqualified, so it resolves through search_path - which can
            # land in a schema other than current_schema(), and that path can change between
            # invocations. Search every schema in information_schema.tables rather than filtering
            # to current_schema(), or a table created in another schema could be left behind. Also
            # restrict to base tables: a view/foreign table sharing the prefix would make DROP
            # TABLE fail and abort cleanup, leaving any remaining canary tables undropped.
            cur.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "  # nosec B608
                "WHERE table_type = 'BASE TABLE' "
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
    def _connect(self, uri: str) -> "Connection": ...

class PostgreSQLClientValidator(_PostgreSQLConnectionMixin, BaseValidator): ...
class PostgreSQLClientPersistenceValidator(_PostgreSQLConnectionMixin, BasePersistenceValidator): ...
```

`_connect` takes the selected connection URI string (e.g. one entry from a
`uris` field split on `,`), not the raw credentials mapping - call
`_resolve_credentials()` first, pick a URI from it, then pass that URI to
`_connect`, matching `PostgreSQLClientValidator`/
`PostgreSQLClientPersistenceValidator`'s actual call sequence in
`validators/postgresql_client/validator.py`.

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
  `ref` values only on success - a prior checkpoint's failure keeps `expected`
  unchanged (the harness only advances tracked state on `PASS`, see the
  `checkpoint()` design point above), so a retry can see the same `ref` it
  saw last time. Always trust the `expected` argument passed in rather than
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
- `./scripts/format.sh` and `./scripts/lint.sh` pass (ruff, mypy, bandit,
  yamlfix, and markdownlint-cli2 for root `*.md` files; if the validator
  includes nested markdown documentation, run markdownlint-cli2 explicitly on
  those files).
- No hardcoded charm names, model names, or relation ids inside the
  validator code.
