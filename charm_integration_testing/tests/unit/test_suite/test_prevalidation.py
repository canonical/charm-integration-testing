# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for test_suite/prevalidation.py."""

from dataclasses import dataclass

import pytest
import requests
from test_suite.prevalidation import (
    DependencyStatus,
    check_charmhub_availability,
    check_test_observer_availability,
    format_unavailable_reason,
    unavailable_dependencies,
)


@dataclass
class _FakeResponse:
    status_code: int


class _FakeSession:
    """Stub for ``requests.get`` that returns a canned response or raises."""

    def __init__(self, response: _FakeResponse | None = None, exception: requests.RequestException | None = None):
        self.response = response
        self.exception = exception
        self.calls: list[tuple[str, dict[str, str] | None, float]] = []

    def __call__(self, url: str, headers: dict[str, str] | None = None, timeout: float = 0.0) -> _FakeResponse:
        self.calls.append((url, headers, timeout))
        if self.exception is not None:
            raise self.exception
        assert self.response is not None
        return self.response


class TestCheckCharmhubAvailability:
    def test_reports_available_on_2xx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN Charmhub responds with HTTP 200
        fake_get = _FakeSession(response=_FakeResponse(status_code=200))
        monkeypatch.setattr(requests, "get", fake_get)

        # WHEN checking availability
        status = check_charmhub_availability("https://api.charmhub.io")

        # THEN it is reported as available and the expected endpoint was probed
        assert status.available is True
        assert fake_get.calls[0][0] == "https://api.charmhub.io/v2/charms/info/ubuntu"

    def test_reports_available_on_client_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN Charmhub responds with a 404 (server is up, endpoint/charm irrelevant)
        monkeypatch.setattr(requests, "get", _FakeSession(response=_FakeResponse(status_code=404)))

        # WHEN checking availability
        status = check_charmhub_availability("https://api.charmhub.io")

        # THEN it is still reported as available: the network path is reachable
        assert status.available is True

    def test_reports_unavailable_on_server_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN Charmhub responds with a 503
        monkeypatch.setattr(requests, "get", _FakeSession(response=_FakeResponse(status_code=503)))

        # WHEN checking availability
        status = check_charmhub_availability("https://api.charmhub.io")

        # THEN it is reported as unavailable
        assert status.available is False
        assert "503" in status.detail

    def test_reports_unavailable_on_connection_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN the request raises a connection error
        monkeypatch.setattr(requests, "get", _FakeSession(exception=requests.ConnectionError("no route to host")))

        # WHEN checking availability
        status = check_charmhub_availability("https://api.charmhub.io")

        # THEN it is reported as unavailable
        assert status.available is False
        assert "ConnectionError" in status.detail


class TestCheckTestObserverAvailability:
    def test_sends_bearer_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN Test Observer responds with HTTP 200
        fake_get = _FakeSession(response=_FakeResponse(status_code=200))
        monkeypatch.setattr(requests, "get", fake_get)

        # WHEN checking availability with a token
        status = check_test_observer_availability("https://to.example.com", "secret-token")

        # THEN the token is sent as a bearer header and availability is reported
        assert status.available is True
        assert fake_get.calls[0][1] == {"Authorization": "Bearer secret-token"}

    def test_reports_unavailable_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN the request times out
        monkeypatch.setattr(requests, "get", _FakeSession(exception=requests.Timeout("timed out")))

        # WHEN checking availability
        status = check_test_observer_availability("https://to.example.com", "secret-token")

        # THEN it is reported as unavailable
        assert status.available is False


class TestUnavailableDependencies:
    def test_filters_only_unavailable(self) -> None:
        # GIVEN a mix of available and unavailable dependency statuses
        statuses = [
            DependencyStatus(name="Charmhub", available=True, detail="ok"),
            DependencyStatus(name="Test Observer", available=False, detail="down"),
        ]

        # WHEN filtering for unavailable dependencies
        result = unavailable_dependencies(statuses)

        # THEN only the unavailable one is returned
        assert result == [DependencyStatus(name="Test Observer", available=False, detail="down")]


class TestFormatUnavailableReason:
    def test_includes_name_and_detail(self) -> None:
        # GIVEN one unavailable dependency
        unavailable = [DependencyStatus(name="Charmhub", available=False, detail="503 from server")]

        # WHEN formatting the skip reason
        reason = format_unavailable_reason(unavailable)

        # THEN the reason names the dependency and includes the detail
        assert "Charmhub" in reason
        assert "503 from server" in reason
