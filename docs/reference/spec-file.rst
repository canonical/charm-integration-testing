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
       platform: kubernetes       # or "machine"
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
         local-app:
           charm: charm-name
           local_charm: ./build/charm-name.charm
           channel: 2/edge         # optional test context, not a Charmhub source
           revision: 42            # optional test context, not the local Juju revision
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
           offer_name: vault-pki-offer    # optional if url is omitted; required (and must match
                                           # the offer name embedded in url) if url is set

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
     - ``kubernetes`` or ``machine``.
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
     - Charm name. When ``local_charm`` is absent, this is resolved from Charmhub.
   * - ``channel``
     - no
     - --
     - Channel override (e.g. ``14/stable``). With ``local_charm``, this is the
       release context used to select channel-scoped overrides and evaluate DSL
       constraints; it does not select a Charmhub artifact.
   * - ``revision``
     - no
     - --
     - Pin to a specific Charmhub revision. With ``local_charm``, this is the
       intended test context for revision-scoped DSL constraints, not the revision
       Juju assigns to the local artifact.
   * - ``base``
     - no
     - --
     - Pin to a specific base (e.g. ``ubuntu@22.04``).
   * - ``local_charm``
     - no
     - --
     - Path to a local charm directory, such as one unpacked from a ``.charm``
       artifact. It is deployed as a local charm; any ``channel`` or ``revision``
       fields describe test context only and are not emitted as Charmhub source
       fields in the generated bundle.

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
   * - ``remote_controller``
     - no
     - --
     - Controller hosting the remote model. When set, the remote model is identified as ``remote_controller/remote_model`` in the domain.
   * - ``offer_name``
     - conditional
     - synthesized (see below)
     - CMR offer name. Required for in-spec CMRs that also set ``url`` (and must match the
       offer name embedded in ``url``); otherwise optional. If omitted for an in-spec CMR,
       Bundle Builder X reuses any explicit ``offer_name`` already declared by another active
       cross-model integration between the same provider/requirer charm pair; only if the pair
       has no declared name does it synthesize
       ``<providing_charm>-<providing_endpoint>-<interface>-offer``, with every underscore in
       those three components replaced by a hyphen. If omitted for an external CMR, it
       defaults to ``<remote_application>-offer``.
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
- An in-spec cross-model integration that sets ``url`` must also set ``offer_name``,
  and the two must agree: ``offer_name`` must equal the offer name embedded in ``url``
  (the segment after the last ``.``).
- Cross-model integrations that declare an explicit ``url`` -- whether in-spec or
  external -- must agree on ``offer_name`` for any two integrations that declare the
  exact same ``url`` (they consume the same offer), and conversely must not resolve to
  the same ``offer_name`` while declaring different ``url`` values (a single remote
  application may legitimately expose multiple distinct offers, but Bundle Builder X
  keys each requiring model's emitted SAAS entries by ``offer_name`` alone, so reusing a
  name across different urls would silently make one relation consume the wrong offer).
  An external CMR that omits ``offer_name`` defaults to ``<remote_application>-offer``
  for this comparison. In-spec CMRs that omit ``url`` are not covered by this
  spec-validation-time check -- this includes both an in-spec CMR that omits
  ``offer_name`` too (whose name is only resolved once Bundle Builder X selects a
  charm-pair anchor at build time) and one that declares ``offer_name`` explicitly
  without a ``url`` (whose declared name is retained, but not yet checked against
  other CMRs here since this check is keyed by ``url``) -- see below.
- After Bundle Builder X resolves every cross-model integration's offer_name and url
  (including in-spec CMRs that omitted both and rely on synthesis), the same
  bidirectional agreement rule is re-checked once more for the fully-resolved values in
  each requiring model. This catches cases the spec-validation-time check above cannot
  see -- for example, two different instances of the same provider charm, endpoint, and
  interface each synthesizing the same default offer_name while pointing at different
  provider models -- and rejects the build rather than silently emitting a bundle with
  one relation consuming the wrong offer.

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
