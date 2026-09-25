# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from typing import Any

from requests.adapters import HTTPAdapter
from test_observer_client.client import DEFAULT_RETRY_KWARGS
from test_observer_client.client import TestObserverClient as ObserverClient
from urllib3.util.retry import Retry


class TestClientInit:
    def test_mounts_default_retry_policy_on_session(self) -> None:
        # GIVEN a client constructed without an explicit retries override
        client = ObserverClient(logging.getLogger(__name__), api_url="https://example.com", token="token")

        # WHEN inspecting the adapter mounted for both schemes
        https_adapter = client._session.get_adapter("https://example.com")
        http_adapter = client._session.get_adapter("http://example.com")
        assert isinstance(https_adapter, HTTPAdapter)
        assert isinstance(http_adapter, HTTPAdapter)

        # THEN both are configured per DEFAULT_RETRY_KWARGS
        for retries in (https_adapter.max_retries, http_adapter.max_retries):
            assert isinstance(retries, Retry)
            assert retries.total == DEFAULT_RETRY_KWARGS["total"]
            assert retries.backoff_factor == DEFAULT_RETRY_KWARGS["backoff_factor"]
            assert retries.status_forcelist == DEFAULT_RETRY_KWARGS["status_forcelist"]
            assert retries.allowed_methods == DEFAULT_RETRY_KWARGS["allowed_methods"]
            assert retries.raise_on_status == DEFAULT_RETRY_KWARGS["raise_on_status"]

    def test_each_client_gets_its_own_retries_instance(self) -> None:
        first = ObserverClient(logging.getLogger(__name__), api_url="https://example.com", token="token")
        second = ObserverClient(logging.getLogger(__name__), api_url="https://example.com", token="token")

        first_adapter = first._session.get_adapter("https://example.com")
        second_adapter = second._session.get_adapter("https://example.com")
        assert isinstance(first_adapter, HTTPAdapter)
        assert isinstance(second_adapter, HTTPAdapter)
        assert first_adapter.max_retries is not second_adapter.max_retries

    def test_accepts_an_injected_retry_policy(self) -> None:
        custom_retries = Retry(total=1)

        client = ObserverClient(
            logging.getLogger(__name__), api_url="https://example.com", token="token", retries=custom_retries
        )

        adapter = client._session.get_adapter("https://example.com")
        assert isinstance(adapter, HTTPAdapter)
        assert adapter.max_retries is custom_retries


class TestHistoricalRevisionSelection:
    def test_iter_historical_revisions_with_passing_test_yields_all_matching_revisions(self) -> None:
        # GIVEN a client with multiple historical builds, each with a passing deploy result
        class FakeClient(ObserverClient):
            def query_artefacts_history(
                self, stage: str, name: str, track: str, family: str = "charm", limit: int = 10
            ) -> dict[str, Any]:
                return {"artefacts": [{"id": 42}]}

            def query_artefact_builds(self, artefact_id: int, limit: int = 100) -> dict[str, Any]:
                assert artefact_id == 42
                return {
                    "builds": [
                        {"revision": 12, "test_executions": [{"id": 201}]},
                        {"revision": 11, "test_executions": [{"id": 101}]},
                    ]
                }

            def query_test_results_for_execution(self, execution_id: int) -> dict[str, Any]:
                assert execution_id in {101, 201}
                return {"test_results": [{"name": "test_deploy", "status": "PASSED"}]}

        client = FakeClient(logging.getLogger(__name__), api_url="https://example.com", token="token")

        # WHEN scanning backwards for passing deploys
        revisions = list(
            client.iter_historical_revisions_with_passing_test(
                charm_name="postgresql-k8s",
                stage="stable",
                current_revision=13,
                track="14",
                test_name="test_deploy",
            )
        )

        # THEN all matching revisions are yielded in search order
        assert revisions == [12, 11]

    def test_choose_historical_revision_with_passing_test_returns_first_match(self) -> None:
        # GIVEN a client with a passing historical revision after a failed one
        class FakeClient(ObserverClient):
            def query_artefacts_history(
                self, stage: str, name: str, track: str, family: str = "charm", limit: int = 10
            ) -> dict[str, Any]:
                return {"artefacts": [{"id": 7}]}

            def query_artefact_builds(self, artefact_id: int, limit: int = 100) -> dict[str, Any]:
                assert artefact_id == 7
                return {
                    "builds": [
                        {"revision": 99, "test_executions": [{"id": 401}]},
                        {"revision": 98, "test_executions": [{"id": 301}]},
                    ]
                }

            def query_test_results_for_execution(self, execution_id: int) -> dict[str, Any]:
                if execution_id == 401:
                    return {"test_results": [{"name": "test_deploy", "status": "FAILED"}]}
                return {"test_results": [{"name": "test_deploy", "status": "PASSED"}]}

        client = FakeClient(logging.getLogger(__name__), api_url="https://example.com", token="token")

        # WHEN selecting the first passing revision
        revision = client.choose_historical_revision_with_passing_test(
            charm_name="postgresql-k8s",
            stage="stable",
            current_revision=100,
            track="14",
            test_name="test_deploy",
        )

        # THEN it stops at the first passing result in search order
        assert revision == 98
