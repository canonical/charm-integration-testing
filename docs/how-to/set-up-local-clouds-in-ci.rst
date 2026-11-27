Set up local Juju clouds in CI
==============================

Product-team workflows can run this repository's Mise setup tasks directly to
prepare a local Kubernetes or LXD cloud for Juju. No GitHub Actions from this
repository are needed.

The tasks change the host. Concierge sets the ``iptables`` ``FORWARD`` policy to
``ACCEPT`` and may refresh existing snaps. Run them only on disposable runners or
on machines where these changes are acceptable.

In a workflow
-------------

See the `sample workflow
<https://github.com/canonical/charm-integration-testing/blob/main/.github/workflows/local-clouds-example.yaml>`_.
It:

1. Checks out this repository into ``cit/`` at a pinned commit.
2. Installs the Mise version from ``cit/.mise-version``.
3. Trusts only ``cit/``, and sets ``MISE_CEILING_PATHS`` to the workspace so
   that a ``mise.toml`` in your own repository is not merged in.
4. Installs Juju and the setup tools, then runs ``setup-k8s`` and ``setup-lxd``.

Use ``--output "$GITHUB_OUTPUT"`` to expose the following values as step
outputs:

- ``setup-k8s``: ``cloud-name``, ``kubeconfig-path`` and ``platform``.
- ``setup-lxd``: ``cloud-name`` and ``platform``.

Run ``mise run setup-k8s -- --help`` for the other options, such as
``--cloud-name``, ``--kubeconfig`` and ``--no-chaos-mesh``.

Using an existing Juju client
-----------------------------

Add ``--no-packages`` so that setup installs only the tools and leaves the
installed Juju client unchanged:

.. code:: bash

   mise run setup -- --group local-k8s --no-packages
   mise run setup-k8s

``setup-k8s`` uses whichever ``juju`` is on ``PATH``.

Using an existing cloud
-----------------------

If the cloud is already registered with Juju, skip the setup tasks. For a
Kubernetes cloud, point the test suite at its kubeconfig:

.. code:: bash

   export KUBECONFIG_my_k8s=/path/to/kubeconfig  # Juju cloud "my-k8s"

In a local shell
----------------

From a checkout, run ``mise trust`` once, then the same ``mise run``
commands. Re-running the tasks is safe; an already registered cloud is kept.
