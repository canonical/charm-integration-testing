# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import textwrap
from inspect import unwrap
from pathlib import Path

import pytest
from pytest import Pytester
from resource_registry import ResourceRegistry
from s3_storage import S3Credentials, StaticS3Provider
from s3_storage.resource_registry import S3SessionHandle
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
    session_handle = S3SessionHandle()
    resources.register(handle=session_handle)
    scopes = unwrap(s3.s3_resources_for_provider)(resources, True, session_handle)
    assert resources.registered_handles() == [session_handle]
    first = StaticS3Provider("http://s3.test", "first", S3Credentials("access", "secret"), identity="shared")
    equivalent = StaticS3Provider("http://s3.test", namespace, S3Credentials("access", "secret"), identity="shared")

    # WHEN equivalent providers are resolved by different callers, THEN their scope is shared
    first_scope = scopes(first)
    assert scopes(first) is first_scope
    assert scopes(equivalent) is first_scope
    assert resources.registered_handles() == [session_handle, first.handle]

    # WHEN a new provider appears later, THEN it receives a separate scope on demand
    late = StaticS3Provider("http://other.test", "late", S3Credentials("other", "secret"))
    assert scopes(late) is not first_scope
    assert resources.registered_handles() == [session_handle, first.handle, late.handle]


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


def test_juju_client_fixture_graph_shares_s3_scope_and_tears_it_down_after_controllers(pytester: Pytester) -> None:
    support_path = str(Path(__file__).resolve().parents[3])
    pytester.makeconftest(
        f"import sys\nsys.path.insert(0, {support_path!r})\n"
        + textwrap.dedent(
            """
            import logging
            from datetime import datetime, timedelta
            from pathlib import Path

            import pytest
            from juju import JujuApplicationInfo
            from juju.backend import JujuTask
            from juju.extension import JujuExtension
            from kubernetes_client import KubernetesClientRegistry
            from s3_storage import S3Bucket, S3BucketHandle, S3Credentials, S3Provider, S3ProviderRegistry, S3StorageHandle
            from test_suite.conftest import juju_client, session_resource_registry
            from test_suite.fixtures.s3 import s3_resources_for_provider, s3_session_handle
            from unit.extensions.shared import NullJujuBackend

            class RecordingBackend(NullJujuBackend):
                def __init__(self, events_path):
                    self.events_path = events_path
                    self.config = {}

                def bootstrap_controller(
                    self, cloud, controller, controller_constraints, bootstrap_configuration,
                    metadata_source=None, agent_version=None
                ):
                    pass

                def kill_controller(self, controller):
                    with self.events_path.open("a") as events:
                        events.write(f"controller:{controller}\\n")

                def deploy_bundle_file(self, model, bundle, timeout=None, trust=False, force=False):
                    pass

                def list_applications(self, model):
                    return {"storage": JujuApplicationInfo(charm="s3-integrator", revision=1)}

                def application_charm(self, model, application):
                    return "s3-integrator"

                def get_application_config(self, model, application):
                    return self.config.get(model.resource_id, {})

                def configure_application(self, model, application, values):
                    self.config[model.resource_id] = values

                def wait_application_scaled(self, model, application, timeout):
                    pass

                def wait_application_settled(self, model, application, timeout):
                    pass

                def num_units(self, model, application):
                    return 1

                def cloud_for_model(self, model):
                    return "target-cloud"

                def run_action(self, model, unit, action, arguments):
                    return JujuTask("action", 0, "completed", "", "")

            class RecordingBucket(S3Bucket):
                def __init__(self, handle, events_path):
                    self._handle = handle
                    self.events_path = events_path

                @property
                def handle(self):
                    return self._handle

                def provision(self):
                    return self.handle.name, S3Credentials("access", "secret")

                def cleanup(self):
                    with self.events_path.open("a") as events:
                        events.write("bucket\\n")

            class RecordingProvider(S3Provider):
                def __init__(self, events_path):
                    self.events_path = events_path
                    self.buckets = []

                @property
                def endpoint(self):
                    return "https://s3.test"

                @property
                def region(self):
                    return "us-east-1"

                @property
                def handle(self):
                    return S3StorageHandle("shared")

                def new_bucket(self):
                    bucket = RecordingBucket(
                        S3BucketHandle("shared", f"bucket-{len(self.buckets)}"), self.events_path
                    )
                    self.buckets.append(bucket)
                    return bucket

                def cleanup_stale(self, age, now=None):
                    return []

            @pytest.fixture(scope="session")
            def logger():
                return logging.getLogger("fixture-graph")

            @pytest.fixture(scope="session")
            def log_dir():
                return None

            @pytest.fixture(scope="session")
            def juju_backend(pytestconfig):
                return RecordingBackend(pytestconfig.rootpath / "events")

            @pytest.fixture(scope="session")
            def kubernetes_client_registry():
                return KubernetesClientRegistry({})

            @pytest.fixture(scope="session")
            def istio_mesh_extension():
                return JujuExtension()

            @pytest.fixture(scope="session")
            def persistence_extension():
                return JujuExtension()

            @pytest.fixture(scope="session")
            def unseal_vault_k8s_extension():
                return JujuExtension()

            @pytest.fixture(scope="session")
            def remote_logging_endpoints_by_controller():
                return {}

            @pytest.fixture(scope="session")
            def register_test_clouds():
                return None

            @pytest.fixture(scope="session")
            def target_controller():
                return "unused-controller"

            @pytest.fixture(scope="session")
            def ubuntu_pro_token():
                return None

            @pytest.fixture(scope="session")
            def cleanup_s3_buckets():
                return True

            @pytest.fixture(scope="session")
            def s3_provider_registry(pytestconfig):
                provider = RecordingProvider(pytestconfig.rootpath / "events")
                return S3ProviderRegistry({"target-cloud": Path("unused")}, lambda _: provider)
            """
        )
    )
    pytester.makepyfile(
        textwrap.dedent(
            """
            from juju import JujuModelHandle
            from s3_storage.resource_registry import S3SessionHandle

            def test_s3_integration_uses_the_real_suite_client(
                juju_client, session_resource_registry, s3_provider_registry
            ):
                for controller in ("controller-one", "controller-two"):
                    juju_client.bootstrap_controller("target-cloud", controller, {}, {})
                    juju_client.deploy_bundle_file(
                        "bundle.yaml", JujuModelHandle(controller=controller, model="model")
                    )

                provider = s3_provider_registry.provider_for_cloud("target-cloud")
                assert len(provider.buckets) == 2
                assert len(session_resource_registry.children_of(provider.handle)) == 2
                assert session_resource_registry.children_of(S3SessionHandle()) == [provider.handle]
            """
        )
    )

    result = pytester.runpytest("-q")
    result.assert_outcomes(passed=1)

    events = (pytester.path / "events").read_text().splitlines()
    assert events[:2] == ["controller:controller-two", "controller:controller-one"]
    assert events[2:] == ["bucket", "bucket"]
