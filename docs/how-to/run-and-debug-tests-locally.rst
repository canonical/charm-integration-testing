.. _build:

Run and debug tests locally
===========================
In this how-to, we will go through how to locally execute and debug charm tests. Specifically, we will test one endpoint from one charm, which is the primary charm being tested and is called the target charm, with one endpoint from one other charm, which we call the neighbor charm.

Note that the steps below are the same used for executing charm tests in the charm integration testing project.

Information you will need
-------------------------
This guide will reference variables that need to contain values specific to your use case. For convenience, they are treated as environment variables, so that you can simply copy and paste the commands as they are written (without any edits or substitutions), as long as you set the environment variables yourself beforehand. However, you may also substitute values directly into the commands if you prefer not to set environment variables.

``TARGET_CHARM``:
  The name of the primary charm under test on `Charmhub <https://charmhub.io/>`_. For example, ``grafana-k8s``.
``TARGET_ENDPOINT``:
  Endpoint of the charm being tested. For example, ``grafana-dashboard``.
``NEIGHBOR_CHARM``:
  The name on `Charmhub <https://charmhub.io/>`_ of the charm to test the primary charm against. For example, ``loki-k8s``.
``NEIGHBOR_ENDPOINT``:
  Endpoint for the neighbor charm being tested. For example, ``grafana-dashboard``.
``REVISION``:
  Revision number of the charm under test. For our example, ``143``.
``SERIES``:
  Series to run the charm tests under. This is one of ``20.04``, ``22.04`` and ``24.04``.
``SUBSTRATE``:
  Substrate to run the tests on. The only possible value at the moment is ``kubernetes``.
``K8S_CLOUD_NAME``:
  The name to use for the Kubernetes cloud that Juju will use for its controller and model. For example, ``k8s-cloud``.
``K8S_CONTROLLER_NAME``:
  The name to use for the Juju controller that is bootstrapped to the Kubernetes cloud and which will control the Juju model used in the testing. For example, ``k8s-controller``.
``MODEL_NAME``:
  The name to use for the Juju model that is created and used in the testing. For example, ``charm-testing``.
``OUTPUT_FILE``:
  A filename to use for for the charm bundle output produced by the ``build-bundle.sh`` script. For example, ``generated-bundle.yaml``.
``JUJU_MODEL_CONFIG_FILE``:
  Path to a JSON file containing Juju model configuration values passed at model creation time. For example, ``./static/juju-model-config.json``.

Optional environment variables
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The following environment variables are optional and only needed when testing specific charms that require them:

``MINIO_CLIENT_FILE``:
  Path to the MinIO client configuration file. Will be downloaded automatically by the test scripts if not provided. Used when deploying the `minio-k8s` charm with `s3-integrator`.
``UV_FILE``:
  Path to a pre-downloaded ``uv`` binary. Will be downloaded automatically if not provided. Used when injecting validators onto units to create the Python virtualenv.
``UBUNTU_PRO_TOKEN``:
  Ubuntu Pro token for configuring the livepatch server. Required when testing the `canonical-livepatch-server-k8s`` charms.
``VALIDATORS_PATH``:
  Path to a local directory containing the validator packages (each a sub-directory with its own ``pyproject.toml``). When set, the test framework will ``scp`` the directory to each unit under test, install the validators in a virtualenv, and run them after each validation phase. If not provided, validator injection is skipped. For example, ``${PWD}/validators``.

Set up Juju and k8s
-------------------
Juju and k8s will be needed to run the tests.

To install Juju, run:

.. code:: bash

  sudo snap install juju

To install ``k8s`` and ``kubectl``, run:

.. code:: bash

   sudo snap install k8s --classic --channel latest/edge
   sudo snap install kubectl --classic --channel 1.30

Next, bootstrap ``k8s`` and configure ``kubectl``:

.. code:: bash

   sudo k8s bootstrap --address=127.0.0.1
   sudo k8s config > ~/.kube/config

Set up the k8s cloud
~~~~~~~~~~~~~~~~~~~~

It is also needed to setup the k8s cloud in juju. Do this with the following commands:

.. code:: bash

   juju add-k8s ${K8S_CLOUD_NAME} --client
   juju bootstrap ${K8S_CLOUD_NAME} ${K8S_CONTROLLER_NAME} --bootstrap-constraints root-disk=5G
   juju add-model ${MODEL_NAME} \
     --config "logging-config=DEBUG" \
     --config="update-status-hook-interval=2m"

Install the repository dependencies
-----------------------------------

Next, install the Python dependencies to run the repository code:

.. code:: bash

   sudo apt-get update
   sudo apt-get install pipx
   pipx install poetry==2.0
   poetry install

Generate dynamic bundles
------------------------

To run the tests, we will generate a dynamic bundle that includes our test charm, the neighbor charm, and their respective endpoints. Do this with the following command:

.. code:: bash

   ./scripts/build-bundle.sh \
    --charms \
      "target::${TARGET_CHARM}::${CHANNEL}::${REVISION}::${SERIES}" \
      "neighbor::${NEIGHBOR_CHARM}::default::default::default" \
    --integrations "target:${TARGET_ENDPOINT}::neighbor:${NEIGHBOR_ENDPOINT}" \
    --substrate "${SUBSTRATE}" \
    --charm-metadata-overrides ./static/charm-metadata-overrides/ \
    --charm-platform-overrides ./static/charm-platform-overrides/ \
    --charm-listing-overrides ./static/charm-listing-overrides.yaml \
    --charm-test-configs ./static/charm-test-configs/ \
    --output-file "${OUTPUT_FILE}"

The contents of the output file will look something like the following:

.. code:: yaml

  applications:
    neighbor:
      base: ubuntu@20.04
      channel: 1/stable
      charm: loki-k8s
      revision: 194
      scale: 1
      trust: true
    target:
      base: ubuntu@20.04
      channel: 1/stable
      charm: grafana-k8s
      revision: 143
      scale: 1
      trust: true
  bundle: kubernetes
  relations:
  - - neighbor:grafana-dashboard
    - target:grafana-dashboard

Run tests
---------

Run all tests with the following command. The state-driven scheduler
automatically handles deploy, integration tests, and teardown in the
correct order:

.. code:: bash

   ./scripts/run-tests.sh \
     --juju-cloud "${K8S_CLOUD_NAME}" \
     --juju-controller "${K8S_CONTROLLER_NAME}" \
     --model "${MODEL_NAME}" \
     --current-state "no_controller" \
     --bundle "${OUTPUT_FILE}" \
     --juju-model-config "${JUJU_MODEL_CONFIG_FILE}" \
     --target-application "target" \
     --target-endpoint "${TARGET_ENDPOINT}" \
     --neighbor-application "neighbor" \
     --neighbor-endpoint "${NEIGHBOR_ENDPOINT}"

To increase test verbosity while debugging, pass ``--log-cli-level`` to stream
logs live to your terminal.

The ``--current-state`` option tells the scheduler the current state of
the environment so it knows which setup steps (if any) still need to run:

- ``no_controller`` — no Juju controller exists yet (default)
- ``no_model`` — a controller exists, but the model does not
- ``empty_model`` — controller and model are ready but nothing is deployed
- ``deployed`` — the charm bundle is already deployed; skip straight to
  integration tests and teardown
- ``neighbor_only`` — only the neighbor application remains after teardown
- ``deployed_with_old_revision`` — the charm bundle is deployed with the target on an
  old revision
- ``deployed_with_upgraded_controller`` — the charm bundle is deployed with an upgraded Juju controller

Use a non-default ``--current-state`` when resuming a partial run or
iterating locally against an already-deployed model to avoid re-running
expensive setup transitions.

The ``--juju-model-config`` file is optional. If omitted, tests create the model
without extra configuration; if provided, pass a JSON object of string keys and values
matching Juju model configuration options.

Testmon and re-running tests
----------------------------

The CI pipeline uses `pytest-testmon <https://testmon.org/>`_ to skip tests
whose code (and transitive dependencies) has not changed since the last
successful run.  The cached database (``.testmondata``) is stored per branch
in the GitHub Actions cache.

During **pull-request checks**, testmon is enabled (``--testmon``) so only
tests affected by the pull request changes are executed.  During the
**merge-main-to-production** workflow, ``full_test_suite`` is set to
``true``, which disables testmon and forces every test to run.

The core/leaf model
~~~~~~~~~~~~~~~~~~~~

The test suite is a *DAG* against live infrastructure:
running a downstream test requires its upstream tests to have run **in the
same session** to build the live state (a bootstrapped controller, a created
model, a deployed bundle).  testmon treats tests as independent and freely
deselectable, which conflicts with that model.

The scheduler reconciles this by splitting the suite into two classes:

- **Core (spine) tests** — tagged ``@pytest.mark.core``.  These form the
  mandatory base chain that must run every time to establish state (e.g.
  ``test_build_bundle``, ``test_bootstrap_controller``, ``test_create_model``,
  ``test_scale_in_and_scale_out``, ``test_teardown``).  They are **never**
  deselected by testmon.
- **Leaf tests** — everything else.  testmon owns selection of these: if
  their code is unchanged, testmon deselects them and they don't run.  This
  is where testmon's speedup applies.

Interaction with the state scheduler
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The scheduler uses a ``hookwrapper`` around ``pytest_collection_modifyitems``
to enforce the core/leaf split:

1. **Before yield**: core items are pulled out of ``items`` so testmon only
   sees leaf tests during its deselection pass.
2. **During yield**: testmon (and any other ``modifyitems`` hooks) run,
   deselecting unchanged leaves.
3. **After yield**: the core items are restored in front of the
   testmon-selected leaves and the scheduler orders the combined set into a
   valid state chain, injecting bridging transitions as needed.

Additionally, ``pytest_ignore_collect`` (``tryfirst=True``) forces collection
of every ``.py`` file inside the test-suite package, even when testmon would
otherwise skip files entirely.  This ensures the scheduler always has the
complete state graph available for Dijkstra pathfinding.

In practice this means:

- Core tests **always run** — testmon never deselects them.
- Testmon decides **which leaf tests to run** (deselection).
- The scheduler decides **what order** everything runs in and **which
  bridging transitions** must be injected to reach required states.
- ``--ignore`` / ``--ignore-glob`` have no effect on files inside the test
  suite directory because the scheduler forces collection.  Use ``-k`` to
  select or exclude specific tests instead.

Injected bridge labelling
~~~~~~~~~~~~~~~~~~~~~~~~~~

When the scheduler injects a bridging transition test that the user did not
explicitly select, it labels the item via ``user_properties`` and the
``injected`` pytest marker — **without** mutating the item's ``nodeid``.
This is critical because testmon keys its stored fingerprints on the nodeid;
a mutated nodeid would never match testmon's database, causing the bridge to
be re-selected on every run.  The ``[injected]`` label is rendered through
``pytest_report_teststatus`` instead.

Forcing a full re-run locally
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

To bypass testmon and run every test locally, either delete the cache file
or pass ``--forcerun``:

.. code:: bash

   # Option 1: delete the cache
   rm -f .testmondata

   # Option 2: force-run everything
   ./scripts/run-tests.sh \
     ... \
     --forcerun
