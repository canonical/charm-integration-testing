# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from inspect import unwrap
from pathlib import Path

import pytest
from resource_registry import ResourceRegistry
from s3_storage import S3Credentials, StaticS3Provider
from test_suite.fixtures import s3
from test_suite.fixtures.s3 import can_cleanup_session_storage, s3_config_paths
from test_suite.scheduler.states import State


def test_cloud_config_paths_follow_kubeconfig_aliases(tmp_path: Path) -> None:
    # GIVEN path variables and unrelated legacy credentials
    environ = {
        "S3_CONFIG_target_cloud": str(tmp_path / "target.conf"),
        "S3_CONFIG_NEIGHBOR_CLOUD": str(tmp_path / "neighbor.conf"),
        "S3_CONFIG_unused": "",
        "S3_CONFIG_whitespace": " \t\n ",
        "S3_CONFIG_": "   ",
        "S3_ACCESS_KEY": "ignored",
    }
    # WHEN paths are read, THEN logical cloud aliases are normalized
    assert s3_config_paths(environ) == {
        "target-cloud": tmp_path / "target.conf",
        "neighbor-cloud": tmp_path / "neighbor.conf",
    }


def test_cloud_config_paths_strip_surrounding_whitespace(tmp_path: Path) -> None:
    # GIVEN an environment value with surrounding whitespace and a space inside the path
    config = tmp_path / "my config.conf"
    # WHEN the path is resolved, THEN only surrounding whitespace is discarded
    assert s3_config_paths({"S3_CONFIG_target_cloud": f" \t{config}\n "}) == {"target-cloud": config}


def test_conflicting_normalized_cloud_aliases_fail(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Conflicting S3 configuration paths for cloud 'target-cloud'"):
        s3_config_paths(
            {"S3_CONFIG_target_cloud": str(tmp_path / "first"), "S3_CONFIG_TARGET_CLOUD": str(tmp_path / "second")}
        )


def test_equivalent_paths_can_share_a_normalized_cloud_alias(tmp_path: Path) -> None:
    config = tmp_path / "s3.conf"
    assert s3_config_paths(
        {"S3_CONFIG_target_cloud": str(config), "S3_CONFIG_TARGET_CLOUD": str(tmp_path / "." / "s3.conf")}
    ) == {"target-cloud": config}


def test_missing_cloud_suffix_fails() -> None:
    with pytest.raises(ValueError, match="requires a cloud name"):
        s3_config_paths({"S3_CONFIG_": "/path/to/config"})


def test_provider_fixture_loads_static_config_lazily(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # GIVEN an explicit static S3 configuration at the suite composition boundary
    path = tmp_path / "s3.conf"
    path.write_text(
        "S3_ENDPOINT=http://s3.test\nS3_ACCESS_KEY=access\nS3_SECRET_KEY=secret\nS3_BUCKET_NAMESPACE=shared\n"
    )
    monkeypatch.setenv("S3_CONFIG_fixture_cloud", str(path))
    # WHEN the suite fixture resolves that cloud, THEN it returns the static provider
    provider = unwrap(s3.s3_provider_registry)().provider_for_cloud("fixture-cloud")
    assert isinstance(provider, StaticS3Provider)


@pytest.mark.parametrize("namespace", ["first", "alias"], ids=["equal-handles", "equal-resource-ids"])
def test_resource_scopes_are_lazy_shared_by_identity_and_resolve_late_providers(namespace: str) -> None:
    # GIVEN a session factory with no providers used yet
    resources = ResourceRegistry([], logging.getLogger(__name__))
    scopes = unwrap(s3.s3_resources_for_provider)(resources, True)
    assert resources.registered_handles() == []
    first = StaticS3Provider("http://s3.test", "first", S3Credentials("access", "secret"), identity="shared")
    equivalent = StaticS3Provider("http://s3.test", namespace, S3Credentials("access", "secret"), identity="shared")

    # WHEN equivalent providers are resolved by different callers, THEN their scope is shared
    first_scope = scopes(first)
    assert scopes(first) is first_scope
    assert scopes(equivalent) is first_scope
    assert resources.registered_handles() == [first.handle]

    # WHEN a new provider appears later, THEN it receives a separate scope on demand
    late = StaticS3Provider("http://other.test", "late", S3Credentials("other", "secret"))
    assert scopes(late) is not first_scope
    assert resources.registered_handles() == [first.handle, late.handle]


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
