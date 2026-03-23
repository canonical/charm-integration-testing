# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Test Observer API client for querying charm test results and metadata."""

import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)


class TestObserverClientError(Exception):
    """Base exception for Test Observer client errors."""

    pass


class TestObserverAPINotConfiguredError(TestObserverClientError):
    """Raised when Test Observer API is not configured."""

    pass


class TestObserverQueryError(TestObserverClientError):
    """Raised when querying Test Observer API fails."""

    pass


class TestObserverClient:
    """Client for interacting with Test Observer API.

    This client provides methods for querying charm artefacts, builds, and test results.
    """

    def __init__(self, api_url: str | None = None, token: str | None = None) -> None:
        """Initialize the Test Observer client.

        Args:
            api_url: Test Observer API base URL. If not provided, reads from TEST_OBSERVER_API
                    or test_observer_api environment variable.
            token: API token for authentication. If not provided, reads from TEST_OBSERVER_TOKEN
                   or test_observer_token environment variable.

        Raises:
            TestObserverAPINotConfiguredError: If API URL is not configured.
        """
        self.api_url = (
            api_url or os.environ.get("TEST_OBSERVER_API") or os.environ.get("test_observer_api") or ""
        ).rstrip("/")
        self.token = token or os.environ.get("TEST_OBSERVER_TOKEN") or os.environ.get("test_observer_token")

        if not self.api_url:
            raise TestObserverAPINotConfiguredError(
                "TEST_OBSERVER_API is not configured. Set via environment variable or constructor argument."
            )

        self._session = requests.Session()
        self._session.headers.update(self._build_headers())

    def _build_headers(self) -> dict[str, str]:
        """Build request headers for API calls."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _get_json(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any] | list[dict[str, Any]]:
        """Perform GET request and return JSON response.

        Args:
            endpoint: API endpoint path (e.g., "/v1/artefacts/history").
            params: Optional query parameters.

        Returns:
            Parsed JSON response as dict or list.

        Raises:
            TestObserverQueryError: If the request fails.
        """
        url = f"{self.api_url}{endpoint}"
        try:
            response = self._session.get(url, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()

            if not isinstance(payload, (dict, list)):
                raise TestObserverQueryError(
                    f"Unexpected JSON response type from {endpoint}: expected dict or list, got {type(payload).__name__}"
                )
            return payload
        except requests.RequestException as exc:
            raise TestObserverQueryError(f"Failed to query {endpoint}: {exc}") from exc

    @staticmethod
    def _extract_first_list(payload: dict[str, Any] | list[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
        """Extract first matching list from nested payload.

        Args:
            payload: Response payload (dict or list).
            *keys: Possible key names to search for in order.

        Returns:
            List of dict items found in payload.
        """
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _extract_id(item: dict[str, Any] | list[dict[str, Any]], *keys: str) -> int | None:
        """Extract ID from item, trying multiple key names.

        Args:
            item: Item dict or list of items.
            *keys: Possible key names for ID field (e.g., "id", "artifact_id", "artefact_id").

        Returns:
            Extracted ID as int, or None if not found.
        """
        if isinstance(item, list):
            for sub_item in item:
                result = TestObserverClient._extract_id(sub_item, *keys)
                if result is not None:
                    return result
            return None

        for key in keys:
            value = item.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
        return None

    @staticmethod
    def _has_test_passed(test_results_payload: dict[str, Any] | list[dict[str, Any]], test_name: str) -> bool:
        """Check if a specific test passed in results.

        Args:
            test_results_payload: Test results response payload.
            test_name: Name of the test to check (e.g., "test_deploy").

        Returns:
            True if the test passed, False otherwise.
        """
        test_results = TestObserverClient._extract_first_list(test_results_payload, "test_results", "results", "items")
        for result in test_results:
            if result.get("name") == test_name and str(result.get("status", "")).lower() == "passed":
                return True
        return False

    def query_artefacts_history(
        self, stage: str, name: str, track: str, family: str = "charm", limit: int = 10
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Query artefacts history.

        Args:
            stage: Stage name (e.g., "stable", "edge").
            name: Charm name.
            track: Track version (e.g., "14").
            family: Artefact family (default: "charm").
            limit: Maximum number of results (default: 10).

        Returns:
            API response containing artefact history.

        Raises:
            TestObserverQueryError: If the query fails.
        """
        params: dict[str, str | int] = {
            "family": family,
            "limit": limit,
            "offset": 0,
            "stage": stage,
            "name": name,
            "track": track,
        }
        return self._get_json("/v1/artefacts/history", params=params)

    def query_artefact_builds(self, artefact_id: int, limit: int = 100) -> dict[str, Any] | list[dict[str, Any]]:
        """Query builds for a specific artefact.

        Args:
            artefact_id: Artefact ID.
            limit: Maximum number of results (default: 100).

        Returns:
            API response containing builds.

        Raises:
            TestObserverQueryError: If the query fails.
        """
        params: dict[str, str | int] = {"limit": limit, "offset": 0}
        return self._get_json(f"/v1/artefacts/{artefact_id}/builds", params=params)

    def query_test_results_for_execution(self, execution_id: int) -> dict[str, Any] | list[dict[str, Any]]:
        """Query test results for a specific test execution.

        Tries multiple endpoints and query parameters for compatibility with different API versions.

        Args:
            execution_id: Test execution ID.

        Returns:
            API response containing test results.

        Raises:
            TestObserverQueryError: If all query attempts fail.
        """
        # Try direct endpoint first
        try:
            response = self._session.get(
                f"{self.api_url}/v1/test-executions/{execution_id}/test-results",
                timeout=30,
            )
            if response.status_code == 200:
                response.raise_for_status()
                return {"test_results": response.json()}
        except requests.RequestException:
            pass

        # Try alternative endpoints with different query parameters
        errors: list[requests.HTTPError] = []
        for query_key in ("test_execution", "test_execution_id", "test_execution_ids"):
            try:
                params: dict[str, str | int] = {query_key: execution_id, "limit": 200, "offset": 0}
                response = self._session.get(
                    f"{self.api_url}/v1/test-results",
                    params=params,
                    timeout=30,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, (dict, list)):
                    continue
                return payload
            except requests.HTTPError as exc:
                errors.append(exc)
                if exc.response.status_code not in (400, 422):
                    raise TestObserverQueryError(f"Failed to query test results: {exc}") from exc

        raise TestObserverQueryError(
            f"Unable to query test results for execution {execution_id}; "
            f"tried {len(errors)} alternative endpoints, all failed"
        )

    def choose_historical_revision_with_passing_test(
        self, charm_name: str, stage: str, current_revision: int, track: str, test_name: str = "test_deploy"
    ) -> int | None:
        """Find a historical revision with a passing test.

        Queries the Test Observer API to find a previous revision of the charm that has
        a passing test result for the specified test.

        Args:
            charm_name: Name of the charm.
            stage: Release stage (e.g., "stable", "edge").
            current_revision: Current revision to exclude from results.
            track: Release track (e.g., "14").
            test_name: Name of the test to check for passing status (default: "test_deploy").

        Returns:
            The first historical revision with a passing test, or None if none found.

        Raises:
            TestObserverQueryError: If API queries fail.
        """
        logger.debug(
            f"Searching for historical {charm_name} revision in {track}/{stage} "
            f"with passing {test_name} (excluding revision {current_revision})"
        )

        history_payload = self.query_artefacts_history(stage=stage, name=charm_name, track=track)
        artefacts = self._extract_first_list(history_payload, "artefacts", "items", "results", "data")

        logger.debug(f"Found {len(artefacts)} artefacts in history")

        for artefact in artefacts:
            artefact_id = self._extract_id(artefact, "id", "artefact_id", "artifact_id")
            if artefact_id is None:
                logger.debug("Skipping artefact without ID")
                continue

            logger.debug(f"Querying builds for artefact {artefact_id}")
            builds_payload = self.query_artefact_builds(artefact_id=artefact_id)
            builds = self._extract_first_list(builds_payload, "builds", "items", "results", "data")

            logger.debug(f"Found {len(builds)} builds for artefact {artefact_id}")

            for build in builds:
                revision = self._extract_id(build, "revision")
                if revision is None or revision == current_revision:
                    if revision == current_revision:
                        logger.debug(f"Skipping current revision {revision}")
                    else:
                        logger.debug("Skipping build without revision")
                    continue

                executions = build.get("test_executions")
                if not isinstance(executions, list):
                    logger.debug(f"No test executions for revision {revision}")
                    continue

                logger.debug(f"Found {len(executions)} test executions for revision {revision}")

                for execution in executions:
                    if not isinstance(execution, dict):
                        continue

                    execution_id = self._extract_id(execution, "id", "test_execution_id")
                    if execution_id is None:
                        continue

                    logger.debug(f"Querying test results for execution {execution_id}")
                    try:
                        test_results_payload = self.query_test_results_for_execution(execution_id=execution_id)
                        if self._has_test_passed(test_results_payload, test_name):
                            logger.info(
                                f"Found historical revision {revision} with passing {test_name} "
                                f"(execution {execution_id})"
                            )
                            return revision
                    except TestObserverQueryError as exc:
                        logger.warning(f"Failed to query test results for execution {execution_id}: {exc}")
                        continue

        logger.info(f"No historical revision found with passing {test_name}")
        return None

    def choose_historical_revision_with_passing_deploy(
        self, charm_name: str, stage: str, current_revision: int, track: str
    ) -> int | None:
        """Convenience method: find a historical revision with a passing test_deploy.

        Args:
            charm_name: Name of the charm.
            stage: Release stage (e.g., "stable", "edge").
            current_revision: Current revision to exclude from results.
            track: Release track (e.g., "14").

        Returns:
            The first historical revision with a passing test_deploy, or None if none found.
        """
        return self.choose_historical_revision_with_passing_test(
            charm_name=charm_name,
            stage=stage,
            current_revision=current_revision,
            track=track,
            test_name="test_deploy",
        )
