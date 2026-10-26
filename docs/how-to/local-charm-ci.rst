Local charm CI runner
=====================

The provider-neutral ``ci_runner`` module runs this repository's integration
suite against a built ``.charm`` file. It uses the built artifact's metadata,
configuration schema, and manifest, not a published version of the target.
Neighbor and transitive dependency metadata come from Charmhub using the
existing HTTP client's bounded retries and exponential backoff. API errors
propagate; unavailable releases are logged and excluded from discovery.
Test and deployment failures are not retried wholesale.

For host provisioning and a minimal GitHub Action wrapper, see the
``charm-integration-testing`` directory in
`canonical/sqa-shared-ci <https://github.com/canonical/sqa-shared-ci>`_.
The same shell entry point works outside GitHub.

Direct usage
------------

Install the repository dependencies with ``poetry install``. Build your charm
with Charmcraft, and register a Juju cloud. Kubernetes clouds need the chaos
tooling and per-cloud kubeconfig described in
:doc:`run-and-debug-tests-locally`. Machine charms can use an LXD cloud.
Then run from this repository's root:

.. code:: bash

   export VALIDATORS_PATH="$PWD/validators"
   export KUBECONFIG_local_k8s=/path/to/kubeconfig
   poetry run python -m ci_runner \
       --charm-path /path/to/target_amd64.charm \
       --cloud local-k8s \
       --platform kubernetes \
       --output-dir /path/to/new-results

For machine charms, pass ``--platform machine --cloud localhost``.
Each scenario gets a fresh controller/model topology managed and cleaned up
by the suite's resource registry. Clouds themselves are not removed. Use
disposable infrastructure: tests include disruptive operations, and neither
the runner nor chaos tooling provides isolation from other cluster tenants.

The default discovers every opposite-role endpoint/neighbor combination
supported by the selected platform and architecture, using the target's local
metadata and the shipped charm overrides. The full suite is selected for
each scenario; the existing scheduler orders transitions and bridges
prerequisites. Normal applicability skips remain visible in JUnit results.
Discovery can generate many expensive deployments; use ``--plan`` first to
save the scenarios without creating test infrastructure.

No matching scenarios is an error, not a successful empty run. Output
directories cannot contain a previous run's ``scenarios.json``. Each scenario
writes its configuration, bundles, Mermaid output, JUnit report, and resource
logs under ``scenario-NNNN``. A failure does not stop later scenarios. Missing
or empty JUnit output fails the run.

Configuration
-------------

Pass ``--config /path/to/charm-ci.json``. Unknown configuration keys are errors.
All fields are optional:

.. code:: json

   {
     "arch": "amd64",
     "base": "24.04",
     "channel": "latest/stable",
     "resources": {"db-image": "ghcr.io/example/db:1"},
     "endpoints": ["database"],
     "neighbors": ["database-client"],
     "pytest_args": ["-k", "not test_upgrade_controller"],
     "overrides": "./charm-overrides"
   }

``arch`` defaults to the host architecture; ``base`` defaults to the first
Ubuntu manifest base supporting it. Only packed artifacts compatible with that
architecture/base are accepted. ``channel`` supplies the override/history
context; it never selects a published target for normal deployment.

Local charms cannot inherit Charmhub release resources. OCI image resources
default to the ``upstream-source`` declared in the charm's metadata; supply
any other declared resource (or an image override) in ``resources`` as an OCI
image reference or existing file path. File resource paths and ``overrides`` are relative to the configuration
file, not the runner checkout. Consumer override files replace the corresponding
shipped per-charm YAML files; other shipped overrides are retained.

``endpoints`` and ``neighbors`` filter discovery. To replace discovery entirely:

.. code:: json

   {
     "scenarios": [
       {
         "endpoint": "database",
         "neighbor": "database-client",
         "neighbor_endpoint": "database",
         "pytest_args": ["-k", "test_remove_and_restore_integration"]
       }
     ]
   }

Global ``pytest_args`` apply before per-scenario arguments. Use these to select
tests, supply Juju bootstrap/model configuration, explicitly select a historical
revision, or configure cross-model neighbors using existing suite options.
The scheduler still includes required prerequisite transitions. Runner-owned
target identity, local artifact, cloud, and result paths are supplied last.
The normal target revision is always reset to ``default`` so an extra pytest
argument cannot replace the built artifact with a published revision.
An explicit empty selection list is an error; omit it to use defaults.

Upgrade/downgrade coverage
-------------------------

The local target is used for initial deployment, idempotent redeployment, and
returning from a historical store revision. Juju numbers local charm revisions
independently of Charmhub, so the suite identifies the local target by its
``local`` charm origin rather than by revision. Historical deployment switches the
bundle's target back to its Charmhub name. Downgrade explicitly switches the
running application to Charmhub; upgrade refreshes from the same local file.

Historical revision selection retains the suite's existing behavior: supply
``--target-downgrade-revision`` in ``pytest_args`` or configure Test Observer
history access. Without a usable published historical revision those tests
skip visibly. An unpublished charm can still run all other applicable tests.

Optional Test Observer reporting
-------------------------------

Set ``observer_execution_id`` to an existing positive Test Observer execution
ID (or pass ``--observer-execution-id`` / export ``TEST_OBSERVER_EXECUTION_ID``,
which override the file) and export ``TEST_OBSERVER_API`` and ``TEST_OBSERVER_TOKEN``. Credentials
are read only from the environment and are not written to the plan.

Reports use the same ``/v1/test-executions/{id}/test-results`` API and result
schema as the existing workflow. Scenario identity and the test's class are included
in the result name, because Test Observer identifies test cases by name rather
than category. The scenario also appears in the category. Results are posted
even when tests fail; an upload failure also fails the overall run and does
not suppress subsequent scenarios. Non-idempotent writes are not replayed
after HTTP error responses.

The caller owns execution creation and completion/lifecycle management.
When reporting is disabled, no upload occurs; credentials may still be used
by historical revision selection. The explicit execution-ID setting provides
a future policy boundary for making reporting mandatory.

Local CLI additions
-------------------

Direct ``scripts/run-tests.sh`` consumers can also pass
``--target-charm-path /absolute/path.charm`` and
``--target-resources '{"image": "registry.example/image:tag"}'`` alongside the
usual target name and suite options. These inputs use the same local metadata
and bundle support as the CI runner.
