# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

import pytest
from test_suite.conftest import target_downgrade_charm

from bundle_builder_x import Charm, CharmChannel


class _RequestConfigStub:
    def getoption(self, option: str) -> str:
        assert option == "--target-downgrade-revision"
        return "27"


class _RequestStub:
    def __init__(self, fixtures: dict[str, object]) -> None:
        self.config = _RequestConfigStub()
        self.fixtures = fixtures

    def getfixturevalue(self, argname: str) -> object:
        return self.fixtures[argname]


class _WrappedFixture(Protocol):
    __wrapped__: Callable[[pytest.FixtureRequest, Path | None], Charm]


class _CharmhubClientStub:
    def __init__(self, charm: Charm) -> None:
        self.charm = charm
        self.call_kwargs: dict[str, object] | None = None

    def charm_from_store(
        self,
        charm_name: str,
        ubuntu_arch: str,
        charm_track: str | None,
        charm_risk: str | None,
        charm_branch: str | None,
        charm_revision: int | None,
        ubuntu_version: str | None,
    ) -> Charm:
        self.call_kwargs = {
            "charm_name": charm_name,
            "ubuntu_arch": ubuntu_arch,
            "charm_track": charm_track,
            "charm_risk": charm_risk,
            "charm_branch": charm_branch,
            "charm_revision": charm_revision,
            "ubuntu_version": ubuntu_version,
        }
        return self.charm


def test_resolves_explicit_downgrade_revision_for_local_target() -> None:
    target = Charm(
        name="my-charm",
        source_path=Path("/charms/my-charm"),
        ubuntu_version="24.04",
        ubuntu_arch="amd64",
        endpoints={},
        platforms=["machine"],
    )
    downgrade = Charm(
        name="my-charm",
        channel=CharmChannel.model_validate("1/stable"),
        revision=27,
        ubuntu_version="24.04",
        ubuntu_arch="amd64",
        endpoints={},
        platforms=["machine"],
    )
    charmhub_client = _CharmhubClientStub(downgrade)
    request = cast(
        pytest.FixtureRequest,
        _RequestStub(
            {
                "target_resolved_charm": target,
                "target_charm": "my-charm",
                "target_channel": None,
                "target_arch": "amd64",
                "charmhub_client": charmhub_client,
            }
        ),
    )
    fixture_function = cast(_WrappedFixture, target_downgrade_charm).__wrapped__
    resolved = fixture_function(request, local_downgrade_charm=None)

    assert resolved is downgrade
    assert charmhub_client.call_kwargs is not None
    assert charmhub_client.call_kwargs["charm_revision"] == 27
    assert charmhub_client.call_kwargs["ubuntu_version"] == "24.04"
