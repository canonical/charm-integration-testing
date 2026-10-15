# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from unittest.mock import Mock

import pytest
from s3_storage import S3Provider
from test_suite.fixtures.s3 import can_cleanup_session_storage, cleanup_pending_s3_resources
from test_suite.scheduler.states import State


def test_pending_s3_cleanup_attempts_all_providers_and_surfaces_failure() -> None:
    failed = Mock(spec=S3Provider)
    failed.cleanup_pending_resources.side_effect = RuntimeError("cleanup failed")
    succeeding = Mock(spec=S3Provider)

    with pytest.raises(RuntimeError, match="Pending S3 cleanup failed for 1 provider"):
        cleanup_pending_s3_resources({"failed": failed, "succeeding": succeeding})

    failed.cleanup_pending_resources.assert_called_once_with()
    succeeding.cleanup_pending_resources.assert_called_once_with()


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (State.NO_BUNDLE, True),
        (State.NO_CONTROLLER, True),
        (State.NO_MODEL, False),
        (State.EMPTY_MODEL, False),
        (State.DEPLOYED, False),
        (State.DEPLOYED_HA, False),
        (State.NEIGHBOR_ONLY, False),
        (State.DEPLOYED_WITH_OLD_REVISION, False),
        (State.DEPLOYED_WITH_UPGRADED_CONTROLLER, False),
    ],
    ids=lambda value: value.name.lower() if isinstance(value, State) else str(value),
)
def test_session_storage_cleanup_is_limited_to_new_controller_states(state: State, expected: bool) -> None:
    assert can_cleanup_session_storage(state) is expected
