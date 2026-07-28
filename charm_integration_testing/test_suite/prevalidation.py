# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Prevalidation checks for external dependencies (Charmhub, Test Observer, ...).

Before running the test suite we verify that the external systems the suite
depends on are reachable. When one of them is suffering an outage, tests can
fail deep inside a transition test (e.g. ``test_build_bundle`` calling
Charmhub) with a confusing network error, several retries later. Checking
availability once, up front, lets us skip the run with a clear reason instead.

See: https://github.com/canonical/charm-integration-testing/issues/461
"""

import logging
from dataclasses import dataclass

import requests

#: Default timeout, in seconds, for a single dependency reachability probe.
DEFAULT_DEPENDENCY_CHECK_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class DependencyStatus:
    """Result of checking whether an external dependency is reachable."""

    name: str
    available: bool
    detail: str


def _check_reachable(
    name: str,
    url: str,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> DependencyStatus:
    """Perform a lightweight GET request to determine whether ``url`` is reachable.

    Any HTTP response with a status code below 500 is treated as reachable, since the
    goal is to detect network-level or server-side outages, not application-level
    errors such as a missing resource or bad auth. Connection failures, timeouts, and
    5xx responses are treated as unavailable.
    """
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        return DependencyStatus(name=name, available=False, detail=f"{type(exc).__name__}: could not reach {url}")
    if response.status_code >= 500:
        return DependencyStatus(name=name, available=False, detail=f"{url} returned HTTP {response.status_code}")
    return DependencyStatus(name=name, available=True, detail=f"{url} returned HTTP {response.status_code}")


def check_charmhub_availability(
    api_url: str, timeout: float = DEFAULT_DEPENDENCY_CHECK_TIMEOUT_SECONDS
) -> DependencyStatus:
    """Check whether the Charmhub API is reachable.

    Args:
        api_url: Base URL of the Charmhub API (no trailing slash required).
        timeout: Request timeout in seconds.

    Returns:
        A ``DependencyStatus`` describing whether Charmhub responded.
    """
    probe_url = f"{api_url.rstrip('/')}/v2/charms/info/ubuntu"
    return _check_reachable("Charmhub", probe_url, timeout)


def check_test_observer_availability(
    api_url: str, token: str, timeout: float = DEFAULT_DEPENDENCY_CHECK_TIMEOUT_SECONDS
) -> DependencyStatus:
    """Check whether the Test Observer API is reachable.

    Args:
        api_url: Base URL of the Test Observer API.
        token: Bearer token used to authenticate with the API.
        timeout: Request timeout in seconds.

    Returns:
        A ``DependencyStatus`` describing whether Test Observer responded.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else None
    return _check_reachable("Test Observer", api_url.rstrip("/"), timeout, headers=headers)


def unavailable_dependencies(statuses: list[DependencyStatus]) -> list[DependencyStatus]:
    """Filter a list of dependency statuses down to the unavailable ones."""
    return [status for status in statuses if not status.available]


def format_unavailable_reason(unavailable: list[DependencyStatus]) -> str:
    """Build a human-readable ``pytest.skip`` reason from unavailable dependency statuses."""
    details = "; ".join(f"{status.name} ({status.detail})" for status in unavailable)
    return f"Skipping test session: external dependencies are unavailable: {details}"


def log_dependency_statuses(logger: logging.Logger, statuses: list[DependencyStatus]) -> None:
    """Log the outcome of each dependency check at an appropriate level."""
    for status in statuses:
        if status.available:
            logger.debug(f"External dependency available: {status.name}: {status.detail}")
        else:
            logger.error(f"External dependency unavailable: {status.name}: {status.detail}")
