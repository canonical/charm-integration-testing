# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

import pytest
import test_suite.conftest as test_suite_conftest
from test_suite.conftest import target_downgrade_charm, target_resolved_charm

from bundle_builder_x import Charm, CharmChannel
from bundle_builder_x import JujuVersion as BundleJujuVersion


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
        platform: str | None,
        juju_version: BundleJujuVersion | None,
        charm_track: str | None,
        charm_risk: str | None,
        charm_branch: str | None,
        charm_revision: int | None,
        ubuntu_version: str | None,
    ) -> Charm:
        self.call_kwargs = {
            "charm_name": charm_name,
            "ubuntu_arch": ubuntu_arch,
            "platform": platform,
            "juju_version": juju_version,
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
        test_channel=CharmChannel.model_validate("2/edge"),
        test_revision=42,
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
                "target_platform": "machine",
                "juju_cli_version": "3.6.1",
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
    assert charmhub_client.call_kwargs["platform"] == "machine"
    assert charmhub_client.call_kwargs["juju_version"] == BundleJujuVersion.parse("3.6.1")
    assert charmhub_client.call_kwargs["charm_track"] == "2"
    assert charmhub_client.call_kwargs["charm_risk"] == "edge"


def test_resolves_local_target_with_declared_test_context(monkeypatch: pytest.MonkeyPatch) -> None:
    local_target = Path("/charms/target.charm")
    resolved_target = Charm(
        name="my-charm",
        source_path=Path("/cache/target"),
        test_channel=CharmChannel.model_validate("2/edge"),
        test_revision=42,
        ubuntu_version="26.04",
        ubuntu_arch="amd64",
        endpoints={},
        platforms=["machine"],
    )
    resolve_calls: list[tuple[Path, str | None, str | None, int | None]] = []

    def resolve_local_charm(
        request: pytest.FixtureRequest,
        charm_path: Path,
        ubuntu_version: str | None,
        test_channel: str | None = None,
        test_revision: int | None = None,
    ) -> Charm:
        resolve_calls.append((charm_path, ubuntu_version, test_channel, test_revision))
        return resolved_target

    monkeypatch.setattr(test_suite_conftest, "_resolve_local_charm", resolve_local_charm)
    request = cast(
        pytest.FixtureRequest,
        _RequestStub(
            {
                "target_charm": "my-charm",
                "target_channel": "2/edge",
                "target_revision": 42,
                "target_series": "26.04",
                "target_arch": "amd64",
                "target_platform": "machine",
                "juju_cli_version": "3.6.1",
                "charmhub_client": object(),
            }
        ),
    )
    fixture_function = cast(_WrappedFixture, target_resolved_charm).__wrapped__

    resolved = fixture_function(request, local_target)

    assert resolved is resolved_target
    assert resolve_calls == [(local_target, "26.04", "2/edge", 42)]


def test_resolves_local_downgrade_artifact_for_target_base(monkeypatch: pytest.MonkeyPatch) -> None:
    target = Charm(
        name="my-charm",
        source_path=Path("/charms/target"),
        test_channel=CharmChannel.model_validate("2/edge"),
        test_revision=42,
        ubuntu_version="26.04",
        ubuntu_arch="amd64",
        endpoints={},
        platforms=["machine"],
    )
    local_downgrade = Path("/charms/downgrade.charm")
    resolved_downgrade = Charm(
        name="my-charm",
        source_path=Path("/cache/downgrade"),
        ubuntu_version="26.04",
        ubuntu_arch="amd64",
        endpoints={},
        platforms=["machine"],
    )
    resolve_calls: list[tuple[Path, str | None, str | None, int | None]] = []

    def resolve_local_charm(
        request: pytest.FixtureRequest,
        charm_path: Path,
        target_series: str | None,
        test_channel: str | None = None,
        test_revision: int | None = None,
    ) -> Charm:
        resolve_calls.append((charm_path, target_series, test_channel, test_revision))
        return resolved_downgrade

    monkeypatch.setattr(test_suite_conftest, "_resolve_local_charm", resolve_local_charm)
    request = cast(
        pytest.FixtureRequest,
        _RequestStub({"target_resolved_charm": target}),
    )
    fixture_function = cast(_WrappedFixture, target_downgrade_charm).__wrapped__

    resolved = fixture_function(request, local_downgrade_charm=local_downgrade)

    assert resolved is resolved_downgrade
    assert resolve_calls == [(local_downgrade, "26.04", "2/edge", None)]
