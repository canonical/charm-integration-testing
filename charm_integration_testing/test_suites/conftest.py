# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

import argparse
from typing import get_type_hints

import pytest
import yaml

from charm_integration_testing.juju import JujuClient
from charm_integration_testing.juju_cmd import JujuCmdClient
from charm_integration_testing.serializeable_dataclass import serializeable_dataclass


@pytest.fixture(autouse=True)
def juju_client() -> JujuClient:
    return JujuCmdClient()


def pytest_addoption(parser):
    parser.addoption(
        "--test-cases", type=argparse.FileType("r"), required=True, help="File containing test cases to run"
    )


@serializeable_dataclass
class TestCase:
    test_suite: str
    test_execution_id: str
    test_params: dict


@serializeable_dataclass
class TestCasesInput:
    test_cases: list[TestCase]


def pytest_generate_tests(metafunc: pytest.Metafunc):
    # Ensure fixture
    if "test_params" not in metafunc.fixturenames:
        return
    
    # Load test cases
    with metafunc.config.getoption("test_cases") as test_cases_file:
        test_case_input = TestCasesInput(**yaml.safe_load(test_cases_file))

    # Filter for this test suite
    test_cases = [
        test_case for test_case in test_case_input.test_cases if test_case.test_suite == metafunc.function.__name__
    ]

    # Parameterize test
    test_params_type = get_type_hints(metafunc.function)["test_params"]
    metafunc.parametrize(
        "test_params",
        [test_params_type(**test_case.test_params) for test_case in test_cases],
        ids=[test_case.test_execution_id for test_case in test_cases],
    )
