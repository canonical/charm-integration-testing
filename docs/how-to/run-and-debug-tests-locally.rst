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

S3 storage
~~~~~~~~~~

Bundles containing an ``unconfigured`` ``s3-integrator`` require external object
storage. The post-deploy extension configures it; no MinIO charm is deployed.
An integrator with an explicit endpoint is left untouched.
The extension depends on the ``S3Provider`` interface; ``SwiftS3Provider`` and
``StaticS3Provider`` share the bucket lifecycle while supplying different
credential strategies.

For Swift, supply ``S3_OS_AUTH_URL``, ``S3_OS_PASSWORD``,
``S3_OS_PROJECT_DOMAIN_NAME``, ``S3_OS_PROJECT_NAME``, ``S3_OS_STORAGE_URL``,
``S3_OS_USERNAME`` and ``S3_OS_USER_DOMAIN_NAME`` in the environment. Also supply
``S3_CLEANUP_ACCESS_KEY`` and ``S3_CLEANUP_SECRET_KEY`` for a persistent cleanup
identity. The test-storage account must be able to create and revoke its own
Keystone EC2 credentials and create/delete buckets and objects.
The RADOS Gateway must enable ``rgw_s3_auth_use_keystone``. Its S3 endpoint is
the origin of the ``/swift/v1/AUTH_*`` storage URL. For deployments that do not
use the RADOS Gateway or have a nonstandard endpoint, set the optional
``S3_ENDPOINT`` explicitly rather than treating a native Swift URL as an S3 URL.

For a local S3-compatible server, supply ``S3_ENDPOINT``, ``S3_ACCESS_KEY`` and
``S3_SECRET_KEY`` instead, and unset ``S3_OS_STORAGE_URL``. In the development
sandbox, ``bash development-sandbox/bin/setup-s3.sh`` provisions this service.
Load ``$HOME/.local/share/charmqa-s3/s3.env`` in the test shell.
``S3_REGION`` defaults to ``us-east-1``.

Each integrator gets an isolated bucket and an initial ``charmqa/`` directory
marker for consumers such as Spark History Server. ``S3_BUCKET_NAMESPACE`` defaults to
``local``; CI uses each environment's ``s3-swift-secret`` value, so numbered
environments have separate cleanup scopes even when they share a substrate.
Session teardown tracks storage in the resource registry. Runs that create their
controller attempt to delete their owned buckets after controller teardown,
even if controller teardown fails. Runs reusing an existing controller preserve storage
because that controller may still be using it. Swift-generated bucket keys are
revoked before their buckets are deleted; local/static keys are never revoked.

Interrupted runs are swept by the scheduled environment cleanup using the
existing age threshold (240 minutes by default). Only strictly named harness
buckets in the matching namespace are eligible; log containers and externally
configured buckets are not touched. The four-hour threshold is longer than any
supported test run. Swift-backed cleanup uses the
pre-provisioned ``S3_CLEANUP_ACCESS_KEY`` and ``S3_CLEANUP_SECRET_KEY`` for S3
operations, so it does not create temporary cleanup keys. If a process is
terminated between creating a bucket EC2 key and tagging the bucket, the sweep
cannot identify that key, so manual revocation may be required. A failure to
create the bucket client triggers immediate revocation; if that fails, session
teardown retains the key identifier and retries revocation. Run the same cleanup
manually with:

.. code:: bash

   poetry run python scripts/cleanup-s3.py --age-minutes 240

Supported test environments opt into Swift storage with ``swift-storage:
true`` and select their own repository secret using ``s3-swift-secret`` in
``static/environments.yaml``. Create one repository secret per enabled
environment (22 with the current environment table) and store that
environment's credentials there, using a dedicated test-storage account
separate from the log-upload account. Each secret contains the seven required
Swift values and two cleanup values, plus optional ``S3_ENDPOINT`` when a
non-RADOS Gateway or nonstandard endpoint is needed. Use unquoted, single-line
``KEY=value`` format:

.. code:: text

   S3_OS_AUTH_URL=https://identity.example/v3
   S3_OS_USERNAME=test-storage
   S3_OS_PASSWORD=...
   S3_OS_USER_DOMAIN_NAME=users
   S3_OS_PROJECT_NAME=charm-qa
   S3_OS_PROJECT_DOMAIN_NAME=projects
   S3_OS_STORAGE_URL=https://storage.example/swift/v1/AUTH_project
   S3_CLEANUP_ACCESS_KEY=...
   S3_CLEANUP_SECRET_KEY=...
   S3_ENDPOINT=https://s3.example

The parser accepts comments and values containing ``=``; do not quote values or
put them on multiple lines. In cross-model runs, each integrator uses the
provider selected by its own environment, so both environment secrets must be
configured. The cleanup identity must be able to list buckets, read harness
bucket tags, and remove buckets, object versions, and incomplete uploads. The
test-storage account must also allow its generated bucket key to create buckets,
write bucket tags, and upload the marker object. Scheduled cleanup uses the
same environment secret and namespace as test provisioning. CI validates each
Swift-enabled environment's configuration before its test step starts and
reports missing variable names; non-Swift environments skip this check.

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

Moderate memory stress
~~~~~~~~~~~~~~~~~~~~~~

``test_live_memory_stress_moderate`` requires Kubernetes and Litmus or Chaos
Mesh with ``StressChaos``. It skips without memory stress support or passing
simple validators for every selected target and neighbor endpoint. If any selected
endpoint has no passing simple validator, the test skips before injecting stress.

Defaults are one worker, 128 MB and five minutes. Per-charm settings
``memory_moderate_pressure_workers``, ``memory_moderate_pressure_size_mb`` and
``memory_moderate_pressure_duration_seconds`` override them. Tune these values
for the workload; container memory limits are unchanged.

After confirmed injection, the test repeatedly checks experiment status,
workload health and simple validators. Error/blocked workloads, disconnected
agents, lost validation coverage, experiment errors and early completion fail.
Non-idle states are allowed. Checks are sampled, so brief failures may be missed.
Startup and the last validation round under stress share a two-minute allowance;
calls exceeding the stress execution budget fail when they return.

Cleanup runs on failure too; failed cleanup remains registered for retry.
Chaos Mesh memory cleanup waits for resource deletion. After successful
observation and cleanup, a separate fifteen-minute timeout applies to the
active/idle wait, followed by simple validation without additional restarts.
These post-cleanup checks are outside the stress execution budget.
Select ``-k test_live_memory_stress_moderate`` to run it.

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

Moderate CPU pressure
---------------------

``test_live_cpu_stress_moderate`` requires Kubernetes and Litmus or Chaos Mesh.
It stresses one target unit's workload container and skips if CPU stress support
or passing simple validators for any selected target or neighbor endpoint are
unavailable. Each selected endpoint must have at least one passing simple result
before injection.

Defaults are one worker and five minutes, overridden by
``cpu_moderate_pressure_workers`` and ``cpu_moderate_pressure_duration_seconds``.
Tune workers for the charm's CPU allocation; one worker is not universally
moderate pressure.

After initial active/idle and simple validation, the test confirms injection
and repeatedly checks stress status, workload health and simple validators.
Error/blocked workloads, disconnected agents, experiment errors and lost
validation coverage fail. Non-idle states are allowed during stress; sampled
checks may miss brief failures.

Cleanup runs on failure too. After successful observation and cleanup, target
and neighbor models must recover to active/idle within fifteen minutes and pass
simple validation without additional restarts. Cleanup failures fail the test.
Select ``-k test_live_cpu_stress_moderate`` to run it.

Total memory stress
-------------------

``test_live_memory_stress_total`` supports StatefulSet-backed Kubernetes workloads
and skips when no memory stress tool is available. It applies a temporary ``1Gi``
workload limit and stresses one unit using Litmus or Chaos Mesh.

Defaults are one worker, ``2048`` MB and ``600`` seconds of observation after
confirmed injection. Per-charm ``memory_exhaustion_workers``,
``memory_exhaustion_size_mb`` and ``memory_exhaustion_duration_seconds`` override
these values. The experiment receives an additional two minutes after settings
are resolved. Set ``memory_exhaustion_limit`` to a positive Kubernetes memory
quantity (for example, ``2Gi``) to override the ``1Gi`` default; stress size must
cover the configured limit.

A new out-of-memory termination in the target container after confirmed injection
ends observation early, even before restart. Neither termination nor a status
change is required. Experiment errors, early completion without this evidence
and cleanup failures fail the test.

After cleanup, all bundle models must reach active/idle within fifteen minutes
and pass available deep validators while the limit remains applied. Missing or
skipped validators provide no functional coverage. Original memory settings are
restored on success or failure, with a final idle check on success.

Total CPU stress
----------------

``test_live_cpu_stress_total`` requires Kubernetes and Litmus or the Chaos Mesh
``StressChaos`` CRD. It skips when no CPU stress tool is available. The test requires
one workload container and a StatefulSet using ``RollingUpdate`` with partition zero.

It temporarily limits the workload container to one CPU, waits for all bundle
units to become active/idle, then starts four stress workers. Stress is held for
``cpu_stress_duration`` (ten minutes by default); a non-active transition is not
required. Chaos Mesh waits up to one minute for the controller to confirm target
selection and injection before the hold period starts. A startup timeout fails
the test and still triggers cleanup. The requested experiment duration includes
two extra minutes to allow for startup and polling.
After cleanup, all bundle units, including a cross-model neighbor,
must become active/idle within ``cpu_recovery_timeout`` (fifteen minutes by
default). Available deep interface validators run for the target application and
its test neighbor, including validators implemented on the consumer side,
before restoring the CPU limit, so a restore rollout cannot mask failed recovery.
When no validator applies, logs report the missing validation coverage; only
status recovery is checked. Deployment validation supplies the initial baseline.
Original CPU requests and limits are restored even after failure or skip.
A StatefulSet patch configures the limit; it does not replace a missing chaos tool.

For PostgreSQL functional coverage, use ``postgresql-k8s:database`` against
``data-integrator:postgresql``. The ``postgresql_client`` validator runs on
data-integrator and checks database connectivity, a query, and a write/read cycle
at the deep level. The integration-test workflow includes this combination with
PostgreSQL revision 495, channel ``14/stable``, and base ``22.04``. For manual
workflow runs, use those same values and the branch containing your changes.
The existing ``certificates`` combination does not exercise this DB validator.
Confirm validation results for the neighbor in the live logs; an empty or skipped
validation result is not evidence of successful database recovery.

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
