#!/usr/bin/python3

import requests
import os
from subprocess import run, PIPE
from argparse import ArgumentParser
from time import sleep

BASE_URL = "https://test-observer-api.canonical.com/v1/"

def get_issue(issue_number : int, repo : str) -> int | None:
    # Returns the issue id for this issue from Test Observer
    ENDPOINT = "issues"
    url = BASE_URL + ENDPOINT
    offset = 0
    limit = 50
    params = {"project": repo, "offset": offset, "limit": limit}
    session = requests.Session()
    while response := session.get(url, params=params):
        if response.status_code != 200:
            break

        data = response.json()
        if not data['issues']:
            break
        
        for issue in data['issues']:
            if int(issue['key']) == issue_number:
                return issue['id']
        offset += limit
        params = {"project": repo, "offset": offset, "limit": limit }
    return None

def get_test_result_input(test_observer_issue_id : int) -> dict:
    # Returns the inputs needed to trigger a workflow for a test result
    # that failed with this issue id
    ENDPOINT = "test-results"
    endpoint_url = BASE_URL + ENDPOINT

    params = {
        "issues": test_observer_issue_id,
        "limit": 1
    }

    response = requests.get(endpoint_url, params=params)
    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch test result input from {endpoint_url} "
            f"(status code {response.status_code}): {response.text}"
        )
    data = response.json()
    if not data.get("test_results"):
        raise ValueError(f"No test results found for issue id {test_observer_issue_id}")
    testplan = data['test_results'][0]['test_execution']['test_plan']
    # anatomy of a testplan <test_type>/<charm>:<endpoint>/<interface>/<charm>:<endpoint>
    testplan = testplan.removeprefix('integration/')
    testplan = testplan.split('/')
    target_name, target_endpoint = testplan[0].split(':')
    neighbor_name, neighbor_endpoint = testplan[-1].split(':')
    target = {
        "charm_name" :   target_name,
        "endpoint_name" : target_endpoint
    }
    
    neighbor = {
        "charm_name" :   neighbor_name,
        "endpoint_name" : neighbor_endpoint
    }

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
        "repo",
        type=str,
        help="The repository to run the workflow on, in the style of {owner}/{repo}",
    )

    return parser


def dispatch_run(
    github_token: str,
    target_charm_name: str,
    target_endpoint_name: str,
    neighbor_charm_name: str,
    neighbor_endpoint_name: str,
    ref: str ,
) -> None:
    url = "https://api.github.com/repos/canonical/charm-integration-testing/actions/workflows/charm-testing.yaml/dispatches"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = {
        "ref": ref,
        "inputs": {
            "charm_under_test": target_charm_name,
            "charm_endpoint": target_endpoint_name,
            "neighbor": neighbor_charm_name,
            "neighbor_endpoint": neighbor_endpoint_name,
            "environment": "staging",
        },
    }
    response = requests.post(url, headers=headers, json=data)
    if response.status_code != 204:
        print(f"Failed to dispatch workflow: {response.status_code} - {response.text}")
        exit(1)
    print("Workflow dispatched successfully")

if __name__ == "__main__":
    parser = argument_parser()
    args = parser.parse_args()
    if not args.issue_number:
        print("No issue found")
        exit(0)
    issue_id = get_issue(args.issue_number, args.repo)
    if issue_id is None:
        print(f"No issue found in Test Observer for issue number {args.issue_number}")
        exit(1)

    target, neighbor = get_test_result_input(issue_id)

    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        print("Error: GITHUB_TOKEN environment variable is not set. Please set it before running this script.")
        exit(1)
    print("Workflow inputs:")
    print(f"target_charm_name: {target['charm_name']}")
    print(f"target_endpoint_name: {target['endpoint_name']}")
    print(f"neighbor_charm_name: {neighbor['charm_name']}")
    print(f"neighbor_endpoint_name: {neighbor['endpoint_name']}")
    dispatch_run(
        github_token,
        target['charm_name'],
        target['endpoint_name'],
        neighbor['charm_name'],
        neighbor['endpoint_name'],
        args.ref,
    )