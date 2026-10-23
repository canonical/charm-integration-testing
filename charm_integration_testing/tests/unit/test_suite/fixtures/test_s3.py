# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from contextlib import contextmanager
from inspect import unwrap
from pathlib import Path

import pytest
import requests
from resource_registry import ResourceRegistry, ResourceTeardownWarning
from s3_storage import S3Credentials, S3Provider, S3StorageHandle, StaticS3Provider
from s3_storage_swift import KeystoneClient, KeystoneResourceRegistry, SwiftCredentials, SwiftS3Provider
from test_suite.fixtures import s3
from test_suite.fixtures.s3 import can_cleanup_session_storage, s3_config_paths
from test_suite.scheduler.states import State

pytest_plugins = ["pytester"]


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


def test_fixture_selects_swift_loader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # GIVEN an explicit Swift configuration at the suite composition boundary
    path = tmp_path / "swift.conf"
    path.write_text(
        "S3_OS_AUTH_URL=https://identity.test/v3\n"
        "S3_OS_USERNAME=user\nS3_OS_PASSWORD=test-password\nS3_OS_USER_DOMAIN_NAME=users\n"
        "S3_OS_PROJECT_NAME=project\nS3_OS_PROJECT_DOMAIN_NAME=projects\n"
        "S3_OS_STORAGE_URL=https://storage.test/swift/v1/AUTH_project\n"
        "S3_CLEANUP_ACCESS_KEY=cleanup-access\nS3_CLEANUP_SECRET_KEY=cleanup-secret\n"
    )
    monkeypatch.setenv("S3_CONFIG_fixture_cloud", str(path))
    # WHEN the suite fixture resolves that cloud, THEN it opts into the sibling implementation
    resources = ResourceRegistry([], logging.getLogger(__name__))
    with requests.Session() as session:
        registry = unwrap(s3.s3_provider_registry)(session, resources, True)
        assert isinstance(registry.provider_for_cloud("fixture-cloud"), SwiftS3Provider)


@pytest.mark.parametrize("fail_cleanup", [False, True], ids=["success", "failure"])
def test_session_registry_cleans_up_before_closing_http_session(
    monkeypatch: pytest.MonkeyPatch, fail_cleanup: bool
) -> None:
    # GIVEN an injected boundary loader and an observable HTTP session
    events: list[str] = []

    class SessionStub(requests.Session):
        def close(self) -> None:
            events.append("session-closed")
            super().close()

    class KeystoneStub(KeystoneClient):
        def __init__(self) -> None:
            self.credentials = SwiftCredentials(
                "https://identity.test/v3", "user", "password", "users", "project", "projects", "https://storage.test"
            )

        def create_credentials(self) -> S3Credentials:
            return S3Credentials("access", "secret")

        def delete_credentials(self, access_key: str) -> None:
            assert "session-closed" not in events
            events.append("credential-deleted")
            if fail_cleanup:
                raise RuntimeError("Credential cleanup failed")

    monkeypatch.setattr(requests, "Session", SessionStub)
    # WHEN the session registry tears down, THEN cleanup still has access to the open session
    with contextmanager(unwrap(s3.s3_http_session))():
        resources = ResourceRegistry([], logging.getLogger(__name__))
        parent = S3StorageHandle("test")
        resources.register(parent)
        KeystoneResourceRegistry(resources, True).create_credentials(KeystoneStub(), parent)
        assert events == []
        if fail_cleanup:
            with pytest.warns(ResourceTeardownWarning, match="Credential cleanup failed"):
                resources.teardown_all()
        else:
            resources.teardown_all()
        assert events == ["credential-deleted"]
    assert events == ["credential-deleted", "session-closed"]


def test_provider_fixture_injects_session_and_resource_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # GIVEN an observable provider factory at the suite composition boundary
    registry = ResourceRegistry([], logging.getLogger(__name__))
    calls: list[Path] = []

    def provider_factory(
        path: Path, session: requests.Session, *, resources: KeystoneResourceRegistry | None = None
    ) -> S3Provider:
        assert session is http_session
        assert isinstance(resources, KeystoneResourceRegistry)
        calls.append(path)
        return StaticS3Provider("http://s3.test", "test", S3Credentials("access", "secret"))

    monkeypatch.setenv("S3_CONFIG_fixture_cloud", str(tmp_path / "s3.conf"))
    monkeypatch.setattr(s3, "provider_from_config", provider_factory)
    # WHEN a cloud is resolved, THEN the shared dependencies reach the loader
    with requests.Session() as http_session:
        providers = unwrap(s3.s3_provider_registry)(http_session, registry, True)
        assert calls == []
        providers.provider_for_cloud("fixture-cloud")
    assert calls == [tmp_path / "s3.conf"]
    assert registry.registered_handles() == []


def test_fixture_graph_keeps_http_session_open_until_credential_teardown(pytester: pytest.Pytester) -> None:
    # GIVEN the real session-registry and S3 fixture dependency graph
    pytester.makeconftest(
        """
import logging
from pathlib import Path

import pytest
import requests
from kubernetes_client import KubernetesClientRegistry
from test_suite.conftest import session_resource_registry
from test_suite.fixtures.s3 import s3_http_session, s3_provider_registry, s3_resources_for_provider

class SessionStub(requests.Session):
    closed = False

    def close(self):
        self.closed = True
        with Path("events").open("a") as events:
            events.write("session-closed\\n")
        super().close()

@pytest.fixture(scope="session", autouse=True)
def stub_http_session():
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(requests, "Session", SessionStub)
        yield

@pytest.fixture(scope="session")
def logger():
    return logging.getLogger(__name__)

@pytest.fixture(scope="session")
def log_dir():
    return None

@pytest.fixture(scope="session")
def juju_backend():
    return None

@pytest.fixture(scope="session")
def kubernetes_client_registry():
    return KubernetesClientRegistry({})

@pytest.fixture(scope="session")
def cleanup_s3_buckets():
    return True
"""
    )
    pytester.makepyfile(
        """
from pathlib import Path
from s3_storage import S3Credentials, StaticS3Provider
from s3_storage_swift import KeystoneResourceRegistry
from s3_storage_swift.credentials import SwiftCredentials

class KeystoneStub:
    credentials = SwiftCredentials(
        "https://identity.test/v3", "user", "password", "users", "project", "projects", "https://storage.test"
    )

    def __init__(self, session):
        self.session = session

    def create_credentials(self):
        return S3Credentials("access", "secret")

    def delete_credentials(self, access_key):
        assert not self.session.closed
        with Path("events").open("a") as events:
            events.write("credential-deleted\\n")

def test_lifecycle(session_resource_registry, s3_http_session, s3_provider_registry, s3_resources_for_provider):
    provider = StaticS3Provider("http://s3.test", "test", S3Credentials("static-access", "static-secret"))
    bucket = s3_resources_for_provider(provider).new_bucket()
    KeystoneResourceRegistry(session_resource_registry, True).create_credentials(
        KeystoneStub(s3_http_session), parent=bucket.handle
    )
"""
    )

    # WHEN pytest closes the session, THEN credential teardown precedes HTTP-session closure
    result = pytester.runpytest("-q")
    result.assert_outcomes(passed=1)
    assert (pytester.path / "events").read_text().splitlines() == ["credential-deleted", "session-closed"]


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
