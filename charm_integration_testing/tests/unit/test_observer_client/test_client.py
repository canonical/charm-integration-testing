# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

import pytest
from requests.adapters import HTTPAdapter
from test_observer_client.client import DEFAULT_RETRY_KWARGS
from test_observer_client.client import TestObserverClient as ObserverClient
from test_observer_client.client import TestObserverQueryError as ObserverQueryError
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


class TestChooseHistoricalRevision:
    @staticmethod
    def _client() -> ObserverClient:
        return ObserverClient(logging.getLogger(__name__), api_url="https://example.com", token="token")

    @staticmethod
    def _stub_history_and_builds(client: ObserverClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(client, "query_artefacts_history", lambda **_: {"artefacts": [{"id": 1}]})
        monkeypatch.setattr(
            client,
            "query_artefact_builds",
            lambda **_: {"builds": [{"revision": 298, "test_executions": [{"id": 555}]}]},
        )

    def test_raises_when_all_result_queries_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN history/builds resolve but every result query fails (e.g. a Test Observer outage)
        client = self._client()
        self._stub_history_and_builds(client, monkeypatch)

        def _raise(**_: object) -> dict[str, object]:
            raise ObserverQueryError("results endpoint down")

        monkeypatch.setattr(client, "query_test_results_for_execution", _raise)

        # THEN the outage is surfaced instead of being reported as "no historical revision"
        with pytest.raises(ObserverQueryError):
            client.choose_historical_revision_with_passing_test(
                charm_name="traefik-k8s", stage="stable", current_revision=378, track="latest"
            )

    def test_returns_none_when_no_passing_and_no_query_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN result queries succeed but none report a passing test
        client = self._client()
        self._stub_history_and_builds(client, monkeypatch)
        monkeypatch.setattr(client, "query_test_results_for_execution", lambda **_: {"test_results": []})
        monkeypatch.setattr(client, "_has_test_passed", lambda *_: False)

        # THEN None is returned to signal a definitive "no passing revision"
        result = client.choose_historical_revision_with_passing_test(
            charm_name="traefik-k8s", stage="stable", current_revision=378, track="latest"
        )
        assert result is None

    def test_returns_none_when_some_queries_fail_but_one_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN one result query fails transiently but another succeeds with no passing test
        client = self._client()
        monkeypatch.setattr(client, "query_artefacts_history", lambda **_: {"artefacts": [{"id": 1}]})
        monkeypatch.setattr(
            client,
            "query_artefact_builds",
            lambda **_: {"builds": [{"revision": 298, "test_executions": [{"id": 555}, {"id": 556}]}]},
        )

        def _partial(**kwargs: object) -> dict[str, object]:
            if kwargs["execution_id"] == 555:
                raise ObserverQueryError("transient failure")
            return {"test_results": []}

        monkeypatch.setattr(client, "query_test_results_for_execution", _partial)
        monkeypatch.setattr(client, "_has_test_passed", lambda *_: False)

        # THEN a partial failure with at least one success is not treated as an outage
        result = client.choose_historical_revision_with_passing_test(
            charm_name="traefik-k8s", stage="stable", current_revision=378, track="latest"
        )
        assert result is None
