# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from kubernetes_client import PvcSnapshot

from test_suite.resource_tracking import StateResourceTracker
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


class TestStateResourceTracker:
    def test_first_visit_records_baseline_without_discrepancy(self) -> None:
        # GIVEN a fresh tracker
        tracker = StateResourceTracker()

        # WHEN a state is visited for the first time
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))

        # THEN the snapshot becomes the baseline and no discrepancy is recorded
        assert tracker.baselines() == {(State.DEPLOYED, "test-model"): frozenset({_pvc("data-0")})}
        assert tracker.discrepancies() == []

    def test_identical_revisit_reports_no_discrepancy(self) -> None:
        # GIVEN a state with a recorded baseline
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))

        # WHEN the same state is revisited with the same resources
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))

        # THEN no discrepancy is recorded
        assert tracker.discrepancies() == []

    def test_missing_resource_on_revisit_is_reported(self) -> None:
        # GIVEN a state whose baseline has two PVCs
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0"), _pvc("data-1")}))

        # WHEN the state is revisited with one PVC missing
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))

        # THEN the missing PVC is reported as a discrepancy and nothing is extra
        discrepancies = tracker.discrepancies()
        assert len(discrepancies) == 1
        assert discrepancies[0].state == State.DEPLOYED
        assert discrepancies[0].model == "test-model"
        assert discrepancies[0].missing == (_pvc("data-1"),)
        assert discrepancies[0].extra == ()

    def test_extra_resource_on_revisit_is_reported(self) -> None:
        # GIVEN a state whose baseline has one PVC
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))

        # WHEN the state is revisited with an unexpected extra PVC
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0"), _pvc("data-1")}))

        # THEN the extra PVC is reported as a discrepancy and nothing is missing
        discrepancies = tracker.discrepancies()
        assert len(discrepancies) == 1
        assert discrepancies[0].missing == ()
        assert discrepancies[0].extra == (_pvc("data-1"),)

    def test_missing_and_extra_reported_together(self) -> None:
        # GIVEN a baseline PVC
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))

        # WHEN the state is revisited with the baseline PVC replaced by another
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-1")}))

        # THEN both the missing and the extra PVC are reported in one discrepancy
        discrepancies = tracker.discrepancies()
        assert len(discrepancies) == 1
        assert discrepancies[0].missing == (_pvc("data-0"),)
        assert discrepancies[0].extra == (_pvc("data-1"),)

    def test_phase_change_alone_is_not_a_discrepancy(self) -> None:
        # GIVEN a baseline PVC in the Bound phase
        tracker = StateResourceTracker()
        bound = _pvc("data-0")
        tracker.record(State.DEPLOYED, "test-model", frozenset({bound}))

        # WHEN the same PVC reappears in a different phase
        pending = PvcSnapshot(
            name="data-0",
            namespace="test-model",
            storage_class="csi-cephfs",
            requested_storage="1Gi",
            phase="Pending",
        )
        tracker.record(State.DEPLOYED, "test-model", frozenset({pending}))

        # THEN identity (which excludes phase) matches and nothing is reported
        assert tracker.discrepancies() == []

    def test_different_states_have_independent_baselines(self) -> None:
        # GIVEN baselines recorded for two different states
        tracker = StateResourceTracker()
        tracker.record(State.EMPTY_MODEL, "test-model", frozenset())
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))

        # WHEN each state is revisited with its own resources unchanged
        tracker.record(State.EMPTY_MODEL, "test-model", frozenset())
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))

        # THEN neither state reports a discrepancy
        assert tracker.discrepancies() == []

    def test_same_state_different_models_are_independent(self) -> None:
        # GIVEN a baseline for the same state across two models
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "model-a", frozenset({_pvc("data-0")}))
        tracker.record(State.DEPLOYED, "model-b", frozenset({_pvc("data-0")}))

        # WHEN model-a loses its PVC on revisit but model-b does not
        tracker.record(State.DEPLOYED, "model-a", frozenset())
        tracker.record(State.DEPLOYED, "model-b", frozenset({_pvc("data-0")}))

        # THEN only model-a reports a discrepancy
        discrepancies = tracker.discrepancies()
        assert len(discrepancies) == 1
        assert discrepancies[0].model == "model-a"


class TestResourceConsistencyReport:
    """Test suite for the end-of-suite report emitted via execution_metadata."""

    def test_baselines_are_reported(self) -> None:
        # GIVEN a tracker with a baseline for a state
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0"), _pvc("data-1")}))
        recorded: list[tuple[str, str]] = []

        # WHEN the report runs
        run_resource_consistency_report(tracker, lambda category, value: recorded.append((category, value)))

        # THEN each baseline PVC is reported under its state/model category
        assert ("resource:pvc:baseline:deployed:test-model", "data-0") in recorded
        assert ("resource:pvc:baseline:deployed:test-model", "data-1") in recorded
        # AND no inconsistency is reported when there are no discrepancies
        assert not any(category == "resource:pvc:inconsistency" for category, _ in recorded)

    def test_missing_pvc_is_reported(self) -> None:
        # GIVEN a tracker whose revisit dropped a PVC
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0"), _pvc("data-1")}))
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))
        recorded: list[tuple[str, str]] = []

        # WHEN the report runs
        run_resource_consistency_report(tracker, lambda category, value: recorded.append((category, value)))

        # THEN the missing PVC is reported under the missing and inconsistency categories
        assert ("resource:pvc:missing:deployed:test-model", "data-1") in recorded
        assert (
            "resource:pvc:inconsistency",
            "state=deployed model=test-model kind=missing pvc=data-1 "
            "storage_class=csi-cephfs requested_storage=1Gi",
        ) in recorded

    def test_extra_pvc_is_reported(self) -> None:
        # GIVEN a tracker whose revisit gained an unexpected PVC
        tracker = StateResourceTracker()
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0")}))
        tracker.record(State.DEPLOYED, "test-model", frozenset({_pvc("data-0"), _pvc("data-1")}))
        recorded: list[tuple[str, str]] = []

        # WHEN the report runs
        run_resource_consistency_report(tracker, lambda category, value: recorded.append((category, value)))

        # THEN the extra PVC is reported under the extra and inconsistency categories
        assert ("resource:pvc:extra:deployed:test-model", "data-1") in recorded
        assert (
            "resource:pvc:inconsistency",
            "state=deployed model=test-model kind=extra pvc=data-1 "
            "storage_class=csi-cephfs requested_storage=1Gi",
        ) in recorded

    def test_empty_tracker_reports_nothing(self) -> None:
        # GIVEN a tracker with no recorded state
        tracker = StateResourceTracker()
        recorded: list[tuple[str, str]] = []

        # WHEN the report runs
        run_resource_consistency_report(tracker, lambda category, value: recorded.append((category, value)))

        # THEN nothing is reported
        assert recorded == []
