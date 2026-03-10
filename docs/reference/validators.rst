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

If ``VALIDATORS_PATH`` is not set the step is silently skipped.

Validation levels
-----------------

``simple``
  Fast, non-destructive checks. Suitable for every test run.

``deep``
  More thorough checks, potentially slower or requiring extra permissions. Not yet widely implemented.

``uat``
  User acceptance checks intended for production gate runs.

Writing a new validator
-----------------------

1. Create a new package directory under ``validators/``, e.g. ``validators/my_interface/``.
2. Implement ``BaseValidator.validate()`` returning a ``ValidationResult``.
3. Register it in ``pyproject.toml`` under ``[project.entry-points."endpoint_validators"]``, keyed by the interface name:

   .. code-block:: toml

      [project.entry-points."endpoint_validators"]
      my_interface = "validators.my_interface:MyInterfaceValidator"

The runner will discover and invoke the validator automatically for any relation whose interface matches the key.
