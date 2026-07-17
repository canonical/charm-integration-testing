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
    PvcSnapshot,
    PvcSource,
    ResourceDiscrepancyError,
    StateResourceTracker,
    calculate_discrepancies,
    diff_snapshots,
)
from resource_tracking.tracker import ResourceObservation
from test_suite.scheduler.states import State
from test_suite.test_resource_consistency_report import (
    test_resource_consistency_report as run_resource_consistency_report,
)


def _pvc(name: str, storage: str = "1Gi", application: str = "", namespace: str = "test-model") -> PvcSnapshot:
    return PvcSnapshot(
        name=name,
        namespace=namespace,
        storage_class="csi-cephfs",
        requested_storage=storage,
        phase="Bound",
        application=application,
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

    def list_model_pvcs(self, model: str) -> list[SimpleNamespace]:
        self.requested_model = model
        return self._pvcs


class _RaisingKubernetesClient:
    """Stand-in whose PVC listing always raises, to exercise error handling."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def list_model_pvcs(self, model: str) -> list[SimpleNamespace]:
        raise self._error


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
        collected = KubernetesResourceCollector(
            {"test-controller": client},  # type: ignore[arg-type]
            registry,  # type: ignore[arg-type]
        ).collect(_LOGGER)

        # THEN only the model handle yields a snapshot set
        assert collected == [CollectedResources("test-model", frozenset({_pvc("data-0")}))]

    def test_api_errors_are_skipped(self) -> None:
        # GIVEN a client whose PVC listing fails
        registry = _FakeResourceRegistry([JujuModelHandle(controller="test-controller", model="test-model")])
        client = _RaisingKubernetesClient(ApiException(status=404))

        # WHEN the collector gathers resources
        collected = KubernetesResourceCollector(
            {"test-controller": client},  # type: ignore[arg-type]
            registry,  # type: ignore[arg-type]
        ).collect(_LOGGER)

        # THEN the model is still recorded, with an empty snapshot set
        assert collected == [CollectedResources("test-model", frozenset())]

    def test_skips_are_scoped_to_the_owning_application(self) -> None:
        # GIVEN two PVCs owned by different applications, one of which skips PVCs
        registry = _FakeResourceRegistry([JujuModelHandle(controller="test-controller", model="test-model")])
        client = _FakeKubernetesClient(
            [
                _raw_pvc("pgdata-target-0", labels={"app.kubernetes.io/name": "target"}),
                _raw_pvc("data-neighbor-0", labels={"app.kubernetes.io/name": "neighbor"}),
            ]
        )
        resource_skips = {"target": frozenset({"pvc"})}

        # WHEN the collector gathers resources
        collected = KubernetesResourceCollector(
            {"test-controller": client},  # type: ignore[arg-type]
            registry,  # type: ignore[arg-type]
            resource_skips=resource_skips,
        ).collect(_LOGGER)

        # THEN only the skipping application's PVC is dropped
        assert collected == [
            CollectedResources("test-model", frozenset({_pvc("data-neighbor-0", application="neighbor")}))
        ]

    def test_each_controller_is_queried_against_its_own_cluster(self) -> None:
        # GIVEN a CMR-style registry with models on two different Kubernetes controllers
        registry = _FakeResourceRegistry(
            [
                JujuModelHandle(controller="target-controller", model="target-model"),
                JujuModelHandle(controller="neighbor-controller", model="neighbor-model"),
            ]
        )
        target_client = _FakeKubernetesClient([_raw_pvc("data-target-0")])
        neighbor_client = _FakeKubernetesClient([_raw_pvc("data-neighbor-0")])

        # WHEN the collector gathers resources using a per-controller client mapping
        collected = KubernetesResourceCollector(
            {"target-controller": target_client, "neighbor-controller": neighbor_client},  # type: ignore[arg-type]
            registry,  # type: ignore[arg-type]
        ).collect(_LOGGER)

        # THEN each model is queried against its own controller's client
        assert target_client.requested_model == "target-model"
        assert neighbor_client.requested_model == "neighbor-model"
        assert set(collected) == {
            CollectedResources("target-model", frozenset({_pvc("data-target-0", namespace="target-model")})),
            CollectedResources("neighbor-model", frozenset({_pvc("data-neighbor-0", namespace="neighbor-model")})),
        }

    def test_models_on_a_controller_without_a_kubernetes_client_are_skipped(self) -> None:
        # GIVEN a CMR-style registry where the neighbor controller has no Kubernetes
        # client (e.g. it is bootstrapped on OpenStack, not Kubernetes)
        registry = _FakeResourceRegistry(
            [
                JujuModelHandle(controller="target-controller", model="target-model"),
                JujuModelHandle(controller="neighbor-controller", model="neighbor-model"),
            ]
        )
        target_client = _FakeKubernetesClient([_raw_pvc("data-target-0")])

        # WHEN the collector gathers resources with only the target controller mapped
        collected = KubernetesResourceCollector(
            {"target-controller": target_client},  # type: ignore[arg-type]
            registry,  # type: ignore[arg-type]
        ).collect(_LOGGER)

        # THEN only the target model is recorded; the non-Kubernetes neighbor is skipped entirely
        assert collected == [
            CollectedResources("target-model", frozenset({_pvc("data-target-0", namespace="target-model")}))
        ]


class TestDiffSnapshots:
    def test_missing_and_extra_are_grouped_by_qualifier(self) -> None:
        # GIVEN a baseline PVC replaced by a different one
        baseline = frozenset({_pvc("data-0")})
        current = frozenset({_pvc("data-1")})

        # WHEN the two snapshot sets are diffed
        qualifiers = diff_snapshots(baseline, current)

        # THEN the dropped PVC is 'missing' and the new one is 'extra'
        assert qualifiers == {"missing": (_pvc("data-0"),), "extra": (_pvc("data-1"),)}

    def test_identical_sets_yield_empty_qualifiers(self) -> None:
        # GIVEN two identical snapshot sets
        snapshots = frozenset({_pvc("data-0")})

        # WHEN they are diffed
        qualifiers = diff_snapshots(snapshots, snapshots)

        # THEN every qualifier is empty
        assert qualifiers == {"missing": (), "extra": ()}
        assert not any(qualifiers.values())

    def test_qualifiers_are_sorted_by_identity(self) -> None:
        # GIVEN a baseline that gains two extra PVCs out of identity order
        baseline: frozenset[PvcSnapshot] = frozenset()
        current = frozenset({_pvc("data-1"), _pvc("data-0")})

        # WHEN the sets are diffed
        qualifiers = diff_snapshots(baseline, current)

        # THEN the extra PVCs are returned sorted by identity
        assert qualifiers["extra"] == (_pvc("data-0"), _pvc("data-1"))


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
                missing=(_pvc("data-1"),),
                extra=(),
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
        assert discrepancies[0].missing == ()
        assert discrepancies[0].extra == (_pvc("data-1"),)

    def test_missing_and_extra_reported_together(self) -> None:
        # GIVEN a baseline PVC replaced by a different one on revisit
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-1")}))

        # WHEN discrepancies are calculated
        discrepancies = calculate_discrepancies(tracker.observations())

        # THEN both the missing and the extra PVC are reported in one discrepancy
        assert discrepancies[0].missing == (_pvc("data-0"),)
        assert discrepancies[0].extra == (_pvc("data-1"),)

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


class TestModelResourceDiscrepancyEntries:
    def test_missing_yields_structured_entry(self) -> None:
        # GIVEN a discrepancy with a missing PVC
        snapshot = _pvc("data-1")
        discrepancy = ModelResourceDiscrepancy(
            state=State.DEPLOYED,
            model="test-model",
            missing=(snapshot,),
            extra=(),
        )

        # WHEN enumerating structured entries
        entries = list(discrepancy.entries())

        # THEN the entry carries generic selectable dimensions plus run context
        assert entries == [
            DiscrepancyEntry(
                resource_type="pvc",
                qualifier="missing",
                state="deployed",
                model="test-model",
                snapshot=snapshot,
            )
        ]

    def test_extra_yields_structured_entry(self) -> None:
        # GIVEN a discrepancy with an extra PVC
        snapshot = _pvc("data-1")
        discrepancy = ModelResourceDiscrepancy(
            state=State.DEPLOYED,
            model="test-model",
            missing=(),
            extra=(snapshot,),
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
        run_resource_consistency_report(tracker)

    def test_raises_with_discrepancies_on_drift(self) -> None:
        # GIVEN a tracker whose revisit dropped a PVC
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0"), _pvc("data-1")}))
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))

        # WHEN the report runs THEN it raises carrying the structured discrepancies
        with pytest.raises(ResourceDiscrepancyError) as excinfo:
            run_resource_consistency_report(tracker)

        assert excinfo.value.discrepancies == (
            ModelResourceDiscrepancy(
                state=State.DEPLOYED,
                model="test-model",
                missing=(_pvc("data-1"),),
                extra=(),
            ),
        )
