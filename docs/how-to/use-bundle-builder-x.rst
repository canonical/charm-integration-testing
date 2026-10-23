Use bundle builder X
====================

Bundle builder X generates Juju-compatible bundles from a YAML spec file.
It fetches charm metadata from Charmhub, applies local overrides, and uses
a Z3 constraint solver to produce minimal, valid bundles.

Prerequisites
-------------

The tool is installed as part of the monorepo Poetry environment::

    poetry install

This makes the ``bundle-builder-x`` CLI available inside the Poetry shell.

Writing a spec file
-------------------

Create a YAML file describing your models and the applications you want in each
one. See :doc:`/reference/spec-file` for the full format. A minimal example:

.. code-block:: yaml

   models:
     - name: my-app
       platform: kubernetes
       applications:
         db:
           charm: postgresql-k8s
         app:
           charm: kratos

The builder auto-discovers required integrations and pulls in additional charms
as needed.

Running the CLI
---------------

.. code-block:: bash

   poetry run bundle-builder-x \
       --spec my-spec.yaml \
       --overrides static/charm-overrides \
       --output-bundles output/ \
       --output-mermaid output/diagram.md \
       --log-level INFO

Arguments:

``--spec`` (required)
    Path to the spec YAML file.

``--overrides``
    Path to a directory of per-charm YAML files. Each file is named
    ``<charm-name>.yaml`` and may contain endpoint overrides, constraints,
    proxy declarations, config defaults, and resource values.

    Example override file with resources:

    .. code-block:: yaml

       overrides:
         - configs:
             namespace: [my-namespace]
             queue: [my-queue]
           resources:
             my-image: [ghcr.io/canonical/my-image:v1.0]
           constraints:
             - 'set(config[namespace]) and set(config[queue])'
             - 'set(resource[my-image])'

``--output-bundles``
    Directory to write per-model bundle YAML files. One file per model,
    named ``<model-name>.yaml``.

``--output-mermaid``
    File path for a Mermaid diagram of the solution. If the path ends in
    ``.md``, the diagram is wrapped in a Markdown fenced code block.

``--output-timeline``
    File path for a Mermaid Gantt chart of build timing. Useful for
    profiling slow solves.

``--log-level``
    One of ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``, ``CRITICAL``.
    Default is ``INFO``.

Reading the output
------------------

Each model produces a standard Juju bundle YAML:

.. code-block:: yaml

   bundle: kubernetes
   applications:
     postgresql-k8s:
       charm: postgresql-k8s
       channel: 14/stable
       revision: 495
       scale: 1
       base: ubuntu@22.04
     temporal-worker-k8s:
       charm: temporal-worker-k8s
       channel: 1/stable
       revision: 12
       scale: 1
       base: ubuntu@22.04
       options:
         namespace: my-namespace
         queue: my-queue
       resources:
         temporal-worker-image: ghcr.io/canonical/my-image:v1.0
   relations:
     - - kratos:pg-database
       - postgresql-k8s:database

The Mermaid output shows all models, their applications, and the integrations
between them, including cross-model relations.

Using the Python API
--------------------

You can also drive the builder programmatically:

.. code-block:: python

   from bundle_builder_x.spec import SpecFile
   from bundle_builder_x.charmhub import CharmhubClient
   from bundle_builder_x.overrides import OverridesClient
   from bundle_builder_x.bundle_builder import BundleBuilder

   spec = SpecFile.load("my-spec.yaml")
   overrides = OverridesClient(overrides="static/charm-overrides")
   charmhub = CharmhubClient(overrides_client=overrides)
   builder = BundleBuilder(charmhub_client=charmhub)

   solution = builder.build(spec)

   for bundle in solution.bundles:
       print(bundle.export())

Handling failures
~~~~~~~~~~~~~~~~~

If the solver cannot produce a valid bundle, ``BundleBuilder.build()`` raises
``UncompletableBundleError``. Its immutable ``diagnostics`` tuple contains every
non-redundant structured reason the bundle could not be completed. Diagnostic
types cover unfulfilled endpoints, feature mismatches, unresolved applications
and integrations, required-application release failures, and internal solver or
optimization failures:

.. code-block:: python

   from bundle_builder_x import ApplicationReleaseDiagnostic, UncompletableBundleError

   try:
       solution = builder.build(spec)
   except UncompletableBundleError as e:
       print(e)
       for diagnostic in e.diagnostics:
           print(f"  {diagnostic.description}")
           if isinstance(diagnostic, ApplicationReleaseDiagnostic):
               print(f"    release rejection: {diagnostic.error}")
