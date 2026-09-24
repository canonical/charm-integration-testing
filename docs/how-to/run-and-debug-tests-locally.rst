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

Cross-model relations
---------------------

By default the target and neighbor applications are deployed into a single model. To
exercise a cross-model relation (CMR) instead, pass ``--neighbor-cloud``: the neighbor
application is deployed into a second model on that cloud. ``--neighbor-cloud`` is
required for any CMR variant; the same-platform case simply passes the same cloud as
``--target-cloud``.

``--same-controller`` is a modifier on top of ``--neighbor-cloud`` — it does not enable
CMR by itself and is rejected if passed without ``--neighbor-cloud``. There are two ways
to place the neighbor model:

- **Same controller** — pass ``--same-controller --neighbor-cloud <cloud>``. The
  neighbor model is created on the target controller (registering ``<cloud>`` there
  first if it differs from ``--target-cloud``), so only one controller is bootstrapped.
  This is the cheapest CMR variant and is what the same-platform/same-controller and
  multi-cloud-controller test matrix cells use.

  .. code:: bash

     ./scripts/run-tests.sh \
       --target-cloud "${CLOUD_NAME}" \
       --target-charm "mysql" \
       --target-endpoint "database" \
       --neighbor-charm "mysql-router" \
       --neighbor-endpoint "backend-database" \
       --same-controller \
       --neighbor-cloud "${CLOUD_NAME}" \
       --current-state "no_bundle" \
       --charm-overrides "./static/charm-overrides/" \
       --log-dir "./test-logs"

- **Cross controller** — pass ``--neighbor-cloud`` without ``--same-controller``. A
  second controller is bootstrapped on that cloud and the neighbor model is created
  there.

When a same-controller run names a different ``--neighbor-cloud`` than
``--target-cloud``, the suite registers that cloud on the target controller before
creating the neighbor model:

- For a Kubernetes ``--neighbor-cloud``, export its kubeconfig via
  ``KUBECONFIG_<cloud>`` (hyphens replaced with underscores, e.g.
  ``KUBECONFIG_local_k8s``); it is piped to ``juju add-k8s --controller`` so no
  client-only registration is needed.
- For any other ``--neighbor-cloud`` (e.g. OpenStack, LXD, manual), export the cloud
  definition and credentials YAML file paths via ``CLOUD_DEFINITION_<cloud>`` and
  ``CLOUD_CREDENTIALS_<cloud>``; they are passed to ``juju add-cloud --controller`` and
  ``juju add-credential --controller`` respectively.

In same-controller mode the neighbor model name is still generated separately, so the
two models remain distinct. ``--neighbor-controller`` must not be passed alongside
``--same-controller``.


