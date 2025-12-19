# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import time
import warnings
from datetime import timedelta

import pytest
from juju import (
    JujuPerformanceWarning,
    JujuStatusPerformanceWarning,
    JujuWaitState,
    JujuWaitTimeoutError,
    warn_performance,
)
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
                label="with_unit_agents",
                wait_state=JujuWaitState(noncompliant_unit_agents={"unit-1": None, "unit-2": None}),
                expected="Timed out while waiting (unit agents: ['unit-1', 'unit-2'])",
            ),
            Params(
                label="with_applications_and_units",
                wait_state=JujuWaitState(
                    noncompliant_applications={"application-1": None}, noncompliant_units={"unit-1": None}
                ),
                expected="Timed out while waiting (applications: ['application-1'], units: ['unit-1'])",
            ),
            Params(
                label="with_insufficient_status_checks",
                wait_state=JujuWaitState(insufficient_status_checks=True),
                expected="Timed out while waiting (insufficient status checks)",
            ),
            Params(
                label="with_everything",
                wait_state=JujuWaitState(
                    message="custom message",
                    insufficient_status_checks=True,
                    noncompliant_applications={"app-1": None},
                    noncompliant_units={"unit-1": None},
                    noncompliant_unit_agents={"unit-2": None},
                ),
                expected="Timed out while custom message (applications: ['app-1'], insufficient status checks, unit agents: ['unit-2'], units: ['unit-1'])",
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


class TestWarnSlow:
    def test_no_warning_when_fast(self):
        # GIVEN a function decorated with warn_performance
        @warn_performance(threshold=timedelta(seconds=1))
        def fast_function():
            return "done"

        # WHEN the function executes quickly
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = fast_function()

            # THEN no warning is issued
            assert len(w) == 0
            assert result == "done"

    def test_warning_when_slow(self):
        # GIVEN a function decorated with warn_performance
        @warn_performance(threshold=timedelta(milliseconds=50))
        def slow_function():
            time.sleep(0.1)  # Sleep for 100ms
            return "done"

        # WHEN the function executes slowly
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = slow_function()

            # THEN a warning is issued
            assert len(w) == 1
            assert issubclass(w[0].category, JujuPerformanceWarning)
            assert "Exceeded threshold" in str(w[0].message)
            assert result == "done"

    def test_custom_warning_category(self):
        # GIVEN a function decorated with warn_performance and a custom category
        @warn_performance(threshold=timedelta(milliseconds=50), category=JujuStatusPerformanceWarning)
        def slow_function():
            time.sleep(0.1)
            return "done"

        # WHEN the function executes slowly
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = slow_function()

            # THEN a warning is issued with the custom category
            assert len(w) == 1
            assert issubclass(w[0].category, JujuStatusPerformanceWarning)
            assert result == "done"

    def test_warning_on_exception(self):
        # GIVEN a function decorated with warn_performance that raises an exception
        @warn_performance(threshold=timedelta(milliseconds=50))
        def error_function():
            time.sleep(0.1)
            raise ValueError("test error")

        # WHEN the function raises an exception after being slow
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            with pytest.raises(ValueError):
                error_function()

            # THEN a warning is still issued
            assert len(w) == 1
            assert issubclass(w[0].category, JujuPerformanceWarning)
