# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass

import pytest
from juju import JujuIntegrationApplication


class TestJujuIntegrationApplication:
    def test_str_representation(self) -> None:
        # GIVEN an application endpoint
        endpoint = JujuIntegrationApplication(application="webapp", endpoint="database")

        # WHEN str is called
        result = str(endpoint)

        # THEN matches expected
        assert result == "webapp:database"

    class TestFromStr:
        @dataclass
        class Params:
            label: str
            input_str: str
            expected_application: str | None = None
            expected_endpoint: str | None = None
            should_raise: bool = False
            error_match: str | None = None

        test_cases = [
            Params(
                label="valid_simple",
                input_str="webapp:database",
                expected_application="webapp",
                expected_endpoint="database",
            ),
            Params(
                label="valid_with_colon_in_endpoint",
                input_str="webapp:db:special",
                expected_application="webapp",
                expected_endpoint="db:special",
            ),
            Params(
                label="invalid_no_colon",
                input_str="webapp",
                should_raise=True,
                error_match="Invalid JujuIntegrationApplication string",
            ),
            Params(
                label="invalid_empty_string",
                input_str="",
                should_raise=True,
                error_match="Invalid JujuIntegrationApplication string",
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            if params.should_raise:
                # WHEN / THEN parsing invalid string raises error
                with pytest.raises(ValueError, match=params.error_match if params.error_match else ""):
                    JujuIntegrationApplication.from_str(params.input_str)
            else:
                # WHEN parsing valid string
                endpoint = JujuIntegrationApplication.from_str(params.input_str)

                # THEN application and endpoint are correctly parsed
                assert endpoint.application == params.expected_application
                assert endpoint.endpoint == params.expected_endpoint
