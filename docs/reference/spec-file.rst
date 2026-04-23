Spec file reference
===================

The spec file is a YAML document that describes the models, applications, and
integrations you want bundle builder X to solve. The builder reads this file,
fetches charm metadata from Charmhub, and produces a bundle for each model.

Structure
---------

.. code-block:: yaml

   models:
     - name: my-model
       platform: kubernetes       # or "vm"
       arch: amd64                # default: amd64
       juju: 3/stable             # Juju snap channel
       controller: my-controller  # optional
       admin: admin               # optional
       applications:
         app-name:
           charm: charm-name
           channel: latest/stable # optional, overrides default channel
           revision: 42           # optional, pin to a specific revision
           base: ubuntu@22.04     # optional, pin to a specific base
       integrations:
         # Local integration (same model)
         - application: app-a
           endpoint: database
           remote_application: app-b
           remote_endpoint: db

         # Cross-model integration (in-spec)
         - application: app-a
           endpoint: certificates
           remote_application: vault
           remote_endpoint: vault-pki
           remote_model: pki-infra        # must match another model's name
           offer_name: vault-pki-offer    # optional, defaults to <remote_app>-offer

         # Cross-model integration (external)
         - application: app-a
           endpoint: certificates
           remote_application: vault
           remote_endpoint: vault-pki
           remote_model: external-pki
           url: prod-k8s:admin/external-pki.vault-offer

Fields
------

Model
~~~~~

.. list-table::
   :header-rows: 1
   :widths: 15 10 12 63

   * - Field
     - Required
     - Default
     - Description
   * - ``name``
     - yes
     - --
     - Unique name for the model. Used in output filenames and CMR references.
   * - ``platform``
     - no
     - ``kubernetes``
     - ``kubernetes`` or ``vm``.
   * - ``arch``
     - no
     - ``amd64``
     - Target architecture.
   * - ``juju``
     - no
     - ``3/stable``
     - Juju snap channel for version resolution.
   * - ``controller``
     - no
     - --
     - Controller name (metadata only, not used by the solver).
   * - ``admin``
     - no
     - ``admin``
     - Admin user (metadata only).
   * - ``applications``
     - yes
     - --
     - Map of application name to app spec.
   * - ``integrations``
     - no
     - ``[]``
     - List of explicit integrations.

Application
~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 15 10 12 63

   * - Field
     - Required
     - Default
     - Description
   * - ``charm``
     - yes
     - --
     - Charmhub charm name.
   * - ``channel``
     - no
     - --
     - Channel override (e.g. ``14/stable``).
   * - ``revision``
     - no
     - --
     - Pin to a specific revision.
   * - ``base``
     - no
     - --
     - Pin to a specific base (e.g. ``ubuntu@22.04``).

Integration
~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 20 10 25 45

   * - Field
     - Required
     - Default
     - Description
   * - ``application``
     - yes
     - --
     - Local application name (must exist in this model's ``applications``).
   * - ``endpoint``
     - yes
     - --
     - Endpoint name on the local application.
   * - ``remote_application``
     - yes
     - --
     - Remote application name.
   * - ``remote_endpoint``
     - yes
     - --
     - Endpoint name on the remote application.
   * - ``remote_model``
     - no
     - --
     - If set, this is a cross-model integration.
   * - ``offer_name``
     - no
     - ``<remote_application>-offer``
     - CMR offer name.
   * - ``url``
     - no
     - --
     - Required for external CMRs (model not in this spec).

Validation rules
----------------

The spec is validated on load. The following rules are enforced:

- At least one model must be defined.
- Every model must have a unique, non-empty name.
- Every model must have at least one application.
- Application names within a model must be unique (enforced by YAML map keys).
- Local integrations must reference applications defined in the same model.
- Cross-model integrations where ``remote_model`` matches another model in the spec
  must reference an application that exists in that remote model.
- Duplicate local integrations (same pair of app:endpoint) are rejected.
- Duplicate cross-model integrations (same local app, endpoint, remote model,
  remote app, remote endpoint) are rejected.
- A cross-model integration cannot target the current model.

Minimal example
---------------

.. code-block:: yaml

   models:
     - name: my-app
       platform: kubernetes
       applications:
         pg:
           charm: postgresql-k8s

This produces a single-model bundle containing PostgreSQL with all auto-discovered
dependencies resolved.
