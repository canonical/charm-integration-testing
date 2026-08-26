# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

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
