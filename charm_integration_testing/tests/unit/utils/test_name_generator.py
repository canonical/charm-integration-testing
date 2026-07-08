# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for name_generator module."""

import pytest
from pydantic.dataclasses import dataclass
from utils.name_generator import generate_juju_name


class TestGenerateJujuNameFormat:
    @dataclass
    class Params:
        label: str
        prefix: str
        expected_start: str

    test_cases = [
        Params(
            label="default_prefix",
            prefix="charmqa",
            expected_start="charmqa-",
        ),
        Params(
            label="custom_prefix",
            prefix="mytest",
            expected_start="mytest-",
        ),
        Params(
            label="ci_style_prefix",
            prefix="charmqa-12345678",
            expected_start="charmqa-12345678-",
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
    def test_prefix(self, params: Params) -> None:
        """Generated name starts with the given prefix followed by a hyphen."""
        name = generate_juju_name(params.prefix)
        assert name.startswith(params.expected_start)

    @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
    def test_suffix_is_digits_only(self, params: Params) -> None:
        """Random suffix contains only digits."""
        name = generate_juju_name(params.prefix)
        suffix = name[len(params.expected_start) :]
        assert suffix.isdigit()

    @pytest.mark.parametrize("params", test_cases, ids=[p.label for p in test_cases])
    def test_suffix_length(self, params: Params) -> None:
        """Random suffix is exactly 8 characters."""
        name = generate_juju_name(params.prefix)
        suffix = name[len(params.expected_start) :]
        assert len(suffix) == 8


class TestGenerateJujuNameUniqueness:
    def test_uniqueness(self) -> None:
        """Repeated calls produce different names."""
        names = {generate_juju_name() for _ in range(20)}
        assert len(names) > 1
