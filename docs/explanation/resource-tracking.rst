Resource Tracking and Consistency Reporting
===========================================

This document explains how the test suite tracks substrate resources across
scheduler states and how detected inconsistencies are published as execution
metadata.

Motivation
----------

The scheduler drives the environment through a series of canonical
:class:`~test_suite.scheduler.states.State` values, revisiting the same state
many times over a run. Entering the *same* state is expected to correspond to
the *same* set of underlying resources every time. Resource tracking detects
drift -- resources that leak (appear ``extra``) or that go ``missing`` -- when a
state is re-entered.

Components
----------

Recording, collection, and comparison are deliberately separated so each part
stays small and substrate-agnostic:

**Snapshots** (``resource_tracking.snapshot``)
  A ``ResourceSnapshot`` is an immutable description of one resource. Each
  snapshot exposes a ``resource_type`` (e.g. ``pvc``), a ``name``, an
  ``application`` (the owning Juju application, used to apply per-charm tracking
  overrides), an ``identity`` (its stable ``(namespace, name)``, the fields that
  make it the "same" resource across visits), ``report_attributes()``
  (descriptive fields recorded for humans), and ``inconsistency_checks`` (the
  resource-specific in-place changes -- e.g. a PVC ``resized`` -- it knows how to
  report). ``identity`` deliberately excludes both volatile fields (for example a
  PVC's ``phase``) and mutable spec fields (compared through
  ``inconsistency_checks`` instead), so a transient status change or an in-place
  edit is not mistaken for one resource vanishing and another appearing. The
  tracker and discrepancy calculator depend only on this structural interface,
  never on a concrete type, so a new resource kind becomes trackable by adding a
  snapshot type that implements it.

**Sources** (``resource_tracking.sources``)
  A ``KubernetesResourceSource`` maps one kind of raw Kubernetes object into
  snapshots for a single model. The canonical set collected when tracking runs
  live is ``DEFAULT_KUBERNETES_SOURCES`` -- PVCs, StatefulSets, Deployments,
  Services, ConfigMaps, Secrets, ServiceAccounts, Roles, RoleBindings,
  NetworkPolicies, and Ingresses -- and is the single source of truth the test
  suite passes to the collector, so a new kind becomes tracked by adding its
  source to that tuple. ``SecretSource`` additionally drops Secrets whose name is
  server-generated rather than declared (``metadata.generateName`` is set, or a
  ``kubernetes.io/service-account-token`` Secret with its volatile
  ``<sa>-token-XXXXX`` name), because such a Secret gets a different name every
  time it is recreated and so cannot be diffed by ``(namespace, name)`` identity;
  tracking it would report a spurious ``missing`` + ``extra`` on every
  recreation. ``PvcSource`` faces the same volatile-name problem but keeps the
  claim trackable by *normalizing* the Juju volume id embedded in the PVC name to
  a ``<volume-id>`` placeholder instead of dropping the resource. These sources
  are Kubernetes-specific by design; other substrates supply their own source
  interface.

**Collectors** (``resource_tracking.collectors``)
  A ``ResourceCollector`` gathers snapshots from one *substrate*.
  ``KubernetesResourceCollector`` iterates the registered Juju model handles,
  runs each source, and returns ``CollectedResources`` per model. Collection is
  best-effort: a scope that cannot be queried (for example a non-Kubernetes
  model) is skipped rather than raising. Every observed snapshot is recorded
  uniformly; per-charm opt-outs are *not* applied here but once at diff time (see
  `Per-charm tracking overrides`_). Adding an ``lxd`` or ``openstack`` collector
  requires no change to the tracker.

**Overrides** (bundle-builder ``OverridesClient``)
  A charm version opts out of tracking a resource kind under its ``overrides``
  block in ``static/charm-overrides/<charm>.yaml``. The
  ``resource_tracking_skips_by_application`` fixture reads each deployed
  application's charm and channel from the live model via
  ``juju_client.list_applications`` and looks up its ``resource_tracking.skip``
  set through the bundle-builder ``OverridesClient``. Each application is
  resolved once and cached for the session, and the resolved map is applied at
  diff time by ``calculate_discrepancies`` (see `Per-charm tracking overrides`_).


**Tracker** (``resource_tracking.tracker``)
  ``StateResourceTracker`` is a substrate-agnostic store. After each passing,
  state-marked test the ``track_state_resources`` fixture builds the collectors
  available for the current substrates and calls ``collect()``, which records one
  ``ResourceObservation`` per (state, model).

**Discrepancies** (``resource_tracking.discrepancy``)
  ``calculate_discrepancies()`` treats the first observation of each (state,
  model) as the baseline and diffs later visits against it by ``identity``. It
  first excludes any ``(application, resource_type)`` pairs the resolved skip map
  opts out of, applying that single map uniformly to every observation so a
  skipped kind cannot read as drift. Each ``ModelResourceDiscrepancy`` publishes
  *structured* data via ``entries()`` -- one ``DiscrepancyEntry`` per drifted
  resource -- rather than pre-formatted strings. This keeps execution-metadata
  formatting out of the domain objects.

Failure and reporting flow
--------------------------

The end-of-suite test ``test_resource_consistency_report`` carries no state
marker, so the scheduler appends it after every state-marked test and runs it
exactly once. It computes discrepancies and, if any exist, raises
``ResourceDiscrepancyError`` carrying the structured discrepancies.

The ``record_failure_execution_metadata`` fixture in ``conftest.py`` recognises
``ResourceDiscrepancyError`` (alongside the other domain exceptions it handles)
and normalises the discrepancies into execution metadata. The test itself does
no formatting; the domain object carries structure; the recorder owns the
metadata schema.

Resource-specific discrepancy kinds
-----------------------------------

*What* counts as a discrepancy can differ per resource type. Comparison happens
in one place -- ``diff_snapshots()`` -- which groups drifted resources under
*qualifiers*. Two generic, presence-based qualifiers apply to every resource:

``missing``
  A resource present in the baseline that is gone on re-entry.

``extra``
  A resource present on re-entry that was not in the baseline.

These follow from each snapshot's ``identity``, which is deliberately just the
resource's stable ``(namespace, name)``: two snapshots are the "same" resource
only when their identities match, so a resource that disappears is ``missing``
and one that newly appears is ``extra``.

Beyond presence, a resource can drift *in place* -- keeping its identity while a
spec field changes. Each snapshot type declares the set of such changes it cares
about as a list of ``InconsistencyCheck`` values in its ``inconsistency_checks``
class attribute. A check names a *report attribute* and the *qualifier* to emit
when that attribute differs between the baseline and the re-entry snapshot of the
same resource. For example ``PvcSnapshot`` declares::

   inconsistency_checks = (
       InconsistencyCheck(qualifier="resized", attribute="requested_storage"),
       InconsistencyCheck(qualifier="storage_class_changed", attribute="storage_class"),
   )

so a PVC that is resized in place is reported once as ``resized`` (carrying its
before/after) rather than as a ``missing`` + ``extra`` pair. The full set of
per-resource qualifiers is:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Resource type
     - Modification qualifiers
   * - ``pvc``
     - ``resized``, ``storage_class_changed``
   * - ``statefulset`` / ``deployment``
     - ``scaled``, ``image_changed``
   * - ``service``
     - ``type_changed``, ``ports_changed``
   * - ``configmap``
     - ``keys_changed``
   * - ``secret``
     - ``type_changed``, ``keys_changed``
   * - ``role``
     - ``rules_changed``
   * - ``rolebinding``
     - ``role_ref_changed``, ``subjects_changed``
   * - ``networkpolicy``
     - ``policy_types_changed``
   * - ``ingress``
     - ``class_changed``, ``hosts_changed``
   * - ``serviceaccount``
     - none (only ``missing`` / ``extra``)

A resource type gains a new notion of drift simply by adding an
``InconsistencyCheck`` (and, if the attribute is not already reported, a
``report_attributes()`` entry to compare on). Because qualifiers flow untouched
through ``ModelResourceDiscrepancy.entries()``, the recorder, and the metadata
key (``resource_discrepancy:<resource_type>:<qualifier>``), no other component
changes -- a new qualifier simply becomes a new selectable value.

Adding a resource kind or check is intentionally cheap because the mutating
tests are idempotent round-trips: scaling in then out, breaking then restoring an
integration, deleting a pod and awaiting its replacement, or redeploying the same
bundle all return the environment to the *same* state. Snapshots are recorded
only after such a test passes and the model reaches idle, so re-entering a state
must reproduce the baseline -- which means any qualifier that fires is genuine
drift, never an expected mid-test change.

Execution metadata format
-------------------------

Resource discrepancies are recorded under keys of the form::

   resource_discrepancy:<resource_type>:<qualifier>

where ``<qualifier>`` is either a presence kind (``missing`` / ``extra``) or a
resource-specific modification kind (see `Resource-specific discrepancy kinds`_).
Only these generically-applicable dimensions appear in the key so that downstream
attachment rules can select on them. Run-specific context (the scheduler state,
the model name, and descriptive resource attributes) is carried in the value
instead of the key. For a presence qualifier the value carries the resource's
attributes whole; a leaked PVC is recorded as::

   key:   resource_discrepancy:pvc:extra
   value: state=deployed model=<model> pvc=<name> requested_storage=1Gi storage_class=csi-cephfs

For a modification qualifier only the attributes that actually changed are
emitted, as ``old->new``, so the drift is visible at a glance. A PVC that grew
in place is recorded as::

   key:   resource_discrepancy:pvc:resized
   value: state=deployed model=<model> pvc=<name> requested_storage=1Gi->2Gi

Because the model name is a per-run identifier it is intentionally kept out of
the key; keys stay stable across runs while the value provides debugging detail.


Per-charm tracking overrides
----------------------------

Some charms legitimately leave resources behind that the tracker would otherwise
flag as drift. ``postgresql-k8s``, for example, retains its ``pgdata``
PersistentVolumeClaims across application removal and scale-in events, so a
leftover PVC is expected rather than a defect.

A charm version opts out of tracking a resource *kind* by adding a
``resource_tracking.skip`` list to the matching ``overrides`` entry in its
``static/charm-overrides/<charm>.yaml`` file::

   overrides:
     - criteria:
         - track: '14'
       resource_tracking:
         skip:
           - pvc
       provides:
         ...

Because the section lives inside a per-version ``overrides`` entry, different
tracks or risks of the same charm can declare different skips. Resolution reuses
the bundle-builder machinery: the deployed application's charm and channel are
read from the live model and the matching entry's skip set is looked up through
``OverridesClient.get_charm_resource_tracking_skips()``, so the resource tracker
and the solver share one source of truth for per-version overrides.

Skips are declared per *charm version*, but resources are attributed to an
*application* on the cluster (a PVC carries an ``app.kubernetes.io/name`` label
equal to the owning Juju application). The
``resource_tracking_skips_by_application`` fixture therefore reads the deployed
applications and their charms from the live models via
``juju_client.list_applications`` -- rather than from hard-coded target/neighbor
options -- so charms pulled in as dependencies are mapped the same way. Each
application is resolved once and cached for the session, so a transient
``list_applications`` failure on a later visit (surfaced at ``WARNING``) does not
lose skip coverage already established. The resolved map is applied once, at diff
time, inside ``calculate_discrepancies``: a skipped ``(application,
resource_type)`` pair is excluded uniformly from the baseline and every
re-entry, so a per-visit resolution difference cannot make a skipped kind read as
``missing``/``extra`` drift. As a result a skip is scoped to the owning
application only: a model can still track the same resource kind for other
applications, and a ``pvc`` skip on ``postgresql-k8s`` never masks drift from a
co-deployed charm.

PersistentVolumeClaims: the reference resource
----------------------------------------------

PVCs are the reference tracked resource kind and the worked example for every
moving part above, so a new resource kind can follow the same shape. They are
collected live alongside the other kinds listed under `Additional resource
kinds`_; every kind is registered through ``DEFAULT_KUBERNETES_SOURCES`` (see
below).

``PvcSnapshot`` (in ``resource_tracking.snapshot``) is a frozen, hashable
dataclass with:

``resource_type = "pvc"``
  A ``ClassVar`` label; it is the ``<resource_type>`` segment of the execution
  metadata key.

``identity`` = ``(namespace, name)``
  The fields that make a claim the "same" resource across visits. The spec fields
  (``storage_class``, ``requested_storage``) are excluded from identity and
  compared through ``inconsistency_checks`` instead, so an in-place change reads
  as a ``resized`` / ``storage_class_changed`` qualifier rather than a ``missing``
  / ``extra`` pair; the volatile ``phase`` is excluded entirely so a claim merely
  transitioning between ``Pending`` and ``Bound`` is not mistaken for drift.

``report_attributes()`` = ``storage_class`` and ``requested_storage``
  The descriptive fields appended to the metadata value for humans.

``application``
  Populated from the ``app.kubernetes.io/name`` label so per-charm overrides can
  be scoped to the owning application; empty when the label is absent.

``PvcSource`` (in ``resource_tracking.sources``) is the matching source. It calls
``kubernetes_client.list_model_pvcs(model)`` and maps each raw
``V1PersistentVolumeClaim`` onto a ``PvcSnapshot``, defaulting optional fields to
empty strings and reading the owning application from the name label. It is one
of the sources in ``DEFAULT_KUBERNETES_SOURCES``, the tuple the test suite passes
to ``KubernetesResourceCollector``.

Juju provisions charm storage as PVCs whose name embeds a volatile 8-hex volume
id -- the first block of the storage UUID -- between the storage label and the
StatefulSet suffix, for example ``postgresql-k8s-pgdata-b0ba0188-postgresql-k8s-0``.
That id is minted afresh whenever the claim is (re)provisioned, so two visits to
the *same* logical storage would otherwise carry different names and be diffed as
a spurious ``missing`` / ``extra`` pair. ``PvcSource`` therefore normalizes the
volume id to a ``<volume-id>`` placeholder before building the snapshot, restoring
a stable ``(namespace, name)`` identity. Unlike ``SecretSource``, which *drops*
volatile-named Secrets outright (their content is not worth tracking), a PVC
still carries meaningful drift (``resized``, ``storage_class_changed``), so its
name is normalized rather than dropped.


Additional resource kinds
-------------------------

Beyond the PVC reference, snapshot types and sources are implemented for ten
further Kubernetes kinds. These follow the exact shape described in `Adding a new
resource kind`_: a frozen ``ResourceSnapshot`` in ``resource_tracking.snapshot``
whose ``identity`` is ``(namespace, name)`` (spec drift is expressed through
``inconsistency_checks``), plus a matching ``KubernetesResourceSource`` in
``resource_tracking.sources`` that reads the owning application from the
``app.kubernetes.io/name`` label.

.. note::

   All ten kinds are registered in ``DEFAULT_KUBERNETES_SOURCES`` and collected
   live alongside PVCs. Some Kubernetes kinds are intentionally *not* implemented
   at all because their lifecycle would read as drift on every revisit; see
   `Deliberately untracked kinds`_.

The collector instantiates only ``CoreV1Api`` and ``AppsV1Api`` on
``KubernetesBackend``. Sources for the RBAC and networking kinds therefore build
their API group from the shared ``backend.api_client`` inside the source, keeping
the addition contained to the resource-tracking layer.

Every kind uses a ``(namespace, name)`` identity; spec drift is surfaced through
the per-type ``inconsistency_checks`` listed below rather than folded into
identity.

.. list-table::
   :header-rows: 1
   :widths: 18 22 30 30

   * - Resource type
     - Kubernetes API
     - Modification checks (qualifier / attribute)
     - Notable exclusions / attributes
   * - ``statefulset``
     - ``apps/v1`` (``AppsV1Api``)
     - ``scaled`` (replicas), ``image_changed`` (image)
     - Excludes rollout ``status``; how Juju runs sidecar charms.
   * - ``deployment``
     - ``apps/v1`` (``AppsV1Api``)
     - ``scaled`` (replicas), ``image_changed`` (image)
     - Excludes rollout ``status``.
   * - ``service``
     - ``v1`` (``CoreV1Api``)
     - ``type_changed`` (type), ``ports_changed`` (ports)
     - Excludes ``cluster_ip`` (reassigned on recreate) and the Juju placeholder port; ``ports_changed`` ignores empty transitions so a service seen before its workload opens ports is not drift.
   * - ``configmap``
     - ``v1`` (``CoreV1Api``)
     - ``keys_changed`` (data_keys)
     - Records sorted ``data_keys`` only; values excluded.
   * - ``secret``
     - ``v1`` (``CoreV1Api``)
     - ``type_changed`` (type), ``keys_changed`` (data_keys)
     - Volatile-named secrets skipped (``generateName``, service-account tokens, and unlabelled Juju secret-content revisions); sorted ``data_keys`` only, values excluded so rotation is not drift.
   * - ``serviceaccount``
     - ``v1`` (``CoreV1Api``)
     - None (presence only)
     - Low-churn presence tracking; volatile Juju secret-consumer accounts skipped.
   * - ``role``
     - ``rbac.authorization.k8s.io/v1`` (``RbacAuthorizationV1Api``)
     - ``rules_changed`` (rules)
     - Summarises ``verbs:resources`` rules for the report; volatile Juju secret-consumer roles skipped.
   * - ``rolebinding``
     - ``rbac.authorization.k8s.io/v1`` (``RbacAuthorizationV1Api``)
     - ``role_ref_changed`` (role_ref), ``subjects_changed`` (subjects)
     - Records ``kind/name`` role ref and sorted subjects; volatile Juju secret-consumer bindings skipped.
   * - ``networkpolicy``
     - ``networking.k8s.io/v1`` (``NetworkingV1Api``)
     - ``policy_types_changed`` (policy_types)
     - Records sorted ``policy_types``.
   * - ``ingress``
     - ``networking.k8s.io/v1`` (``NetworkingV1Api``)
     - ``class_changed`` (ingress_class), ``hosts_changed`` (hosts)
     - Records ``ingress_class`` and sorted ``hosts``.

Volatile instances within tracked kinds
---------------------------------------

Some individual objects are skipped even though their *kind* is tracked, because
Juju names them with a volatile component that changes every time they are
recreated. Diffing them by ``(namespace, name)`` identity would report spurious
``missing`` / ``extra`` drift on every revisit, so the sources filter them out:

* **Juju secret-consumer RBAC triad.** For each secret consumer Juju creates a
  ``Role``, ``RoleBinding`` and ``ServiceAccount`` all named
  ``juju-secret-consumer-<uuid>``; the embedded UUID is regenerated on recreate.
* **Juju secret-content revisions.** Juju stores secret payloads as ``secret``
  objects named ``<xid>-<revision>`` (a per-secret xid plus a rotating revision).
  These are skipped only when unlabelled, so a charm-declared secret is never
  dropped by a coincidental name match.

Deliberately untracked kinds
----------------------------

Some Kubernetes kinds are intentionally left untracked. Their identity is
inherently volatile or their lifecycle is ephemeral, so the baseline-and-revisit
diff (`Resource-specific discrepancy kinds`_) would flag spurious ``missing`` /
``extra`` drift on every revisit to a state even when nothing is wrong.

.. list-table::
   :header-rows: 1
   :widths: 18 22 60

   * - Resource type
     - Kubernetes API
     - Reason for exclusion
   * - ``pod``
     - ``v1`` (``CoreV1Api``)
     - Names and UIDs churn on every reschedule for Deployment/ReplicaSet-owned
       pods, and ``phase`` is transient. Only StatefulSet pods have stable
       ordinal names, and those are already covered by tracking the owning
       ``statefulset``.
   * - ``replicaset``
     - ``apps/v1`` (``AppsV1Api``)
     - Deployment-owned ReplicaSets carry a hash suffix in their name that
       changes on every template update, and superseded revisions are retained,
       so the set churns constantly. The owning ``deployment`` captures the
       meaningful state instead.
   * - ``job``
     - ``batch/v1`` (``BatchV1Api``)
     - Jobs are ephemeral: they are created to run to completion and are often
       garbage-collected afterwards, so a Job present on one visit to a state is
       legitimately absent on the next.

Adding a new resource kind
--------------------------

Because the tracker, discrepancy calculator, report, and recorder all depend
only on the ``ResourceSnapshot`` structural interface, a new Kubernetes resource
kind is added without touching any of them. Following the PVC example:

1. **Add a snapshot type** in ``resource_tracking.snapshot``. Make it a frozen
   dataclass implementing ``ResourceSnapshot``: set ``resource_type`` to a short
   label (e.g. ``service``), use a ``(namespace, name)`` ``identity``, return
   descriptive fields from ``report_attributes()``, list any spec-drift
   ``inconsistency_checks`` (see step 5), and populate ``application`` (empty if
   the resource cannot be attributed to an application).

2. **Add a source** in ``resource_tracking.sources`` implementing
   ``KubernetesResourceSource``. Query the raw objects via ``kubernetes_client``
   and map each onto the new snapshot type for the given model. Raise/propagate
   ``ApiException`` on query failure; the collector treats it as best-effort.

3. **Register the source** by adding it to ``DEFAULT_KUBERNETES_SOURCES`` in
   ``resource_tracking.sources`` -- the single tuple the test suite passes to
   ``KubernetesResourceCollector`` -- so it runs for every model.

4. **(Optional) support opting out.** Nothing extra is needed: any charm version
   can already skip the new kind by adding its ``resource_type`` label under
   ``resource_tracking.skip`` in the matching ``overrides`` entry of
   ``static/charm-overrides/<charm>.yaml``, and the diff-time per-application
   filtering in ``calculate_discrepancies`` applies automatically.

5. **(Optional) define resource-specific drift.** The default ``missing`` /
   ``extra`` qualifiers cover appearance and disappearance. If the resource needs
   a richer notion of drift (for example a ``resized`` volume), add an
   ``InconsistencyCheck`` -- pairing a qualifier with the ``report_attributes()``
   key to compare -- to the snapshot's ``inconsistency_checks``; ``diff_snapshots()``
   runs them generically, so no change to it is required. See
   `Resource-specific discrepancy kinds`_.

No change to ``StateResourceTracker``, ``calculate_discrepancies``,
``ModelResourceDiscrepancy``, ``test_resource_consistency_report``, or the
``record_failure_execution_metadata`` recorder is required: the new kind flows
through the same generic path and is published under
``resource_discrepancy:<new_resource_type>:<qualifier>``.


