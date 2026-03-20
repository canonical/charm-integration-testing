# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import requests
import yaml
from juju import JujuClient

from .scheduler.states import State


def _build_headers(token: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _extract_first_list(payload: dict[str, Any] | list[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _extract_id(item: dict[str, Any] | list[dict[str, Any]], *keys: str) -> int | None:
    if isinstance(item, list):
        for sub_item in item:
            result = _extract_id(sub_item, *keys)
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


def _extract_track_and_stage(channel: str | None) -> tuple[str, str]:
    if not channel:
        return "14", "stable"
    # Channels are usually track/risk, e.g. "latest/edge".
    return channel.split("/")[0], channel.split("/")[1]


def _json_object(response: requests.Response, endpoint: str) -> dict[str, Any] | list[dict[str, Any]]:
    """Return response JSON payload as an object and fail fast on unexpected shapes."""
    payload = response.json()
    if not (isinstance(payload, dict) or isinstance(payload, list)):
        raise RuntimeError(f"Unexpected JSON payload from {endpoint}: expected object or list")
    return payload


def query_artefacts_history(
    api_url: str, token: str | None, stage: str, name: str, track: str
) -> dict[str, Any] | list[dict[str, Any]]:
    """Query Test Observer artefacts history endpoint for the current model/bundle context."""
    params: dict[str, str | int] = {
        "family": "charm",
        "limit": 10,
        "offset": 0,
        "stage": stage,
        "name": name,
        "track": track,
    }
    headers = _build_headers(token)

    response = requests.get(
        f"{api_url.rstrip('/')}/v1/artefacts/history",
        params=params,
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    return _json_object(response, "/v1/artefacts/history")


def query_artefact_builds(api_url: str, token: str | None, artefact_id: int) -> dict[str, Any] | list[dict[str, Any]]:
    headers = _build_headers(token)
    params: dict[str, int] = {"limit": 100, "offset": 0}
    response = requests.get(
        f"{api_url.rstrip('/')}/v1/artefacts/{artefact_id}/builds",
        params=params,
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    return _json_object(response, "/v1/artefacts/{artefact_id}/builds")


def query_test_results_for_execution(
    api_url: str, token: str | None, execution_id: int
) -> dict[str, Any] | list[dict[str, Any]]:
    headers = _build_headers(token)

    # Preferred endpoint in newer Test Observer API versions.
    direct_response = requests.get(
        f"{api_url.rstrip('/')}/v1/test-executions/{execution_id}/test-results",
        headers=headers,
        timeout=30,
    )
    if direct_response.status_code == 200:
        return {"test_results": direct_response.json()}

    # Different deployments may expose slightly different query parameter names.
    errors: list[requests.HTTPError] = []
    for query_key in ("test_execution", "test_execution_id", "test_execution_ids"):
        response = requests.get(
            f"{api_url.rstrip('/')}/v1/test-results",
            params={query_key: execution_id, "limit": 200, "offset": 0},
            headers=headers,
            timeout=30,
        )
        try:
            response.raise_for_status()
            return _json_object(response, "/v1/test-results")
        except requests.HTTPError as exc:
            errors.append(exc)
            if response.status_code not in (400, 422):
                raise

    raise RuntimeError(
        "Unable to query /v1/test-results for test execution"
        f"{execution_id}; all supported query parameters failed: {errors}"
    )


def has_test_deploy_passed(test_results_payload: dict[str, Any] | list[dict[str, Any]]) -> bool:
    test_results = _extract_first_list(test_results_payload, "test_results", "results", "items")
    for result in test_results:
        name = result.get("name")
        status = str(result.get("status", "")).lower()
        if name == "test_deploy" and status == "passed":
            return True
    return False


def choose_historical_revision_with_passing_deploy(
    api_url: str,
    token: str | None,
    charm_name: str,
    stage: str,
    current_revision: int,
    track: str,
) -> int | None:
    history_payload = query_artefacts_history(api_url=api_url, token=token, stage=stage, name=charm_name, track=track)
    artefacts = _extract_first_list(history_payload, "artefacts", "items", "results", "data")

    for artefact in artefacts:
        artefact_id = _extract_id(artefact, "id", "artefact_id", "artifact_id")
        if artefact_id is None:
            continue

        builds_payload = query_artefact_builds(api_url=api_url, token=token, artefact_id=artefact_id)
        builds = _extract_first_list(builds_payload, "builds", "items", "results", "data")

        for build in builds:
            revision = _extract_id(build, "revision")
            if revision is None:
                continue
            if revision == current_revision:
                continue

            executions = build.get("test_executions")
            if not isinstance(executions, list):
                continue

            for execution in executions:
                if not isinstance(execution, dict):
                    continue
                execution_id = _extract_id(execution, "id", "test_execution_id")
                if execution_id is None:
                    continue

                test_results_payload = query_test_results_for_execution(
                    api_url=api_url,
                    token=token,
                    execution_id=execution_id,
                )
                if has_test_deploy_passed(test_results_payload):
                    return revision

    return None


def create_bundle_with_revision_override(
    source_bundle: Path,
    destination_bundle: Path,
    target_application: str,
    target_revision: int,
) -> None:
    with source_bundle.open("r", encoding="utf-8") as file:
        bundle_data = yaml.safe_load(file)

    if not isinstance(bundle_data, dict):
        raise ValueError(f"Invalid bundle file: {source_bundle}")

    applications = bundle_data.get("applications")
    if not isinstance(applications, dict) or target_application not in applications:
        raise ValueError(f"Application '{target_application}' not found in bundle: {source_bundle}")

    target_application_data = applications[target_application]
    if not isinstance(target_application_data, dict):
        raise ValueError(f"Invalid application definition for '{target_application}' in {source_bundle}")

    target_application_data["revision"] = target_revision

    with destination_bundle.open("w", encoding="utf-8") as file:
        yaml.safe_dump(bundle_data, file, sort_keys=False)


@pytest.mark.state(requires=State.DEPLOYED, provides=State.OLD_REVISION)
def test_deploy(
    juju_client: JujuClient,
    model: str,
    bundle: Path,
    target_application: str,
    target_charm: str,
    target_channel: str | None,
    target_revision: int | None,
    tmp_path: Path,
) -> None:
    test_observer_api = os.getenv("TEST_OBSERVER_API") or os.getenv("test_observer_api")
    test_observer_token = os.getenv("TEST_OBSERVER_TOKEN") or os.getenv("test_observer_token")

    if not test_observer_api:
        pytest.skip("TEST_OBSERVER_API is required to query artefacts history")

    if target_revision is None:
        pytest.fail("--target-revision must be provided as an integer for this test.")

    # Extract stage and track from --target-channel passed by charm-testing.yaml.
    track, stage = _extract_track_and_stage(target_channel)

    # Get all applications from the model
    apps = list(juju_client.list_applications(model=model).keys())

    # Remove all applications from the model
    if apps:
        juju_client.remove_applications(*apps, model=model)
        juju_client.wait_for_removal(*apps, model=model, timeout=timedelta(minutes=15))

    # Query Test Observer for historical revisions and pick the first one with a passing test_deploy run.
    selected_revision = choose_historical_revision_with_passing_deploy(
        api_url=test_observer_api,
        token=test_observer_token,
        charm_name=target_charm,
        stage=stage,
        current_revision=target_revision,
        track=track,
    )
    if selected_revision is None:
        pytest.fail(
            "Unable to find a historical revision with a passing test_deploy result "
            f"for charm '{target_charm}' in stage '{stage}'."
        )

    juju_client.logger.info(
        f"Selected historical revision {selected_revision} for {target_application} ({target_charm})"
    )

    overridden_bundle = tmp_path / f"bundle-{target_application}-rev-{selected_revision}.yaml"
    create_bundle_with_revision_override(
        source_bundle=bundle,
        destination_bundle=overridden_bundle,
        target_application=target_application,
        target_revision=selected_revision,
    )

    # Deploy the original bundle with only the target app revision overridden.
    juju_client.deploy_bundle_file(str(overridden_bundle), model=model)

    # Wait until idle
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))

    # Validate all applications and relations
    juju_client.validate_model(model=model, level="simple")
