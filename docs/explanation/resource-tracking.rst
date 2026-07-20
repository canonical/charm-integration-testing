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
  overrides), an ``identity`` (the fields that make it the "same" resource across
  visits), and ``report_attributes()`` (descriptive fields recorded for humans).
  ``identity`` deliberately excludes volatile fields (for example a PVC's
  ``phase``) so that a transient status change is not mistaken for drift. The
  tracker and discrepancy calculator depend only on this structural interface,
  never on a concrete type, so a new resource kind becomes trackable by adding a
  snapshot type that implements it.

**Sources** (``resource_tracking.sources``)
  A ``KubernetesResourceSource`` maps one kind of raw Kubernetes object into
  snapshots for a single model. ``PvcSource`` is the reference implementation.
  These are Kubernetes-specific by design; other substrates supply their own
  source interface.

**Collectors** (``resource_tracking.collectors``)
  A ``ResourceCollector`` gathers snapshots from one *substrate*.
  ``KubernetesResourceCollector`` iterates the registered Juju model handles,
  runs each source, and returns ``CollectedResources`` per model. Collection is
  best-effort: a scope that cannot be queried (for example a non-Kubernetes
  model) is skipped rather than raising. The collector also drops snapshots
  whose owning application opts out of a resource kind (see `Per-charm tracking
  overrides`_). Adding an ``lxd`` or ``openstack`` collector requires no change
  to the tracker.

**Overrides** (bundle-builder ``OverridesClient``)
  A charm version opts out of tracking a resource kind under its ``overrides``
  block in ``static/charm-overrides/<charm>.yaml``. The
  ``resource_tracking_skips_by_application`` fixture reads each deployed
  application's charm and channel from the live model via
  ``juju_client.list_applications`` and looks up its ``resource_tracking.skip``
  set through the bundle-builder ``OverridesClient``, which is passed to the
  collector (see `Per-charm tracking overrides`_).


**Tracker** (``resource_tracking.tracker``)
  ``StateResourceTracker`` is a substrate-agnostic store. After each passing,
  state-marked test the ``track_state_resources`` fixture builds the collectors
  available for the current substrates and calls ``collect()``, which records one
  ``ResourceObservation`` per (state, model).

**Discrepancies** (``resource_tracking.discrepancy``)
  ``calculate_discrepancies()`` treats the first observation of each (state,
  model) as the baseline and diffs later visits against it by ``identity``. Each
  ``ModelResourceDiscrepancy`` publishes *structured* data via ``entries()`` --
  one ``DiscrepancyEntry`` per drifted resource -- rather than pre-formatted
  strings. This keeps execution-metadata formatting out of the domain objects.

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
*qualifiers*. Today two generic qualifiers exist:

``missing``
  A resource present in the baseline that is gone on re-entry.

``extra``
  A resource present on re-entry that was not in the baseline.

These follow directly from each snapshot's ``identity``: two snapshots are the
"same" resource only when their identities match, so a change to any identity
field reads as one resource going ``missing`` and another appearing ``extra``.

A resource type can define its own notion of drift by choosing what its
``identity`` includes and, if needed, by adding a qualifier. For example, a PVC
that is resized in place has the same ``namespace`` and ``name`` but a different
``requested_storage``; because ``requested_storage`` is part of ``PvcSnapshot``'s
identity it is currently reported as ``missing`` + ``extra``. To instead report
it as a single ``resized`` discrepancy, drop ``requested_storage`` from
``identity`` and add a ``resized`` branch to ``diff_snapshots()`` that matches on
name and compares the size. Because qualifiers flow untouched through
``ModelResourceDiscrepancy.entries()``, the recorder, and the metadata key
(``resource_discrepancy:<resource_type>:<qualifier>``), no other component changes -- a new
qualifier simply becomes a new selectable value.

Execution metadata format
-------------------------

Resource discrepancies are recorded under keys of the form::

   resource_discrepancy:<resource_type>:<qualifier>

where ``<qualifier>`` is a resource-specific drift kind -- generically
``missing`` or ``extra`` today, extensible per resource type (see
`Resource-specific discrepancy kinds`_). Only these generically-applicable
dimensions appear in the key so that downstream attachment rules can select on
them. Run-specific context (the scheduler state, the model name, and descriptive
resource attributes) is carried in the value instead of the key. For example, a
leaked PVC is recorded as::

   key:   resource_discrepancy:pvc:extra
   value: state=deployed model=<model> pvc=<name> requested_storage=1Gi storage_class=csi-cephfs

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
options -- so charms pulled in as dependencies are mapped the same way. As a
result a skip is scoped to the owning application only: a model can still track
the same resource kind for other applications, and a ``pvc`` skip on
``postgresql-k8s`` never masks drift from a co-deployed charm.

PersistentVolumeClaims: the reference resource
----------------------------------------------

PVCs are the reference tracked resource kind, and today the only one collected at
runtime. They serve as the worked example for every moving part above, so a new
resource kind can follow the same shape. Snapshot and source implementations for
several further kinds also exist (see `Additional resource kinds`_); PVCs remain
the only source registered in the collector's default tuple.

``PvcSnapshot`` (in ``resource_tracking.snapshot``) is a frozen, hashable
dataclass with:

``resource_type = "pvc"``
  A ``ClassVar`` label; it is the ``<resource_type>`` segment of the execution
  metadata key.

``identity`` = ``(namespace, name, storage_class, requested_storage)``
  The fields that make a claim the "same" resource across visits. ``phase`` is
  deliberately excluded so a claim merely transitioning between ``Pending`` and
  ``Bound`` is not mistaken for drift.

``report_attributes()`` = ``storage_class`` and ``requested_storage``
  The descriptive fields appended to the metadata value for humans.

``application``
  Populated from the ``app.kubernetes.io/name`` label so per-charm overrides can
  be scoped to the owning application; empty when the label is absent.

``PvcSource`` (in ``resource_tracking.sources``) is the matching source. It calls
``kubernetes_client.list_model_pvcs(model)`` and maps each raw
``V1PersistentVolumeClaim`` onto a ``PvcSnapshot``, defaulting optional fields to
empty strings and reading the owning application from the name label. It is
registered as a default source of ``KubernetesResourceCollector``.

Additional resource kinds
-------------------------

Beyond the PVC reference, snapshot types and sources are implemented for ten
further Kubernetes kinds. These follow the exact shape described in `Adding a new
resource kind`_: a frozen ``ResourceSnapshot`` in ``resource_tracking.snapshot``
whose ``identity`` excludes volatile fields, plus a matching
``KubernetesResourceSource`` in ``resource_tracking.sources`` that reads the
owning application from the ``app.kubernetes.io/name`` label.

.. note::

   These kinds provide the *record* logic only. They are **not** yet registered
   in ``KubernetesResourceCollector``'s default ``sources`` tuple, so at runtime
   only PVCs are collected. Wiring them in (step 3 of `Adding a new resource
   kind`_) is a deliberate follow-up. Some Kubernetes kinds are intentionally
   *not* implemented at all because their lifecycle would read as drift on every
   revisit; see `Deliberately untracked kinds`_.

The collector instantiates only ``CoreV1Api`` and ``AppsV1Api`` on
``KubernetesBackend``. Sources for the RBAC and networking kinds therefore build
their API group from the shared ``backend.api_client`` inside the source, keeping
the addition contained to the resource-tracking layer.

.. list-table::
   :header-rows: 1
   :widths: 18 22 30 30

   * - Resource type
     - Kubernetes API
     - Identity (diffed fields)
     - Notable exclusions / attributes
   * - ``statefulset``
     - ``apps/v1`` (``AppsV1Api``)
     - ``(namespace, name, replicas, image)``
     - Excludes rollout ``status``; how Juju runs sidecar charms.
   * - ``deployment``
     - ``apps/v1`` (``AppsV1Api``)
     - ``(namespace, name, replicas, image)``
     - Excludes rollout ``status``.
   * - ``service``
     - ``v1`` (``CoreV1Api``)
     - ``(namespace, name, service_type, ports)``
     - Excludes ``cluster_ip`` (reassigned on recreate).
   * - ``configmap``
     - ``v1`` (``CoreV1Api``)
     - ``(namespace, name)``
     - Records sorted ``data_keys`` only; values excluded.
   * - ``secret``
     - ``v1`` (``CoreV1Api``)
     - ``(namespace, name, secret_type)``
     - Records sorted ``data_keys`` only; values excluded so rotation is not drift.
   * - ``serviceaccount``
     - ``v1`` (``CoreV1Api``)
     - ``(namespace, name)``
     - Low-churn presence tracking.
   * - ``role``
     - ``rbac.authorization.k8s.io/v1`` (``RbacAuthorizationV1Api``)
     - ``(namespace, name)``
     - Summarises ``verbs:resources`` rules for the report.
   * - ``rolebinding``
     - ``rbac.authorization.k8s.io/v1`` (``RbacAuthorizationV1Api``)
     - ``(namespace, name, role_ref)``
     - Records ``kind/name`` role ref and sorted subjects.
   * - ``networkpolicy``
     - ``networking.k8s.io/v1`` (``NetworkingV1Api``)
     - ``(namespace, name)``
     - Records sorted ``policy_types``.
   * - ``ingress``
     - ``networking.k8s.io/v1`` (``NetworkingV1Api``)
     - ``(namespace, name)``
     - Records ``ingress_class`` and sorted ``hosts``.

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
   label (e.g. ``service``), choose an ``identity`` tuple that excludes volatile
   fields, return descriptive fields from ``report_attributes()``, and populate
   ``application`` (empty if the resource cannot be attributed to an application).

2. **Add a source** in ``resource_tracking.sources`` implementing
   ``KubernetesResourceSource``. Query the raw objects via ``kubernetes_client``
   and map each onto the new snapshot type for the given model. Raise/propagate
   ``ApiException`` on query failure; the collector treats it as best-effort.

3. **Register the source** with ``KubernetesResourceCollector``. Add it to the
   default ``sources`` tuple in its constructor so it runs for every model.

4. **(Optional) support opting out.** Nothing extra is needed: any charm version
   can already skip the new kind by adding its ``resource_type`` label under
   ``resource_tracking.skip`` in the matching ``overrides`` entry of
   ``static/charm-overrides/<charm>.yaml``, and the collector's per-application
   filtering applies automatically.

5. **(Optional) define resource-specific drift.** The default ``missing`` /
   ``extra`` qualifiers cover appearance and disappearance. If the resource needs
   a richer notion of drift (for example a ``resized`` volume), adjust its
   ``identity`` and add a qualifier in ``diff_snapshots()`` as described in
   `Resource-specific discrepancy kinds`_.

No change to ``StateResourceTracker``, ``calculate_discrepancies``,
``ModelResourceDiscrepancy``, ``test_resource_consistency_report``, or the
``record_failure_execution_metadata`` recorder is required: the new kind flows
through the same generic path and is published under
``resource_discrepancy:<new_resource_type>:<qualifier>``.


