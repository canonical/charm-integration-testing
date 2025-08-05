# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from juju import JujuWaitState, JujuWaitTimeoutError
from pydantic.dataclasses import dataclass


class TestJujuWaitTimeoutError:
    class TestStr:
        @dataclass
        class Params:
            label: str
            wait_state: JujuWaitState | None
            expected: str

        test_cases = [
            Params(
                label="basic",
                wait_state=None,
                expected="Timed out while waiting",
            ),
            Params(
                label="custom_message",
                wait_state=JujuWaitState(message="waiting for thing"),
                expected="Timed out while waiting for thing",
            ),
            Params(
                label="with_applications",
                wait_state=JujuWaitState(noncompliant_applications={"application-1": None, "application-2": None}),
                expected="Timed out while waiting (applications: ['application-1', 'application-2'])",
            ),
            Params(
                label="with_units",
                wait_state=JujuWaitState(noncompliant_units={"unit-1": None, "unit-2": None}),
                expected="Timed out while waiting (units: ['unit-1', 'unit-2'])",
            ),
            Params(
                label="with_applications_and_units",
                wait_state=JujuWaitState(
                    noncompliant_applications={"application-1": None}, noncompliant_units={"unit-1": None}
                ),
                expected="Timed out while waiting (applications: ['application-1'], units: ['unit-1'])",
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params):
            # GIVEN an error of the wait state
            error = JujuWaitTimeoutError(params.wait_state)

            # WHEN converted to a string
            actual = str(error)

            # THEN matches expected
            assert actual == params.expected
