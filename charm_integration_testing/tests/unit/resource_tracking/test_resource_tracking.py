# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from types import SimpleNamespace

import pytest
from resource_tracking import (
    ModelResourceDiscrepancy,
    PvcSnapshot,
    PvcSource,
    StateResourceTracker,
    calculate_discrepancies,
    diff_snapshots,
)
from resource_tracking.tracker import ResourceObservation
from test_suite.scheduler.states import State
from test_suite.test_resource_consistency_report import (
    test_resource_consistency_report as run_resource_consistency_report,
)


def _pvc(name: str, storage: str = "1Gi") -> PvcSnapshot:
    return PvcSnapshot(
        name=name,
        namespace="test-model",
        storage_class="csi-cephfs",
        requested_storage=storage,
        phase="Bound",
    )


def _raw_pvc(
    name: str,
    storage_class: str | None = "csi-cephfs",
    requested_storage: str | None = "1Gi",
    phase: str | None = "Bound",
) -> SimpleNamespace:
    """Build a raw V1PersistentVolumeClaim-like object for PvcSource mapping."""
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
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


class TestModelResourceDiscrepancyReportEntries:
    def test_missing_uses_stable_key_with_model_in_value(self) -> None:
        # GIVEN a discrepancy with a missing PVC
        discrepancy = ModelResourceDiscrepancy(
            state=State.DEPLOYED,
            model="test-model",
            missing=(_pvc("data-1"),),
            extra=(),
        )

        # WHEN rendering report entries
        entries = list(discrepancy.report_entries())

        # THEN the key is model-independent and the model is carried in the value
        assert entries == [
            (
                "resource:pvc:missing",
                "state=deployed model=test-model pvc=data-1 requested_storage=1Gi storage_class=csi-cephfs",
            )
        ]

    def test_extra_uses_stable_key(self) -> None:
        # GIVEN a discrepancy with an extra PVC
        discrepancy = ModelResourceDiscrepancy(
            state=State.DEPLOYED,
            model="test-model",
            missing=(),
            extra=(_pvc("data-1"),),
        )

        # WHEN rendering report entries
        entries = list(discrepancy.report_entries())

        # THEN the extra PVC is reported under a stable key
        assert entries == [
            (
                "resource:pvc:extra",
                "state=deployed model=test-model pvc=data-1 requested_storage=1Gi storage_class=csi-cephfs",
            )
        ]


class TestResourceConsistencyReport:
    """The end-of-suite report test emits metadata and fails on discrepancies."""

    def test_passes_and_emits_nothing_when_consistent(self) -> None:
        # GIVEN a tracker whose state re-entry is consistent
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))
        recorded: list[tuple[str, str]] = []

        # WHEN the report runs THEN it does not fail and emits nothing
        run_resource_consistency_report(tracker, lambda category, value: recorded.append((category, value)))
        assert recorded == []

    def test_fails_and_emits_metadata_on_discrepancy(self) -> None:
        # GIVEN a tracker whose revisit dropped a PVC
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0"), _pvc("data-1")}))
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))
        recorded: list[tuple[str, str]] = []

        # WHEN the report runs THEN it fails
        with pytest.raises(AssertionError):
            run_resource_consistency_report(tracker, lambda category, value: recorded.append((category, value)))

        # AND the discrepancy is emitted under a stable key before failing
        assert (
            "resource:pvc:missing",
            "state=deployed model=test-model pvc=data-1 requested_storage=1Gi storage_class=csi-cephfs",
        ) in recorded
