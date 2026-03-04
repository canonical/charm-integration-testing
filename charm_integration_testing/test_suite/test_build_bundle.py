# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest

from .scheduler.states import State


@pytest.mark.state(requires=State.NO_BUNDLE, provides=State.BUNDLE_BUILT)
def test_build_bundle() -> None:
    # TODO: implement bundle building logic
    pass


@pytest.mark.state(requires=State.BUNDLE_BUILT, provides=State.NO_CONTROLLER)
def test_verify_bundle() -> None:
    # TODO: implement Bundle verification logic
    pass
