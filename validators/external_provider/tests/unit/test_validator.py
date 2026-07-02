# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
from typing import cast

import ops
import pytest

from validators.base import ValidationLevel
from validators.external_provider.validator import ExternalProviderValidator
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import ApplicationStub, RelationRoleStub, RelationStub

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_validator(
    databag: dict[str, str],
    endpoint: str = "kratos-external-idp",
    role: RelationRoleStub = RelationRoleStub.requires,
) -> ExternalProviderValidator:
    app = ApplicationStub()
    relation = RelationStub(name=endpoint, id=0, app=app, data={app: databag})
    charm = make_charm_from_relation(relation, interface_name="external_provider", role=role)
    return ExternalProviderValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))


def _providers_databag(*providers: dict[str, str]) -> dict[str, str]:
    return {"providers": json.dumps(list(providers))}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_GENERIC_PROVIDER: dict[str, str] = {
    "provider": "generic",
    "client_id": "my-client-id",
    "client_secret": "my-client-secret",
    "issuer_url": "https://example.com/.well-known/openid-configuration",
}

VALID_GOOGLE_PROVIDER: dict[str, str] = {
    "provider": "google",
    "client_id": "google-client-id",
    "client_secret": "google-client-secret",
}

VALID_APPLE_PROVIDER: dict[str, str] = {
    "provider": "apple",
    "client_id": "apple-client-id",
    "apple_team_id": "TEAM123",
    "apple_private_key_id": "KEYID456",
    "apple_private_key": "-----BEGIN EC PRIVATE KEY-----\nfake\n-----END EC PRIVATE KEY-----",
}


# ---------------------------------------------------------------------------
# Tests: role and level guards
# ---------------------------------------------------------------------------


class TestExternalProviderValidatorGuards:
    @pytest.mark.parametrize(
        "role,should_skip",
        [
            (RelationRoleStub.requires, False),
            (RelationRoleStub.provides, True),
            (RelationRoleStub.peer, True),
        ],
    )
    def test_returns_skipped_based_on_role(self, role: RelationRoleStub, should_skip: bool) -> None:
        # GIVEN
        validator = _make_validator(_providers_databag(VALID_GENERIC_PROVIDER), role=role)

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert (result.status == "SKIPPED") == should_skip

    @pytest.mark.parametrize("level", ["deep", "uat"])
    def test_returns_skipped_for_unsupported_levels(self, level: ValidationLevel) -> None:
        # GIVEN
        validator = _make_validator(_providers_databag(VALID_GENERIC_PROVIDER))

        # WHEN
        result = validator.validate(level=level)

        # THEN
        assert result.status == "SKIPPED"
        assert result.error is not None
        assert "not supported" in result.error

    def test_returns_error_when_relation_app_not_in_scope(self) -> None:
        # GIVEN a relation whose remote app has not yet joined
        app = ApplicationStub()
        relation = RelationStub(name="kratos-external-idp", id=0, app=app)
        relation.data = {}  # simulate app not yet in scope
        charm = make_charm_from_relation(relation, interface_name="external_provider")
        validator = ExternalProviderValidator(cast(ops.CharmBase, charm), cast(ops.Relation, relation))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "ERROR"


# ---------------------------------------------------------------------------
# Tests: simple level — schema
# ---------------------------------------------------------------------------


class TestExternalProviderValidatorSchema:
    def test_fails_when_providers_key_missing(self) -> None:
        # GIVEN a databag with no 'providers' key
        validator = _make_validator({})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        schema_check = next(c for c in result.checks if c.name == "schema")
        assert not schema_check.passed
        assert "providers" in schema_check.message

    def test_fails_when_providers_is_invalid_json(self) -> None:
        # GIVEN a databag with malformed JSON
        validator = _make_validator({"providers": "not-valid-json"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        json_check = next(c for c in result.checks if c.name == "providers_json")
        assert not json_check.passed

    def test_fails_when_providers_is_json_object_not_array(self) -> None:
        # GIVEN providers is a JSON object instead of an array
        validator = _make_validator({"providers": '{"provider": "google"}'})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        json_check = next(c for c in result.checks if c.name == "providers_json")
        assert not json_check.passed
        assert "array" in json_check.message

    def test_fails_when_providers_array_is_empty(self) -> None:
        # GIVEN providers is a valid JSON empty array
        validator = _make_validator({"providers": "[]"})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        non_empty_check = next(c for c in result.checks if c.name == "providers_non_empty")
        assert not non_empty_check.passed


# ---------------------------------------------------------------------------
# Tests: simple level — provider field checks
# ---------------------------------------------------------------------------


class TestExternalProviderValidatorFields:
    def test_passes_for_valid_generic_provider(self) -> None:
        # GIVEN a complete generic provider databag
        validator = _make_validator(_providers_databag(VALID_GENERIC_PROVIDER))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        fields_check = next(c for c in result.checks if c.name == "provider_fields")
        assert fields_check.passed

    def test_passes_for_valid_google_provider(self) -> None:
        # GIVEN a complete google provider databag
        validator = _make_validator(_providers_databag(VALID_GOOGLE_PROVIDER))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"

    def test_passes_for_valid_apple_provider_without_client_secret(self) -> None:
        # GIVEN apple provider which uses asymmetric keys, not client_secret
        validator = _make_validator(_providers_databag(VALID_APPLE_PROVIDER))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "PASS"
        fields_check = next(c for c in result.checks if c.name == "provider_fields")
        assert fields_check.passed

    def test_fails_when_provider_name_missing(self) -> None:
        # GIVEN a provider entry without the 'provider' field
        bad = {**VALID_GENERIC_PROVIDER}
        del bad["provider"]
        validator = _make_validator(_providers_databag(bad))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        fields_check = next(c for c in result.checks if c.name == "provider_fields")
        assert not fields_check.passed
        assert "provider" in fields_check.message

    def test_fails_when_client_id_missing(self) -> None:
        # GIVEN a provider entry without 'client_id'
        bad = {**VALID_GENERIC_PROVIDER}
        del bad["client_id"]
        validator = _make_validator(_providers_databag(bad))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        fields_check = next(c for c in result.checks if c.name == "provider_fields")
        assert not fields_check.passed
        assert "client_id" in fields_check.message

    def test_fails_when_client_secret_missing_for_non_apple_provider(self) -> None:
        # GIVEN a google provider without client_secret
        bad = {k: v for k, v in VALID_GOOGLE_PROVIDER.items() if k != "client_secret"}
        validator = _make_validator(_providers_databag(bad))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        fields_check = next(c for c in result.checks if c.name == "provider_fields")
        assert not fields_check.passed
        assert "client_secret" in fields_check.message

    def test_fails_when_provider_type_is_unknown(self) -> None:
        # GIVEN a provider entry with an unrecognised provider type
        bad = {**VALID_GENERIC_PROVIDER, "provider": "unknown-idp"}
        validator = _make_validator(_providers_databag(bad))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        fields_check = next(c for c in result.checks if c.name == "provider_fields")
        assert not fields_check.passed
        assert "unknown-idp" in fields_check.message

    def test_reports_issues_for_multiple_providers(self) -> None:
        # GIVEN two provider entries, both missing client_id
        bad1 = {**VALID_GENERIC_PROVIDER, "client_id": ""}
        bad2 = {**VALID_GOOGLE_PROVIDER, "client_id": ""}
        validator = _make_validator(_providers_databag(bad1, bad2))

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        fields_check = next(c for c in result.checks if c.name == "provider_fields")
        assert not fields_check.passed
        assert "providers[0]" in fields_check.message
        assert "providers[1]" in fields_check.message

    def test_sets_endpoint_and_interface_on_result(self) -> None:
        # GIVEN a custom endpoint name
        validator = _make_validator(_providers_databag(VALID_GENERIC_PROVIDER), endpoint="my-idp")

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.endpoint == "my-idp"
        assert result.interface == "external_provider"

    def test_fails_gracefully_when_provider_entry_is_not_a_dict(self) -> None:
        # GIVEN providers array contains a non-object entry (e.g. a string)
        validator = _make_validator({"providers": '["not-an-object"]'})

        # WHEN
        result = validator.validate(level="simple")

        # THEN
        assert result.status == "FAIL"
        fields_check = next(c for c in result.checks if c.name == "provider_fields")
        assert not fields_check.passed
        assert "providers[0]" in fields_check.message
