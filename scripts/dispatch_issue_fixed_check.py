#!/usr/bin/python3

import os
from argparse import ArgumentParser
from typing import NamedTuple

import requests
from requests.adapters import HTTPAdapter
from urllib3 import Retry

BASE_URL = os.getenv("TEST_OBSERVER_BASE_URL", "https://test-observer-api.canonical.com/v1/")
RETRY = Retry(
    total=5,
    backoff_factor=1,
    status_forcelist=[500, 502, 503, 504],
)
ADAPTER = HTTPAdapter(max_retries=RETRY)


class CharmEndpoint(NamedTuple):
    charm_name: str
    endpoint_name: str


def get_test_observer_issue_id(issue_number: int, project: str) -> int | None:
    """Returns the issue id for this issue from Test Observer"""
    endpoint = "issues"
    url = BASE_URL + endpoint
    session = requests.Session()
    session.mount("https://", ADAPTER)

    offset = 0
    limit = 50

    while True:
        params = {"project": project, "offset": offset, "limit": limit}
        response = session.get(url, params=params)
        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(f"Failed to fetch issues from Test Observer: {e}") from e
        data = response.json()
        issues = data.get("issues", [])
        if not issues:
            break

        for issue in issues:
            if int(issue["key"]) == issue_number:
                return issue["id"]

        if len(issues) < limit:
            break

        offset += limit
    return None


def get_test_result_input(test_observer_issue_id: int) -> tuple[CharmEndpoint, CharmEndpoint]:
    """Returns the inputs needed to trigger a workflow for a test result
    that failed with this issue id.

    The returned tuple is (target, neighbor), where the first element is the
    charm under test (target) and the second element is the neighbor charm.
    """
    endpoint = "test-results"
    endpoint_url = BASE_URL + endpoint

    params = {"issues": test_observer_issue_id, "limit": 1}
    session = requests.Session()
    session.mount("https://", ADAPTER)
    response = session.get(endpoint_url, params=params)
    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        raise RuntimeError(f"Failed to fetch test results from Test Observer: {e}") from e

    data = response.json()
    if not data.get("test_results"):
        raise ValueError(f"No test results found for issue id {test_observer_issue_id}")
    testplan = data["test_results"][0]["test_execution"]["test_plan"]
    # anatomy of a testplan: <test_type>/<charm>:<endpoint>/<interface>/<charm>:<endpoint>
    testplan = testplan.removeprefix("integration/")
    testplan = testplan.split("/")
    target_name, target_endpoint = testplan[0].split(":")
    neighbor_name, neighbor_endpoint = testplan[-1].split(":")
    target = CharmEndpoint(charm_name=target_name, endpoint_name=target_endpoint)
    neighbor = CharmEndpoint(charm_name=neighbor_name, endpoint_name=neighbor_endpoint)

    return (target, neighbor)


def argument_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description="Get workflow inputs for a test result that failed with a given issue number",
    )
    parser.add_argument(
        "issue_number",
        type=int,
        help="The issue number on GitHub",
    )
    parser.add_argument(
        "ref",
        type=str,
        help="The git ref (branch or tag) to run the workflow on",
    )
    parser.add_argument(
        "--project",
        type=str,
        help="The repository the issue is associated with in Test Observer, in the style of {owner}/{repo}",
        default="canonical/charm-integration-testing",
        required=False,
    )

    parser.add_argument(
        "--environment",
        type=str,
        help="The environment to run the workflow in",
        choices=["staging", "production"],
        default="staging",
        required=False,
    )

    return parser


def dispatch_run(
    github_token: str,
    workflow_url: str,
    workflow_inputs: dict,
    ref: str,
) -> None:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = {
        "ref": ref,
        "inputs": workflow_inputs,
    }

    session = requests.Session()
    session.mount("https://", ADAPTER)
    response = session.post(workflow_url, headers=headers, json=data)
    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        raise RuntimeError(f"Failed to dispatch workflow: {e}") from e
    print("Workflow dispatched successfully")


if __name__ == "__main__":
    DISPATCH_WORKFLOW_URL = "https://api.github.com/repos/canonical/charm-integration-testing/actions/workflows/charm-testing.yaml/dispatches"
    parser = argument_parser()
    args = parser.parse_args()
    try:
        issue_id = get_test_observer_issue_id(args.issue_number, args.project)

        if issue_id is None:
            print(f"No issue found in Test Observer for issue number {args.issue_number}")
            exit(1)

        target, neighbor = get_test_result_input(issue_id)

        github_token = os.getenv("GITHUB_TOKEN")
        if not github_token:
            print(
                "Error: GITHUB_TOKEN environment variable is not set.\n"
                "Set it in your shell, for example:\n"
                "  export GITHUB_TOKEN=your_token_here\n"
                "If you are running this in GitHub Actions, define GITHUB_TOKEN (or a PAT) as a repository/organization secret\n"
                "and reference it in your workflow (see https://docs.github.com/actions/security-guides/encrypted-secrets)."
            )
            exit(1)

        data = {
            "charm_under_test": target.charm_name,
            "charm_endpoint": target.endpoint_name,
            "neighbor": neighbor.charm_name,
            "neighbor_endpoint": neighbor.endpoint_name,
            "environment": args.environment,
        }

        print(f"Workflow inputs: {data}")

        dispatch_run(
            github_token=github_token,
            workflow_url=DISPATCH_WORKFLOW_URL,
            workflow_inputs=data,
            ref=args.ref,
        )
    except Exception as e:
        print(f"Error: {e}")
        exit(1)
