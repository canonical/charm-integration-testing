# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from types import SimpleNamespace

import pytest
from juju.resource_registry import JujuControllerHandle, JujuModelHandle
from kubernetes.client import ApiException  # type: ignore[import-untyped]
from resource_tracking import (
    CollectedResources,
    DiscrepancyEntry,
    KubernetesResourceCollector,
    ModelResourceDiscrepancy,
    QualifiedSnapshot,
    ResourceDiscrepancyError,
    StateResourceTracker,
    calculate_discrepancies,
    diff_snapshots,
)
from resource_tracking.snapshot import PvcSnapshot, ServiceSnapshot
from resource_tracking.sources import PvcSource
from resource_tracking.tracker import ResourceObservation
from test_suite.scheduler.states import State
from test_suite.test_resource_consistency_report import (
    test_resource_consistency_report as run_resource_consistency_report,
)


def _pvc(name: str, storage: str = "1Gi", application: str = "") -> PvcSnapshot:
    return PvcSnapshot(
        name=name,
        namespace="test-model",
        storage_class="csi-cephfs",
        requested_storage=storage,
        phase="Bound",
        application=application,
    )


def _qs(snapshot: PvcSnapshot, baseline: PvcSnapshot | None = None) -> QualifiedSnapshot:
    return QualifiedSnapshot(snapshot=snapshot, baseline=baseline)


def _service(name: str, ports: str, service_type: str = "ClusterIP") -> ServiceSnapshot:
    return ServiceSnapshot(
        name=name,
        namespace="test-model",
        service_type=service_type,
        cluster_ip="10.1.2.3",
        ports=ports,
    )


def _raw_pvc(
    name: str,
    storage_class: str | None = "csi-cephfs",
    requested_storage: str | None = "1Gi",
    phase: str | None = "Bound",
    labels: dict[str, str] | None = None,
) -> SimpleNamespace:
    """Build a raw V1PersistentVolumeClaim-like object for PvcSource mapping."""
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels=labels),
        spec=SimpleNamespace(
            storage_class_name=storage_class,
            resources=SimpleNamespace(requests={"storage": requested_storage} if requested_storage else {}),
        ),
        status=SimpleNamespace(phase=phase),
    )


class _FakeKubernetesClient:
    """Minimal stand-in exposing only the method PvcSource depends on."""

    def __init__(self, pvcs: list[SimpleNamespace]) -> None:
        self._pvcs = pvcs
        self.requested_model: str | None = None

    def namespace_exists(self, model: str) -> bool:
        return True

    def list_model_pvcs(self, model: str) -> list[SimpleNamespace]:
        self.requested_model = model
        return self._pvcs


class _RaisingKubernetesClient:
    """Stand-in whose PVC listing always raises, to exercise error handling.

    The namespace exists, so the collector runs the source; the failure models a
    transient per-kind list error rather than a missing namespace.
    """

    def __init__(self, error: Exception) -> None:
        self._error = error

    def namespace_exists(self, model: str) -> bool:
        return True

    def list_model_pvcs(self, model: str) -> list[SimpleNamespace]:
        raise self._error


class _ProbeRaisingKubernetesClient:
    """Stand-in whose namespace probe always raises, to exercise probe error handling.

    The failure models a transient API error on the namespace read rather than a
    missing namespace (a 404), which the client would surface as absence instead.
    """

    def __init__(self, error: Exception) -> None:
        self._error = error

    def namespace_exists(self, model: str) -> bool:
        raise self._error


class _NoNamespaceKubernetesClient:
    """Stand-in whose model namespace does not exist (a non-Kubernetes model)."""

    def namespace_exists(self, model: str) -> bool:
        return False


class _FakeResourceRegistry:
    """Stand-in returning a fixed set of registered handles."""

    def __init__(self, handles: list[object]) -> None:
        self._handles = handles

    def registered_handles(self) -> list[object]:
        return self._handles


class _FakeCollector:
    """Stand-in ResourceCollector returning fixed collected resources."""

    def __init__(self, collected: list[CollectedResources]) -> None:
        self._collected = collected

    def collect(self, logger: logging.Logger) -> list[CollectedResources]:
        return self._collected


_LOGGER = logging.getLogger("test-resource-tracking")


class TestPvcSource:
    def test_maps_raw_pvc_fields_to_snapshot(self) -> None:
        # GIVEN a client returning a single raw PVC
        client = _FakeKubernetesClient([_raw_pvc("data-postgresql-0")])

        # WHEN the source collects snapshots for a model
        snapshots = PvcSource().collect(client, "test-model")  # type: ignore[arg-type]

        # THEN each field is mapped onto a PvcSnapshot scoped to the model namespace
        assert snapshots == [
            PvcSnapshot(
                name="data-postgresql-0",
                namespace="test-model",
                storage_class="csi-cephfs",
                requested_storage="1Gi",
                phase="Bound",
            )
        ]
        assert client.requested_model == "test-model"

    def test_missing_storage_class_and_request_default_to_empty(self) -> None:
        # GIVEN a raw PVC without a storage class or storage request
        client = _FakeKubernetesClient([_raw_pvc("data-0", storage_class=None, requested_storage=None, phase=None)])

        # WHEN the source collects snapshots
        snapshots = PvcSource().collect(client, "test-model")  # type: ignore[arg-type]

        # THEN the optional fields default to empty strings
        assert snapshots == [
            PvcSnapshot(
                name="data-0",
                namespace="test-model",
                storage_class="",
                requested_storage="",
                phase="",
            )
        ]

    def test_owning_application_is_read_from_the_name_label(self) -> None:
        # GIVEN a raw PVC labelled with its owning Juju application
        client = _FakeKubernetesClient([_raw_pvc("pgdata-target-0", labels={"app.kubernetes.io/name": "target"})])

        # WHEN the source collects snapshots
        snapshots = PvcSource().collect(client, "test-model")  # type: ignore[arg-type]

        # THEN the snapshot records the owning application
        assert snapshots[0].application == "target"

    def test_missing_name_label_leaves_application_empty(self) -> None:
        # GIVEN a raw PVC without the owning-application label
        client = _FakeKubernetesClient([_raw_pvc("data-0", labels={"other": "label"})])

        # WHEN the source collects snapshots
        snapshots = PvcSource().collect(client, "test-model")  # type: ignore[arg-type]

        # THEN the application defaults to empty
        assert snapshots[0].application == ""

    def test_volatile_volume_id_in_name_is_normalized(self) -> None:
        # GIVEN a Juju charm-storage PVC whose name embeds a volatile 8-hex volume id
        client = _FakeKubernetesClient([_raw_pvc("postgresql-k8s-pgdata-b0ba0188-postgresql-k8s-0")])

        # WHEN the source collects snapshots
        snapshots = PvcSource().collect(client, "test-model")  # type: ignore[arg-type]

        # THEN the volume id is replaced with a placeholder, stabilising the name
        assert snapshots[0].name == "postgresql-k8s-pgdata-<volume-id>-postgresql-k8s-0"

    def test_different_volume_ids_normalize_to_the_same_identity(self) -> None:
        # GIVEN the same logical claim seen across two visits with different volume ids
        first = PvcSource().collect(
            _FakeKubernetesClient([_raw_pvc("postgresql-k8s-pgdata-b0ba0188-postgresql-k8s-0")]),  # type: ignore[arg-type]
            "test-model",
        )[0]
        second = PvcSource().collect(
            _FakeKubernetesClient([_raw_pvc("postgresql-k8s-pgdata-81a553d9-postgresql-k8s-0")]),  # type: ignore[arg-type]
            "test-model",
        )[0]

        # THEN the normalized identity matches, so it is not diffed as missing/extra
        assert first.identity == second.identity

    def test_name_without_a_volume_id_is_left_unchanged(self) -> None:
        # GIVEN a PVC name that carries no 8-hex volume id
        client = _FakeKubernetesClient([_raw_pvc("data-postgresql-0")])

        # WHEN the source collects snapshots
        snapshots = PvcSource().collect(client, "test-model")  # type: ignore[arg-type]

        # THEN the name passes through untouched
        assert snapshots[0].name == "data-postgresql-0"

    def test_only_the_volume_id_segment_is_normalized_when_name_has_two_hex_segments(self) -> None:
        # GIVEN a name carrying an earlier hex-like segment as well as the real volume id
        first = PvcSource().collect(
            _FakeKubernetesClient([_raw_pvc("deadbeef-pgdata-b0ba0188-postgresql-k8s-0")]),  # type: ignore[arg-type]
            "test-model",
        )[0]
        second = PvcSource().collect(
            _FakeKubernetesClient([_raw_pvc("deadbeef-pgdata-81a553d9-postgresql-k8s-0")]),  # type: ignore[arg-type]
            "test-model",
        )[0]

        # THEN only the trailing volume id is replaced; the earlier hex segment is preserved
        assert first.name == "deadbeef-pgdata-<volume-id>-postgresql-k8s-0"
        # AND the same logical claim across visits still normalizes to one identity
        assert first.identity == second.identity

    def test_distinct_claims_sharing_a_volume_id_position_do_not_collapse(self) -> None:
        # GIVEN two claims that differ only in an earlier hex-like segment
        first = PvcSource().collect(
            _FakeKubernetesClient([_raw_pvc("deadbeef-pgdata-b0ba0188-postgresql-k8s-0")]),  # type: ignore[arg-type]
            "test-model",
        )[0]
        second = PvcSource().collect(
            _FakeKubernetesClient([_raw_pvc("cafed00d-pgdata-b0ba0188-postgresql-k8s-0")]),  # type: ignore[arg-type]
            "test-model",
        )[0]

        # THEN the preserved earlier segment keeps them as distinct identities
        assert first.identity != second.identity

    def test_none_spec_and_status_do_not_abort_collection(self) -> None:
        # GIVEN a raw PVC whose optional spec/status subtrees are entirely None
        raw = SimpleNamespace(
            metadata=SimpleNamespace(name="data-0", labels=None),
            spec=None,
            status=None,
        )
        client = _FakeKubernetesClient([raw])

        # WHEN the source collects snapshots
        snapshots = PvcSource().collect(client, "test-model")  # type: ignore[arg-type]

        # THEN the snapshot is produced with empty defaults instead of raising
        assert snapshots == [
            PvcSnapshot(
                name="data-0",
                namespace="test-model",
                storage_class="",
                requested_storage="",
                phase="",
            )
        ]

    def test_none_resources_default_requested_storage_to_empty(self) -> None:
        # GIVEN a raw PVC whose spec has no resources block
        raw = SimpleNamespace(
            metadata=SimpleNamespace(name="data-0", labels=None),
            spec=SimpleNamespace(storage_class_name="csi-cephfs", resources=None),
            status=SimpleNamespace(phase="Bound"),
        )
        client = _FakeKubernetesClient([raw])

        # WHEN the source collects snapshots
        snapshots = PvcSource().collect(client, "test-model")  # type: ignore[arg-type]

        # THEN requested_storage defaults to empty while other fields map through
        assert snapshots == [
            PvcSnapshot(
                name="data-0",
                namespace="test-model",
                storage_class="csi-cephfs",
                requested_storage="",
                phase="Bound",
            )
        ]


class TestStateResourceTracker:
    def test_record_accumulates_observations_in_order(self) -> None:
        # GIVEN a tracker
        tracker = StateResourceTracker()

        # WHEN two observations are recorded
        tracker.record(State.DEPLOYED, "model-a", frozenset({_pvc("data-0")}))
        tracker.record(State.DEPLOYED, "model-b", frozenset())

        # THEN they are stored in order
        assert tracker.observations() == (
            ResourceObservation(State.DEPLOYED, "model-a", frozenset({_pvc("data-0")})),
            ResourceObservation(State.DEPLOYED, "model-b", frozenset()),
        )

    def test_collect_records_each_collectors_resources(self) -> None:
        # GIVEN two collectors each reporting resources for a model
        collector_a = _FakeCollector([CollectedResources("model-a", frozenset({_pvc("data-0")}))])
        collector_b = _FakeCollector([CollectedResources("model-b", frozenset())])
        tracker = StateResourceTracker()

        # WHEN the tracker collects for a state
        tracker.collect(State.DEPLOYED, [collector_a, collector_b], _LOGGER)

        # THEN every collector's resources are recorded under that state
        assert tracker.observations() == (
            ResourceObservation(State.DEPLOYED, "model-a", frozenset({_pvc("data-0")})),
            ResourceObservation(State.DEPLOYED, "model-b", frozenset()),
        )


class TestKubernetesResourceCollector:
    def test_collects_snapshots_for_each_registered_model(self) -> None:
        # GIVEN a registry with a model handle (and a non-model handle to skip)
        registry = _FakeResourceRegistry(
            [
                JujuControllerHandle(controller="test-controller"),
                JujuModelHandle(controller="test-controller", model="test-model"),
            ]
        )
        client = _FakeKubernetesClient([_raw_pvc("data-0")])

        # WHEN the collector gathers resources
        collected = KubernetesResourceCollector(client, registry).collect(_LOGGER)  # type: ignore[arg-type]

        # THEN only the model handle yields a snapshot set
        assert collected == [CollectedResources("test-model", frozenset({_pvc("data-0")}))]

    def test_source_errors_drop_the_whole_model_observation(self) -> None:
        # GIVEN a model whose namespace exists but whose PVC listing transiently fails
        registry = _FakeResourceRegistry([JujuModelHandle(controller="test-controller", model="test-model")])
        client = _RaisingKubernetesClient(ApiException(status=500))

        # WHEN the collector gathers resources
        collected = KubernetesResourceCollector(client, registry).collect(_LOGGER)  # type: ignore[arg-type]

        # THEN no observation is recorded, so a partial snapshot is never diffed as drift
        assert collected == []

    def test_namespace_probe_errors_drop_the_model_observation(self) -> None:
        # GIVEN a model whose namespace probe transiently fails
        registry = _FakeResourceRegistry([JujuModelHandle(controller="test-controller", model="test-model")])
        client = _ProbeRaisingKubernetesClient(ApiException(status=500))

        # WHEN the collector gathers resources
        collected = KubernetesResourceCollector(client, registry).collect(_LOGGER)  # type: ignore[arg-type]

        # THEN the probe failure is treated as best-effort skip rather than raising
        assert collected == []

    def test_models_without_a_namespace_are_skipped(self) -> None:
        # GIVEN a model whose namespace does not exist (e.g. a machine model)
        registry = _FakeResourceRegistry([JujuModelHandle(controller="test-controller", model="machine-model")])
        client = _NoNamespaceKubernetesClient()

        # WHEN the collector gathers resources
        collected = KubernetesResourceCollector(client, registry).collect(_LOGGER)  # type: ignore[arg-type]

        # THEN no observation is recorded for the non-Kubernetes model
        assert collected == []

    def test_collects_every_snapshot_uniformly(self) -> None:
        # GIVEN two PVCs owned by different applications
        registry = _FakeResourceRegistry([JujuModelHandle(controller="test-controller", model="test-model")])
        client = _FakeKubernetesClient(
            [
                _raw_pvc("pgdata-target-0", labels={"app.kubernetes.io/name": "target"}),
                _raw_pvc("data-neighbor-0", labels={"app.kubernetes.io/name": "neighbor"}),
            ]
        )

        # WHEN the collector gathers resources
        collected = KubernetesResourceCollector(client, registry).collect(_LOGGER)  # type: ignore[arg-type]

        # THEN every snapshot is recorded; per-charm skips are applied at diff time
        assert collected == [
            CollectedResources(
                "test-model",
                frozenset(
                    {
                        _pvc("pgdata-target-0", application="target"),
                        _pvc("data-neighbor-0", application="neighbor"),
                    }
                ),
            )
        ]


class TestDiffSnapshots:
    def test_missing_and_extra_are_grouped_by_qualifier(self) -> None:
        # GIVEN a baseline PVC replaced by a differently-named one
        baseline = frozenset({_pvc("data-0")})
        current = frozenset({_pvc("data-1")})

        # WHEN the two snapshot sets are diffed
        qualifiers = diff_snapshots(baseline, current)

        # THEN the dropped PVC is 'missing' and the new one is 'extra'
        assert qualifiers == {"missing": (_qs(_pvc("data-0")),), "extra": (_qs(_pvc("data-1")),)}

    def test_identical_sets_yield_no_qualifiers(self) -> None:
        # GIVEN two identical snapshot sets
        snapshots = frozenset({_pvc("data-0")})

        # WHEN they are diffed
        qualifiers = diff_snapshots(snapshots, snapshots)

        # THEN no qualifier is present at all (empty qualifiers are omitted)
        assert qualifiers == {}

    def test_qualifiers_are_sorted_by_identity(self) -> None:
        # GIVEN a baseline that gains two extra PVCs out of identity order
        baseline: frozenset[PvcSnapshot] = frozenset()
        current = frozenset({_pvc("data-1"), _pvc("data-0")})

        # WHEN the sets are diffed
        qualifiers = diff_snapshots(baseline, current)

        # THEN the extra PVCs are returned sorted by identity
        assert qualifiers["extra"] == (_qs(_pvc("data-0")), _qs(_pvc("data-1")))

    def test_in_place_change_is_a_modification_not_missing_extra(self) -> None:
        # GIVEN a PVC that keeps its name but is resized in place
        baseline = frozenset({_pvc("data-0", storage="1Gi")})
        current = frozenset({_pvc("data-0", storage="2Gi")})

        # WHEN the sets are diffed
        qualifiers = diff_snapshots(baseline, current)

        # THEN it reads as a single 'resized' qualifier carrying its baseline,
        # never as a missing/extra pair
        assert qualifiers == {"resized": (_qs(_pvc("data-0", storage="2Gi"), baseline=_pvc("data-0", storage="1Gi")),)}

    def test_ports_appearing_from_empty_is_not_flagged(self) -> None:
        # GIVEN a Service first seen without ports (placeholder filtered out) then
        # observed with its real ports opened
        baseline = frozenset({_service("target", ports="")})
        current = frozenset({_service("target", ports="5432/TCP,8008/TCP")})

        # WHEN the sets are diffed
        qualifiers = diff_snapshots(baseline, current)

        # THEN the empty->real transition is not reported as ports_changed
        assert qualifiers == {}

    def test_ports_change_between_real_sets_is_flagged(self) -> None:
        # GIVEN a Service whose established port set changes
        baseline = frozenset({_service("target", ports="5432/TCP,8008/TCP")})
        current = frozenset({_service("target", ports="5432/TCP")})

        # WHEN the sets are diffed
        qualifiers = diff_snapshots(baseline, current)

        # THEN the real change is reported as ports_changed
        assert qualifiers == {
            "ports_changed": (
                QualifiedSnapshot(
                    snapshot=_service("target", ports="5432/TCP"),
                    baseline=_service("target", ports="5432/TCP,8008/TCP"),
                ),
            )
        }


class TestCalculateDiscrepancies:
    def test_first_visit_records_baseline_without_discrepancy(self) -> None:
        # GIVEN a single observation for a state
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))

        # WHEN discrepancies are calculated
        # THEN the first visit establishes a baseline and reports nothing
        assert calculate_discrepancies(tracker.observations()) == []

    def test_identical_revisit_reports_no_discrepancy(self) -> None:
        # GIVEN a state visited twice with the same resources
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))

        # WHEN discrepancies are calculated THEN nothing is reported
        assert calculate_discrepancies(tracker.observations()) == []

    def test_missing_resource_on_revisit_is_reported(self) -> None:
        # GIVEN a baseline with two PVCs revisited with one missing
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0"), _pvc("data-1")}))
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))

        # WHEN discrepancies are calculated
        discrepancies = calculate_discrepancies(tracker.observations())

        # THEN the missing PVC is reported and nothing is extra
        assert discrepancies == [
            ModelResourceDiscrepancy(
                state=State.DEPLOYED,
                model="test-model",
                qualified={"missing": (_qs(_pvc("data-1")),)},
            )
        ]

    def test_extra_resource_on_revisit_is_reported(self) -> None:
        # GIVEN a baseline with one PVC revisited with an unexpected extra
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0"), _pvc("data-1")}))

        # WHEN discrepancies are calculated
        discrepancies = calculate_discrepancies(tracker.observations())

        # THEN the extra PVC is reported and nothing is missing
        assert discrepancies[0].qualified == {"extra": (_qs(_pvc("data-1")),)}

    def test_missing_and_extra_reported_together(self) -> None:
        # GIVEN a baseline PVC replaced by a differently-named one on revisit
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-1")}))

        # WHEN discrepancies are calculated
        discrepancies = calculate_discrepancies(tracker.observations())

        # THEN both the missing and the extra PVC are reported in one discrepancy
        assert discrepancies[0].qualified == {
            "missing": (_qs(_pvc("data-0")),),
            "extra": (_qs(_pvc("data-1")),),
        }

    def test_resized_pvc_is_reported_as_a_modification(self) -> None:
        # GIVEN a baseline PVC resized in place on revisit
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0", storage="1Gi")}))
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0", storage="2Gi")}))

        # WHEN discrepancies are calculated
        discrepancies = calculate_discrepancies(tracker.observations())

        # THEN a single 'resized' qualifier carries both baseline and current
        assert discrepancies[0].qualified == {
            "resized": (_qs(_pvc("data-0", storage="2Gi"), baseline=_pvc("data-0", storage="1Gi")),)
        }

    def test_phase_change_alone_is_not_a_discrepancy(self) -> None:
        # GIVEN a baseline PVC that reappears in a different phase
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))
        pending = PvcSnapshot(
            name="data-0",
            namespace="test-model",
            storage_class="csi-cephfs",
            requested_storage="1Gi",
            phase="Pending",
        )
        tracker.record(State.DEPLOYED, "test-model", frozenset({pending}))

        # WHEN discrepancies are calculated
        # THEN identity (which excludes phase) matches and nothing is reported
        assert calculate_discrepancies(tracker.observations()) == []

    def test_different_states_have_independent_baselines(self) -> None:
        # GIVEN baselines recorded and revisited for two different states
        tracker = StateResourceTracker()
        tracker.record(State.EMPTY_MODEL, "test-model", frozenset())
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))
        tracker.record(State.EMPTY_MODEL, "test-model", frozenset())
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))

        # WHEN discrepancies are calculated THEN neither state reports one
        assert calculate_discrepancies(tracker.observations()) == []

    def test_same_state_different_models_are_independent(self) -> None:
        # GIVEN the same state tracked across two models
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "model-a", frozenset({_pvc("data-0")}))
        tracker.record(State.DEPLOYED, "model-b", frozenset({_pvc("data-0")}))
        tracker.record(State.DEPLOYED, "model-a", frozenset())
        tracker.record(State.DEPLOYED, "model-b", frozenset({_pvc("data-0")}))

        # WHEN discrepancies are calculated THEN only model-a reports one
        discrepancies = calculate_discrepancies(tracker.observations())
        assert len(discrepancies) == 1
        assert discrepancies[0].model == "model-a"

    def test_skips_are_scoped_to_the_owning_application(self) -> None:
        # GIVEN two applications' PVCs where only the skipping one drifts
        tracker = StateResourceTracker()
        tracker.record(
            State.DEPLOYED,
            "test-model",
            frozenset({_pvc("data-target-0", application="target"), _pvc("data-neighbor-0", application="neighbor")}),
        )
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-neighbor-0", application="neighbor")}))

        # WHEN discrepancies are calculated with the target application skipping PVCs
        discrepancies = calculate_discrepancies(tracker.observations(), skips={"target": frozenset({"pvc"})})

        # THEN the skipped application's dropped PVC is excluded, so nothing is reported
        assert discrepancies == []

    def test_skip_excludes_kind_uniformly_across_visits(self) -> None:
        # GIVEN a skipped PVC present on the baseline visit but absent on re-entry
        # (as a transient collection difference would produce)
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0", application="target")}))
        tracker.record(State.DEPLOYED, "test-model", frozenset())

        # WHEN discrepancies are calculated with the owning application skipping PVCs
        discrepancies = calculate_discrepancies(tracker.observations(), skips={"target": frozenset({"pvc"})})

        # THEN the skip is applied uniformly to both visits, so no drift is reported
        assert discrepancies == []


class TestModelResourceDiscrepancyEntries:
    def test_missing_yields_structured_entry(self) -> None:
        # GIVEN a discrepancy with a missing PVC
        snapshot = _pvc("data-1")
        discrepancy = ModelResourceDiscrepancy(
            state=State.DEPLOYED,
            model="test-model",
            qualified={"missing": (_qs(snapshot),)},
        )

        # WHEN enumerating structured entries
        entries = list(discrepancy.entries())

        # THEN the entry carries generic selectable dimensions plus run context,
        # with no baseline counterpart for a presence qualifier
        assert entries == [
            DiscrepancyEntry(
                resource_type="pvc",
                qualifier="missing",
                state="deployed",
                model="test-model",
                snapshot=snapshot,
                baseline=None,
            )
        ]

    def test_extra_yields_structured_entry(self) -> None:
        # GIVEN a discrepancy with an extra PVC
        snapshot = _pvc("data-1")
        discrepancy = ModelResourceDiscrepancy(
            state=State.DEPLOYED,
            model="test-model",
            qualified={"extra": (_qs(snapshot),)},
        )

        # WHEN enumerating structured entries
        entries = list(discrepancy.entries())

        # THEN the extra PVC is reported with the 'extra' qualifier
        assert entries == [
            DiscrepancyEntry(
                resource_type="pvc",
                qualifier="extra",
                state="deployed",
                model="test-model",
                snapshot=snapshot,
                baseline=None,
            )
        ]

    def test_modification_entry_carries_baseline(self) -> None:
        # GIVEN a resized PVC discrepancy carrying both baseline and current
        baseline = _pvc("data-0", storage="1Gi")
        current = _pvc("data-0", storage="2Gi")
        discrepancy = ModelResourceDiscrepancy(
            state=State.DEPLOYED,
            model="test-model",
            qualified={"resized": (_qs(current, baseline=baseline),)},
        )

        # WHEN enumerating structured entries
        entries = list(discrepancy.entries())

        # THEN the entry exposes the drifted snapshot and its first-visit baseline
        assert entries == [
            DiscrepancyEntry(
                resource_type="pvc",
                qualifier="resized",
                state="deployed",
                model="test-model",
                snapshot=current,
                baseline=baseline,
            )
        ]


class TestResourceConsistencyReport:
    """The end-of-suite report test raises a structured error on discrepancies."""

    def test_passes_when_consistent(self) -> None:
        # GIVEN a tracker whose state re-entry is consistent
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))

        # WHEN the report runs THEN it does not raise
        run_resource_consistency_report(tracker, {})

    def test_raises_with_discrepancies_on_drift(self) -> None:
        # GIVEN a tracker whose revisit dropped a PVC
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0"), _pvc("data-1")}))
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))

        # WHEN the report runs THEN it raises carrying the structured discrepancies
        with pytest.raises(ResourceDiscrepancyError) as excinfo:
            run_resource_consistency_report(tracker, {})

        assert excinfo.value.discrepancies == (
            ModelResourceDiscrepancy(
                state=State.DEPLOYED,
                model="test-model",
                qualified={"missing": (_qs(_pvc("data-1")),)},
            ),
        )
