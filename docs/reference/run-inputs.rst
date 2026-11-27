Run input groups
================

This is a proposal for `SQT-1464 <https://warthogs.atlassian.net/browse/SQT-1464>`_.
The grouped inputs and their validation are not implemented. The scheduler-side
counterpart is `test-scheduler ADRs 0001 and 0002
<https://github.com/canonical/test-scheduler/pull/169>`_.

The run spec describes what the suite owns and realizes. Resources realized
outside the suite are supplied separately. If the suite later takes over a
resource, its intent can be added to the spec while an externally provided
resource remains usable as an override.

Run spec: test intent
---------------------

The reusable spec contains a version, target, optional neighbor, test selection,
and ``model_topology``:

* ``single-model`` (default): target and optional neighbor share a model.
* ``cross-model``: separate models share a controller.
* ``cross-controller``: separate models use separate controllers.

Both cross-model forms require a neighbor. The suite currently calls either
form CMR. See the `draft run spec example
<https://github.com/canonical/charm-integration-testing/pull/1328>`_.

State: overrides
----------------

A proposed separate state input carries ``current_state``, bundles keyed by
model, existing clouds, controllers and models, and eventually other provided
resources. These override resources the suite would otherwise create and must
be checked against the spec and live state before mutation. Credentials are
referenced, not embedded.

The schema and transport are undecided, as are checkpoint writing, validation
of hand-edited state, cleanup ownership, and size limits. References may not
be portable across runners; state files and bundles still require access
controls and secret handling.

Environment and setup: where
----------------------------

Each role selects an environment in ``static/environments.yaml``. The setup
configuration supplies the runner, setup mode, credential references, and
bootstrap and model configuration. Under the current model, setup provides
clouds, S3 configuration when needed, and the Juju client selected by
``juju_channel``. Setup supplies the effective platform to the suite;
architecture is fixed by the runner or cloud.

The proposed setup contract takes per-role features: exactly one substrate
(``k8s-api``, ``lxd-api``, or ``o7k-api``) and zero or more services such as
``s3-api``. Architecture features are not setup inputs. This would replace the
workflow's hardcoded ``CLOUD_FEATURES``. Slot matching and feature discovery
are described in the scheduler ADRs.

The proposed ``reservation_id`` is opaque scheduler bookkeeping, written to
the workflow run name; neither setup nor the suite interprets it.

Reporting and run options
-------------------------

Reporting uses ``execution_id`` and ``test_observer_environment``. Debug
settings, log level and ad-hoc filters such as ``-k`` are run options rather
than reusable test intent.

Current workflow inputs
-----------------------

The following maps today's ``charm-testing.yaml`` inputs to the proposed
groups. The current workflow still takes individual inputs; this table does
not imply the new contract is implemented.

.. list-table::
   :header-rows: 1
   :widths: 60 40

   * - Current inputs
     - Proposed group
   * - ``charm_under_test``, ``charm_endpoint``, ``neighbor``,
       ``neighbor_endpoint``, ``channel``, ``revision``,
       ``downgrade_revision``, ``series``, ``same_controller``
     - Run spec; ``same_controller`` is represented by ``model_topology``
   * - ``target_environment``, ``neighbor_environment``, ``juju_channel``
     - Environment and setup
   * - ``execution_id``, ``test_observer_environment``
     - Reporting
   * - ``debug_sleep_after``, ``debug_extra_ssh_imports``,
       ``debug_sleep_seconds``, ``log_level``
     - Run options

The current environment inputs also identify slots in the workflow run name.
The proposed ``reservation_id`` separates that bookkeeping from setup.
