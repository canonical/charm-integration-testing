Validators
==========

Validators are small Python packages that run inside a Juju unit after each charm deployment and check that a relation is working correctly. They complement the standard test suite by verifying integration behaviour from the charm's own perspective.

Architecture
------------

The validator framework consists of three layers:

``validators/base``
  Defines the shared data models and the ``BaseValidator`` abstract class that all validators must implement.

``validators/runner``
  Provides the ``run_validators`` CLI entry point. It is installed inside each unit under test, discovers validators via Python entry points, instantiates the Ops runtime, and runs every registered validator against every active relation. Results are emitted as JSON to stdout.

``validators/<interface>``
  One package per interface (e.g. ``validators/postgresql_client``). Each package registers a validator class under the ``endpoint_validators`` entry-point group, keyed by interface name.

How validators are injected
---------------------------

When ``VALIDATORS_PATH`` is set, the ``ValidatorInjectorExtension`` is active. After each validation phase it:

1. Uses ``scp`` to copy the ``validators/`` directory to ``/var/lib/validators/`` on the unit.
2. Creates a virtualenv and installs every package found there.
3. Runs ``run_validators --level <level>`` and parses the JSON output.
4. Raises an error (failing the test) if any result has status ``FAIL`` or ``ERROR``.
   Results with status ``SKIPPED`` are emitted as warnings and do not fail the test.

If ``VALIDATORS_PATH`` is not set the step is silently skipped.

Validation levels
-----------------

``simple``
  Fast, non-destructive checks. Suitable for every test run.

``deep``
  More thorough checks, potentially slower or requiring extra permissions.

``uat``
  User acceptance checks intended for production gate runs.

Level fallback
--------------

When the runner is invoked with ``--level deep`` (or ``uat``) and a validator
does not support that level, it returns ``SKIPPED``. The runner automatically
retries with the next lower level (``deep`` -> ``simple``, ``uat`` -> ``deep``
-> ``simple``) until it gets a real result or exhausts all levels. The
``ValidationResult.level`` field always reflects the level that was actually
run, which may be lower than the level that was requested.

Writing a new validator
-----------------------

1. Create a new package directory under ``validators/``, e.g. ``validators/my_interface/``.
2. Implement ``BaseValidator.validate()`` returning a ``ValidationResult``.
   For levels your validator does not support, return ``self._skipped_result(level)``
   so the runner can fall back to a lower level automatically.
3. Register it in ``pyproject.toml`` under ``[project.entry-points."endpoint_validators"]``, keyed by the interface name:

   .. code-block:: toml

      [project.entry-points."endpoint_validators"]
      my_interface = "validators.my_interface:MyInterfaceValidator"

The runner will discover and invoke the validator automatically for any relation whose interface matches the key.

Data integrity and persistence validation
------------------------------------------

Persistence validators check that data survives disruptive operations (e.g. restarting a
controller, upgrading/downgrading a charm, scaling, migrating a model) rather than just checking
that a relation currently works. A relation remove/re-add is handled differently: it changes
``relation_id`` (see below), so the harness re-seeds fresh canary data for the new ID rather than
verifying the old data survived the operation. They implement ``validators/base``'s
``BasePersistenceValidator`` and are registered under a separate
``endpoint_persistence_validators`` entry-point group, keyed by interface name (a package may
register both an ``endpoint_validators`` and an ``endpoint_persistence_validators`` entry, or
only the latter).

Lifecycle
~~~~~~~~~

A persistence validator implements three methods, called at different points in the test suite:

``prepare() -> PersistenceState``
  Seeds canary data (e.g. a marker row) for a relation and returns a ``PersistenceState``
  identifying it. Called when a relation is first established, and again whenever a relation is
  re-established with a fresh ``relation_id`` (e.g. by ``test_idempotent_redeploy`` or
  ``test_deploy_target_old_revision`` after the preceding ``test_teardown`` already ran
  ``cleanup()`` and removed the relation, or when a neighbor relation is re-added).

``checkpoint(expected: PersistenceState) -> tuple[ValidationResult, PersistenceState]``
  Verifies the canary data seeded by ``prepare()`` (or a previous ``checkpoint()``) is still
  present, returning both the ``ValidationResult`` for this check and the updated
  ``PersistenceState`` to pass to the next call. Called after a disruptive operation to confirm
  nothing was lost.

``cleanup() -> None``
  Removes all canary data written by this validator instance. Called during test teardown.

Raise ``PersistenceNotApplicable`` from any of these methods when persistence checking doesn't
apply to the current side of the relation (e.g. only the ``requires`` role can seed data, as
``PostgreSQLClientPersistenceValidator`` does); the
runner treats this as "no applicable validator" rather than a failure.

``PersistenceState`` (``validators/base``) is the opaque state passed between calls: ``id`` is a
validator-chosen identifier for the canary (stable for the lifetime of the canary data it names,
unlike Juju's ``relation_id`` which changes across a relation remove/re-add), ``ref`` is a
monotonically increasing counter used to detect lost writes, and ``token`` is a random value the
validator mints in ``prepare()`` and matches on in ``checkpoint()``. The token exists because
``id`` and ``ref`` are reproducible: a canary resource dropped and recreated from scratch would
otherwise reproduce the same identifier-derived values and report a false ``PASS``. The harness tracks state keyed by
``PersistenceKey`` (controller, model, unit, and ``relation_id`` together - see
``charm_integration_testing/juju/models.py``), so a relation remove/re-add invalidates the old
entry and runs ``prepare()`` again for that relation under its new ``relation_id``, rather than
remapping the old ``id`` onto it - preserving the design intent of a stable ``id`` across that
scenario would require the harness to discover the new ``relation_id`` before checkpointing, which
it cannot do today.

CLI and wire format
~~~~~~~~~~~~~~~~~~~~

``run_validators`` exposes persistence operations via two additional flags:

``--persistence {prepare,checkpoint,cleanup}``
  Selects which lifecycle method to invoke, for every active relation whose interface has a
  registered persistence validator. Can be combined with ``--level`` in the same invocation, or
  used on its own for a persistence-only run.

``--refs '{"<relation_id>": {"id": <identifier>, "ref": <ref>, "token": <token>}}'``
  A JSON dict mapping relation IDs to their current ``PersistenceState``, required when
  ``--persistence checkpoint`` is used (``checkpoint()`` needs the state ``prepare()`` returned).

The runner's JSON output includes an ``updated_refs`` field alongside the usual functional
``results``: a dict of the same shape as ``--refs``, containing each relation's new
``PersistenceState`` after the requested operation. Callers (the ``ValidatorInjectorExtension``,
via its ``post_persistence`` hook) are responsible for persisting ``updated_refs`` across test
steps (e.g. in ``persistence_state``, keyed by controller/model/unit) and passing the relevant
entries back in via ``--refs`` on the next ``checkpoint`` call.

For ``--persistence cleanup``, the output also includes a ``cleaned_relation_ids`` field: the
relation IDs that cleanup actually visited (i.e. had a live relation with a registered
persistence validator at cleanup time), regardless of whether the cleanup call itself succeeded.
``post_persistence`` only drops tracked ``persistence_state`` entries for relation IDs that
appear here *and* did not produce a FAIL/ERROR result - a tracked relation ID that cleanup never
visited (e.g. its relation was already removed, or its interface's persistence validator failed
to load) keeps its tracking entry, so orphaned canary data isn't silently forgotten.

Writing a new persistence validator
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

See the ``develop-persistence-validator`` skill
(``.agents/skills/develop-persistence-validator/SKILL.md``) for a full walkthrough, using
``validators/postgresql_client`` as the reference implementation.
