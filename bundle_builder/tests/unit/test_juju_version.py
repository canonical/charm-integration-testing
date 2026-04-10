# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.


import pytest
from pydantic.dataclasses import dataclass

from bundle_builder.juju_version import JujuVersion


class TestJujuVersionFromString:
    @dataclass
    class Params:
        label: str
        value: str
        expected: JujuVersion | None = None
        raises: bool = False

    test_cases = [
        Params(
            label="major_only",
            value="4",
            expected=JujuVersion(major=4, minor=0, patch=0),
        ),
        Params(
            label="major_minor",
            value="3.6",
            expected=JujuVersion(major=3, minor=6, patch=0),
        ),
        Params(
            label="major_minor_patch",
            value="3.6.21",
            expected=JujuVersion(major=3, minor=6, patch=21),
        ),
        Params(
            label="prerelease_suffix_ignored",
            value="3.6-beta2",
            expected=JujuVersion(major=3, minor=6, patch=0),
        ),
        Params(
            label="patch_with_build_metadata",
            value="4.0.6-d20c9e8",
            expected=JujuVersion(major=4, minor=0, patch=6),
        ),
        Params(
            label="juju_version_cli_output",
            value="3.6.21-genericlinux-amd64",
            expected=JujuVersion(major=3, minor=6, patch=21),
        ),
        Params(
            label="invalid_string_raises",
            value="not-a-version",
            raises=True,
        ),
        Params(
            label="empty_string_raises",
            value="",
            raises=True,
        ),
        Params(
            label="channel_string_raises",
            value="3/stable",
            raises=True,
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
    def test(self, params: Params) -> None:
        if params.raises:
            with pytest.raises(ValueError):
                JujuVersion.parse(params.value)
        else:
            assert JujuVersion.parse(params.value) == params.expected


class TestJujuVersionStr:
    @dataclass
    class Params:
        label: str
        version: JujuVersion
        expected: str

    test_cases = [
        Params(label="full_version", version=JujuVersion(major=3, minor=6, patch=21), expected="3.6.21"),
        Params(label="zero_patch", version=JujuVersion(major=4, minor=0, patch=0), expected="4.0.0"),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
    def test(self, params: Params) -> None:
        # GIVEN a JujuVersion
        version = params.version

        # WHEN str is called
        result = str(version)

        # THEN matches expected
        assert result == params.expected


class TestJujuVersionComparison:
    @dataclass
    class Params:
        label: str
        left: JujuVersion
        right: JujuVersion
        lt: bool
        le: bool
        gt: bool
        ge: bool
        eq: bool

    test_cases = [
        Params(
            label="equal_versions",
            left=JujuVersion.parse("3.6.21"),
            right=JujuVersion.parse("3.6.21"),
            lt=False,
            le=True,
            gt=False,
            ge=True,
            eq=True,
        ),
        Params(
            label="older_major",
            left=JujuVersion.parse("3.0.0"),
            right=JujuVersion.parse("4.0.0"),
            lt=True,
            le=True,
            gt=False,
            ge=False,
            eq=False,
        ),
        Params(
            label="older_minor",
            left=JujuVersion.parse("3.5.0"),
            right=JujuVersion.parse("3.6.0"),
            lt=True,
            le=True,
            gt=False,
            ge=False,
            eq=False,
        ),
        Params(
            label="older_patch",
            left=JujuVersion.parse("3.6.1"),
            right=JujuVersion.parse("3.6.21"),
            lt=True,
            le=True,
            gt=False,
            ge=False,
            eq=False,
        ),
        Params(
            label="major_only_vs_full",
            left=JujuVersion.parse("3"),
            right=JujuVersion.parse("3.6.21"),
            lt=True,
            le=True,
            gt=False,
            ge=False,
            eq=False,
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
    def test(self, params: Params) -> None:
        # GIVEN two JujuVersions
        left = params.left
        right = params.right

        # THEN comparisons match expected
        assert (left < right) == params.lt
        assert (left <= right) == params.le
        assert (left > right) == params.gt
        assert (left >= right) == params.ge
        assert (left == right) == params.eq
