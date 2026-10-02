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

Chaos tools
~~~~~~~~~~~

Install Litmus or Chaos Mesh on the Kubernetes cluster for CPU and memory
pressure. Disk I/O latency requires Chaos Mesh. On PS6 staging and PS7, the
infrastructure repositories manage the shared ``litmus-core`` Helm release.
See ``docs/how-to/install_litmus_core.rst`` in sqa-ops for installation steps
and chart versions.

Set ``KUBECONFIG_<cloud_name>`` to the kubeconfig path for each Kubernetes
cloud, replacing hyphens in the cloud name with underscores. For example:

.. code:: bash

   export KUBECONFIG_local_k8s=/path/to/kubeconfig

The combined tool report runs once per session, when a test first requests
``require_chaos_tool`` or ``chaos_tool_for_model``. Before models exist,
the report checks each configured Kubernetes cloud once. Detection does not
run automatically for unrelated tests. Installation checks run again for
each client request. Litmus requires its three CRDs and a ready ``litmus``
Deployment in ``litmus-system``.

For Litmus experiments, the Kubernetes credentials must allow creation and
cleanup of experiment resources, ServiceAccounts, Roles and RoleBindings in
the test model namespace, including granting the runner's required permissions.

Tests select a supporting tool automatically for each experiment. When both
tools are available, Litmus handles CPU and memory pressure and Chaos Mesh
handles Disk I/O latency. Chaos Mesh also handles CPU and memory pressure
when Litmus is unavailable. Disk fill and Kubernetes network isolation do
not require either tool. Native disk fill supports machine and Kubernetes models.
Native CPU and memory commands are not used as fallback for external pressure.

Chaos Mesh support is checked per experiment. CPU and memory pressure require
the ``stresschaos.chaos-mesh.org`` CRD, while Disk I/O latency requires
``iochaos.chaos-mesh.org``. Either CRD enables its corresponding experiments.
The former ``require_chaos_mesh`` fixture is replaced by ``require_chaos_tool``.

Only unsupported operations permit fallback. Tests skip when no available
implementation supports the requested experiment. Each test uses separate
clients with cleanup at teardown, including after failure or skip.
API, execution and cleanup errors fail the test. Cleanup retains resources for manual
investigation when their recorded creation identifier cannot be verified.

Litmus uses pinned Docker Hub images and waits for confirmed stress injection.
Cleanup allows graceful termination and requires reversion evidence for observed
injections before deleting results and permissions; otherwise, it reports an
error and retains them for investigation.

Shared resources remain managed by the infrastructure repositories. Use
approved disposable workloads for experiments; the shared operator and
privileged helpers do not provide isolation between tenants.

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

Total memory stress
-------------------

``test_live_memory_stress_total`` requires Kubernetes and Litmus or Chaos Mesh
with the ``StressChaos`` resource; otherwise it skips. It temporarily applies a
``1Gi`` workload memory limit and stresses one unit. Defaults are one worker,
``2048`` MB and up to ``600`` seconds of observation after confirmed injection.

Per-charm settings can override ``memory_exhaustion_workers``,
``memory_exhaustion_size_mb`` and ``memory_exhaustion_duration_seconds``.
Override the ``memory_limit`` fixture separately; the requested stress size must
be at least the limit.

A new out-of-memory termination in the target container, timestamped after
injection is confirmed, ends observation early. A status change or restart is
not required. Experiment errors,
Experiment completion without this evidence before the observation period ends,
and cleanup failures fail the test.

After cleanup, all bundle models must reach active/idle within fifteen minutes
and pass available deep validators while the memory limit remains in place.
Skipped or missing validators provide no functional coverage. Original memory
settings are restored on success or failure, with a final idle check on success.

Network isolation recovery
--------------------------

``test_live_network_isolation`` requires Kubernetes and applies an ingress-only
``NetworkPolicy`` to all Pods of the target application. It leaves egress unrestricted
and retains the policy for ten minutes after the API accepts it. This interval
does not prove when the network plugin started enforcing the policy.

All bundle models must be active/idle before isolation. Remaining active/idle
during isolation is valid. After policy removal, all bundle models must return
to active/idle within fifteen minutes, without a test-driven restart. Deep
validators then run for every application in the target and neighbor models,
including consumer-side validators. Missing or skipped validators provide no
functional coverage; the status check alone only verifies Juju state recovery.
Policy creation, cleanup, recovery and validation errors fail the test.

The temporary connection probes have been removed. This test does not independently
measure packet blocking or continuously verify agent connectivity during the
observation interval. Network enforcement must be supported by the cluster.

Disk fill recovery
------------------

``test_live_disk_fill`` uses the shared native disk fill client on Kubernetes
and machine models. It allocates 98 percent of the available space reported by
``df`` in the execution working directory, using a unique file per test. This
is not a guarantee of 98 percent total file system usage or of filling the
application's data volume. Per-charm resource settings are not consumed yet.

All bundle models must be active/idle before allocation. The file remains for
ten minutes; no unhealthy status transition is required. Cleanup removes the
file, then all bundle models must recover to active/idle within fifteen minutes
without a test-driven restart. Available deep validators run on all applications
in both target and neighbor models. Missing validators leave functional coverage
unverified. Allocation, file checks, cleanup, recovery and validation errors fail
the test.
