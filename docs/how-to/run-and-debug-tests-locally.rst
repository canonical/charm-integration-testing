.. _build:

Run and debug tests locally
===========================
In this how-to we will go through how to locally execute and debug charm tests. Specifically, we will have a specific charm and endpoint under test, and test them against a neighbor charm and endpoint.

Note that the steps below are the same used for executing charm tests in the charm integration testing project.

Information you will need
-------------------------
For following along the how-to, you need some information (in the form of environment variables) specific to your use case. We will show some example values now, but along the how-to they are referenced as shell variables.

``charm_under_test``:
  The charmhub.io name of the primary charm under test. For example, ``grafana-k8s``.
``charm_endpoint``:
  Endpoint of the charm being tested. For example, ``grafana-dashboard``.
``neighbor``:
  The charmhub.io name of the charm to test the primary charm against. For example, ``loki-k8s``.
``neighbor_endpoint``:
  Endpoint for the neighbor charm being tested. For example, ``grafana-dashboard``.
``revision``:
  Revision number of the charm under test. For our example, ``143``.
``series``:
  Series to run the charm tests under. This is one of ``20.04``, ``22.04`` and ``24.04``.
``substrate``:
  Substrate to run the tests on. The only possible value at the moment is ``kubernetes``.

Code in this text also references the ``model_name`` environment variable, which is supposed to hold the juju model name.

Setting up Juju and k8s
------------------------------
Juju and k8s will be needed to run the tests on.

To install Juju, run:

.. code:: bash

  sudo snap install juju

To install k8s and kubectl, run:

.. code:: bash

   sudo snap install k8s --classic --channel latest/edge
   sudo snap install kubectl --classic --channel 1.30

Next, bootstrap k8s and configure kubectl:

.. code:: bash

   sudo k8s bootstrap --address=127.0.0.1
   sudo k8s config > ~/.kube/config

Setting up the k8s cloud
~~~~~~~~~~~~~~~~~~~~~~~~ 

It is also needed to setup the k8s cloud in juju. Do this with the following commands:

.. code:: bash

   juju add-k8s K8S_CLOUD_NAME --client
   juju bootstrap k8s K8S-CONTROLLER --bootstrap-constraints root-disk=5G
   juju add-model $model_name \
    --config "logging-config=DEBUG" \
    --config="update-status-hook-interval=30s"

Note that ``K8S_CLOUD_NAME`` and ``K8S-CONTROLLER`` are arbitrary string values. You may change them according to your needs.

Installing the repository dependencies
--------------------------------------

Next, install the Python dependencies to run the repository code:

.. code:: bash

   sudo apt-get update
   sudo apt-get install pipx
   pipx install poetry==2.0
   poetry install

Generate dynamic bundles
------------------------

To run the tests, we will generate a dynamic bundle that includes our charm under testing, the neighbor charm, and their respective endpoints. Do this with the following command:

.. code:: bash

   ./scripts/build-bundle.sh \
    --charms \
      "target::${charm_under_test}::${revision}::${series}" \
      "neighbor::${neighbor}::default::default" \
    --integrations "target:${charm_endpoint}::neighbor:${neighbor_endpoint}" \
    --substrate "${substrate}" \
    --charm-metadata-overrides ./static/charm-metadata-overrides/ \
    --output-file generated-bundle.yaml

Again, note that you may change the value of ``output-file`` according to your needs.

Next, you may verify the contents of the generated file ``generated-bundle.yaml``. They will look something like the following:

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

Deploy bundles
--------------
The first step is deploying the bundle. Do this with the following command:

.. code:: bash

   ./scripts/test-deploy.sh \
     --model "${model_name}" \
     --bundles "path/to/generated-bundle.yaml"

Execute tests
-------------
Run the following command to run the tests:

.. code:: bash

  ./scripts/test-integration.sh \
    --model "${model_name}" \
    --target-application "target" \
    --target-endpoint "${charm_endpoint}" \
    --neighbor-application "neighbor" \
    --neighbor-endpoint "${neighbor_endpoint}"

Teardown charm under test
-------------------------
Finally, execute the test teardown:

.. code:: bash

   ./scripts/test-teardown.sh \
     --model "${model_name}" \
     --applications "target"
