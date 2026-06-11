# Copyright (C) 2026 Canonical Ltd

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from typing import cast
from unittest.mock import AsyncMock, patch

import ops
import pytest

from validators.temporal_host_info.validator import TemporalHostInfoValidator
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
)

# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _make_validator(
    databag: dict[str, str],
    endpoint: str = "temporal-host-info",
    role: RelationRoleStub = RelationRoleStub.requires,
) -> TemporalHostInfoValidator:
    app = ApplicationStub()
    relation = RelationStub(name=endpoint, id=0, app=app, data={app: databag})
    charm = cast(
        ops.CharmBase,
        make_charm_from_relation(relation, interface_name="temporal-host-info", role=role),
    )
    return TemporalHostInfoValidator(charm, cast(ops.Relation, relation))


def _make_validator_no_app(endpoint: str = "temporal-host-info") -> TemporalHostInfoValidator:
    """Factory that produces a validator with no remote application on the relation."""
    relation = RelationStub(name=endpoint, id=0, app=None, data={})
    charm = cast(
        ops.CharmBase,
        make_charm_from_relation(relation, interface_name="temporal-host-info", role=RelationRoleStub.requires),
    )
    return TemporalHostInfoValidator(charm, cast(ops.Relation, relation))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_DATABAG: dict[str, str] = {
    "host": "10.1.2.3",
    "port": "7233",
}

# ---------------------------------------------------------------------------
# Simple-level tests
# ---------------------------------------------------------------------------


class TestTemporalHostInfoValidatorSimple:
    def test_happy_path_pass(self) -> None:
        # GIVEN a complete databag and a reachable Temporal frontend
        validator = _make_validator(VALID_DATABAG)

        with patch.object(TemporalHostInfoValidator, "_probe_system_info", new_callable=AsyncMock):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        assert result.level == "simple"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert schema_check.passed
        port_check = next(c for c in result.checks if c.name == "port_format")
        assert port_check.passed
        gsi_check = next(c for c in result.checks if c.name == "get_system_info")
        assert gsi_check.passed

    def test_fails_when_temporal_api_unreachable(self) -> None:
        # GIVEN a complete databag but the Temporal frontend is not responding
        validator = _make_validator(VALID_DATABAG)

        with patch.object(
            TemporalHostInfoValidator,
            "_probe_system_info",
            new=AsyncMock(side_effect=Exception("connection refused")),
        ):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        gsi_check = next(c for c in result.checks if c.name == "get_system_info")
        assert not gsi_check.passed
        assert "connection refused" in gsi_check.message

    def test_fails_when_host_missing(self) -> None:
        # GIVEN databag is missing host
        validator = _make_validator({"port": "7233"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "host" in schema_check.message

    def test_fails_when_port_missing(self) -> None:
        # GIVEN databag is missing port
        validator = _make_validator({"host": "10.1.2.3"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "port" in schema_check.message

    def test_fails_when_both_fields_missing(self) -> None:
        # GIVEN empty databag
        validator = _make_validator({})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "host" in schema_check.message
        assert "port" in schema_check.message

    def test_fails_when_port_is_not_integer(self) -> None:
        # GIVEN port is a non-integer string
        validator = _make_validator({"host": "10.1.2.3", "port": "not-a-port"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        port_check = next(c for c in result.checks if c.name == "port_format")
        assert not port_check.passed

    def test_fails_when_port_is_out_of_range(self) -> None:
        # GIVEN port is 0 (invalid)
        validator = _make_validator({"host": "10.1.2.3", "port": "0"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        port_check = next(c for c in result.checks if c.name == "port_format")
        assert not port_check.passed

    def test_errors_when_no_remote_app(self) -> None:
        # GIVEN no remote application on the relation
        validator = _make_validator_no_app()

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"
        assert result.error is not None

    def test_skipped_for_unsupported_level(self) -> None:
        # GIVEN a valid databag
        validator = _make_validator(VALID_DATABAG)

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None

    @pytest.mark.parametrize(
        "role,should_skip",
        [
            (RelationRoleStub.requires, False),
            (RelationRoleStub.provides, True),
            (RelationRoleStub.peer, True),
        ],
    )
    def test_skips_non_requires_roles(self, role: RelationRoleStub, should_skip: bool) -> None:
        # GIVEN a validator with the specified role
        validator = _make_validator(VALID_DATABAG, role=role)

        with patch.object(TemporalHostInfoValidator, "_probe_system_info", new_callable=AsyncMock):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert (result.status == "SKIPPED") == should_skip

    def test_sets_endpoint_and_interface_on_result(self) -> None:
        # GIVEN a custom endpoint name
        validator = _make_validator(VALID_DATABAG, endpoint="my-temporal")

        with patch.object(TemporalHostInfoValidator, "_probe_system_info", new_callable=AsyncMock):
            # WHEN
            result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "my-temporal"
        assert result.interface == "temporal-host-info"


# ---------------------------------------------------------------------------
# Deep-level tests
# ---------------------------------------------------------------------------


class TestTemporalHostInfoValidatorDeep:
    def test_skipped_for_deep_level(self) -> None:
        # GIVEN a valid databag — deep is not implemented by this validator
        validator = _make_validator(VALID_DATABAG)

        # WHEN validate() is called directly at deep level
        result = validator.validate(level="deep")

        # THEN the validator itself returns SKIPPED (the runner's level-fallback
        # logic is a separate concern and is not exercised here)
        assert result.status == "SKIPPED"
        assert result.error is not None

    def test_skipped_for_uat_level(self) -> None:
        # GIVEN a valid databag
        validator = _make_validator(VALID_DATABAG)

        # WHEN
        result = validator.validate(level="uat")

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None
