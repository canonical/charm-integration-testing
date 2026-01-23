#!/bin/python3

import requests
import os
from subprocess import run, PIPE
from argparse import ArgumentParser
from time import sleep

BASE_URL = "https://test-observer-api.canonical.com/v1/"
PROJECT = "canonical/charm-integration-testing"

def get_issue(issue_number : int) -> int | None:
    # Returns the issue id for this issue from Test Observer
    ENDPOINT = "issues"
    url = BASE_URL + ENDPOINT
    offset = 0
    limit = 50
    params = {"project": PROJECT, "offset": offset, "limit": limit}
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
        params = {"project": PROJECT, "offset": offset, "limit": limit }
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
    data = response.json()
    testplan = data['test_results'][0]['test_execution']['test_plan']
    print(testplan)
    #anatomy of a testplan <test_type>/<charm>:<endpoint>/<interface>/<charm>:<endpoint>
    testplan = testplan.removeprefix('integration/')
    testplan = testplan.split('/')
    print(testplan)
    target = {
        "charm_name" :   testplan[0].split(':')[0],
        "endpoint_name" : testplan[0].split(':')[1]
    }
    neighbor = {
        "charm_name" :   testplan[-1].split(':')[0],
        "endpoint_name" : testplan[-1].split(':')[1]
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

    issue_id = get_issue(args.issue_number)
    if issue_id is None:
        print(f"No issue found in Test Observer for issue number {args.issue_number}")
        exit(1)

    target, neighbor = get_test_result_input(issue_id)

    github_token = os.getenv("GITHUB_TOKEN")
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