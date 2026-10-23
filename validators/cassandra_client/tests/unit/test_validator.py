# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import patch

import ops
import pytest
from cassandra import InvalidRequest
from pydantic import ValidationError

from validators.base import PersistenceNotApplicable, PersistenceState
from validators.cassandra_client.validator import CassandraClientPersistenceValidator
from validators.test_utils.helpers import make_charm_from_relation, make_charm_from_relation_and_secrets
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
    UnitStub,
)

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

# Arbitrary non-empty token used by checkpoint() tests; prepare() generates a random one per run.
TEST_TOKEN = "test-token-abc123"


def _make_persistence_validator(
    databag: dict[str, str],
    endpoint: str = "db",
    role: RelationRoleStub = RelationRoleStub.requires,
    relation_id: int = 0,
    model_uuid: str = "11111111-1111-1111-1111-111111111111",
    unit_name: str = "app/0",
) -> CassandraClientPersistenceValidator:
    app = ApplicationStub()
    relation = RelationStub(name=endpoint, id=relation_id, app=app, data={app: databag})
    charm = cast(
        ops.CharmBase,
        make_charm_from_relation(
            relation,
            interface_name="cassandra",
            role=role,
            local_model_uuid=model_uuid,
            local_unit_name=unit_name,
        ),
    )
    return CassandraClientPersistenceValidator(charm, cast(ops.Relation, relation))


def _make_legacy_persistence_validator(
    unit_databags: dict[str, dict[str, str]],
    endpoint: str = "db",
    role: RelationRoleStub = RelationRoleStub.requires,
    relation_id: int = 0,
    model_uuid: str = "11111111-1111-1111-1111-111111111111",
    local_unit_name: str = "app/0",
) -> CassandraClientPersistenceValidator:
    """Build a validator against a legacy, unit-scoped relation: the remote application sets no
    app-scoped data at all (matching the real "cassandra" charm), and each entry in
    *unit_databags* (keyed by unit name) becomes that remote unit's own databag."""
    app = ApplicationStub()
    units = [UnitStub(name) for name in unit_databags]
    data: dict[Any, dict[str, str]] = {app: {}}
    data.update({unit: unit_databags[unit.name] for unit in units})
    relation = RelationStub(name=endpoint, id=relation_id, app=app, data=data, units=frozenset(units))
    charm = cast(
        ops.CharmBase,
        make_charm_from_relation(
            relation,
            interface_name="cassandra",
            role=role,
            local_model_uuid=model_uuid,
            local_unit_name=local_unit_name,
        ),
    )
    return CassandraClientPersistenceValidator(charm, cast(ops.Relation, relation))


@dataclass
class HostStub:
    """Minimal stub for cassandra.pool.Host; only "is_up" and "datacenter" matter here."""

    is_up: bool | None = True
    datacenter: str | None = "datacenter1"


@dataclass
class MetadataStub:
    """Minimal stub for cassandra.metadata.Metadata; only all_hosts() is used here."""

    hosts: list[HostStub] = field(default_factory=lambda: [HostStub()])

    def all_hosts(self) -> list[HostStub]:
        return self.hosts


@dataclass
class ClusterStub:
    """Minimal stub for cassandra.cluster.Cluster; tracks whether shutdown() was called."""

    metadata: MetadataStub = field(default_factory=MetadataStub)
    shutdown_called: bool = field(default=False, init=False)

    def shutdown(self) -> None:
        self.shutdown_called = True


@dataclass
class RowsStub:
    """Minimal stub for a cassandra ResultSet: supports one() and iteration."""

    rows: list[tuple[Any, ...]] = field(default_factory=list)

    def one(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def __iter__(self) -> Any:
        return iter(self.rows)


@dataclass
class SessionStub:
    """Minimal stub for cassandra.cluster.Session.

    ``execute_results`` supplies one RowsStub per successive execute() call (an empty RowsStub()
    is used once results are exhausted); ``execute_error``, if set, is raised on every call.
    """

    cluster: ClusterStub = field(default_factory=ClusterStub)
    default_consistency_level: Any = None
    execute_results: list[RowsStub] = field(default_factory=list)
    execute_error: Exception | None = None
    executed_queries: list[str] = field(default_factory=list, init=False)
    executed_params: list[Any] = field(default_factory=list, init=False)
    _result_index: int = field(default=0, init=False)

    def execute(self, query: str, params: Any = None) -> RowsStub:
        self.executed_queries.append(query)
        self.executed_params.append(params)
        if self.execute_error is not None:
            raise self.execute_error
        if self._result_index < len(self.execute_results):
            result = self.execute_results[self._result_index]
            self._result_index += 1
            return result
        return RowsStub([])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_DATABAG: dict[str, str] = {
    "endpoints": "10.1.2.3:9042",
    "database": "myks",
    "username": "myuser",
    "password": "mypassword",
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCassandraClientPersistenceValidatorRole:
    @pytest.mark.parametrize(
        "role",
        [RelationRoleStub.provides, RelationRoleStub.peer],
    )
    def test_prepare_raises_not_applicable_for_non_requires_role(self, role: RelationRoleStub) -> None:
        # GIVEN a validator on the non-requires side of the relation
        validator = _make_persistence_validator(VALID_DATABAG, role=role)

        # WHEN / THEN
        with pytest.raises(PersistenceNotApplicable):
            validator.prepare()

    def test_checkpoint_raises_not_applicable_for_non_requires_role(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG, role=RelationRoleStub.provides)

        # WHEN / THEN
        with pytest.raises(PersistenceNotApplicable):
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=1))

    def test_cleanup_raises_not_applicable_for_non_requires_role(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG, role=RelationRoleStub.provides)

        # WHEN / THEN
        with pytest.raises(PersistenceNotApplicable):
            validator.cleanup()


class TestCassandraClientPersistenceValidatorConnection:
    def test_prepare_raises_when_endpoints_is_blank(self) -> None:
        # GIVEN a databag with a present but blank "endpoints" field.
        databag = {**VALID_DATABAG, "endpoints": ""}
        validator = _make_persistence_validator(databag)

        # WHEN / THEN
        with pytest.raises(RuntimeError, match="endpoints"):
            validator.prepare()

    def test_checkpoint_raises_when_endpoints_is_missing(self) -> None:
        # GIVEN a databag missing the "endpoints" field entirely
        databag = {k: v for k, v in VALID_DATABAG.items() if k != "endpoints"}
        validator = _make_persistence_validator(databag)

        # WHEN / THEN
        with pytest.raises(RuntimeError, match="endpoints"):
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=1))

    def test_prepare_raises_when_every_endpoint_entry_is_blank_after_split(self) -> None:
        # GIVEN a non-blank "endpoints" whose entries are all blank once stripped.
        # Regression test for: this previously reached Cluster(contact_points=[]), which the
        # driver only rejects once it tries (and fails) to connect, rather than failing loudly here.
        databag = {**VALID_DATABAG, "endpoints": " , ,"}
        validator = _make_persistence_validator(databag)

        # WHEN / THEN
        with pytest.raises(RuntimeError, match="contact points"):
            validator.prepare()

    def test_prepare_rejects_modern_relation_missing_database_instead_of_using_a_dedicated_keyspace(
        self,
    ) -> None:
        # GIVEN a modern, app-scoped relation (has "endpoints") that is missing "database" - a
        # misconfigured provider, not the legacy interface. Regression test for: this used to
        # silently fall back to a validator-owned dedicated keyspace instead of the provider's
        # intended one, which could mask a real provider misconfiguration as a false PASS.
        databag = {k: v for k, v in VALID_DATABAG.items() if k != "database"}
        validator = _make_persistence_validator(databag)

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            # WHEN / THEN
            with pytest.raises(RuntimeError, match="database"):
                validator.prepare()

        # THEN no connection was even attempted, let alone a dedicated keyspace created.
        mock_cluster_cls.assert_not_called()

    def test_parses_a_bracketed_ipv6_endpoint(self) -> None:
        # GIVEN a modern "endpoints" entry using a bracketed IPv6 literal, as produced by any
        # provider deployed on an IPv6-only network. Regression test for: partition(":") split
        # "[::1]:9042" on the first colon (inside the address itself), leaving host="[" and
        # port_str="1]:9042", which raised ValueError in int() before any connection was
        # attempted.
        databag = {**VALID_DATABAG, "endpoints": "[::1]:9042"}
        validator = _make_persistence_validator(databag)
        session = SessionStub()

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            validator.prepare()

        # THEN the driver receives a bare (unbracketed) IPv6 contact point and the right port.
        _, kwargs = mock_cluster_cls.call_args
        assert kwargs["contact_points"] == ["::1"]
        assert kwargs["port"] == 9042

    def test_parses_a_bracketed_ipv6_endpoint_alongside_a_plain_hostname(self) -> None:
        # GIVEN a mix of a bracketed IPv6 contact point and a plain hostname, sharing one port.
        databag = {**VALID_DATABAG, "endpoints": "cassandra-0.example:9042,[2001:db8::1]:9042"}
        validator = _make_persistence_validator(databag)
        session = SessionStub()

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            validator.prepare()

        # THEN
        _, kwargs = mock_cluster_cls.call_args
        assert kwargs["contact_points"] == ["cassandra-0.example", "2001:db8::1"]
        assert kwargs["port"] == 9042

    def test_rejects_endpoints_with_inconsistent_ports(self) -> None:
        # GIVEN two "endpoints" entries that disagree on their port. Regression test for: the
        # port from the last entry that specified one silently won, so this validator could
        # connect to - and validate - a different service than the one the provider actually
        # advertised. Cluster only accepts one shared port, so this must be rejected outright.
        databag = {**VALID_DATABAG, "endpoints": "node-a:9042,node-b:9142"}
        validator = _make_persistence_validator(databag)

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            # WHEN / THEN
            with pytest.raises(RuntimeError, match="inconsistent ports"):
                validator.prepare()

        # THEN no connection was even attempted.
        mock_cluster_cls.assert_not_called()

    def test_rejects_an_endpoint_with_a_malformed_empty_port(self) -> None:
        # GIVEN an "endpoints" entry with a trailing colon but no port digits at all.
        databag = {**VALID_DATABAG, "endpoints": "node-a:"}
        validator = _make_persistence_validator(databag)

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            # WHEN / THEN
            with pytest.raises(RuntimeError, match="malformed, empty port"):
                validator.prepare()

        # THEN no connection was even attempted.
        mock_cluster_cls.assert_not_called()

    def test_rejects_an_endpoint_with_a_non_numeric_port(self) -> None:
        # GIVEN an "endpoints" entry whose port isn't a number.
        databag = {**VALID_DATABAG, "endpoints": "node-a:native"}
        validator = _make_persistence_validator(databag)

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            # WHEN / THEN
            with pytest.raises(RuntimeError, match="malformed, non-numeric port"):
                validator.prepare()

        # THEN no connection was even attempted.
        mock_cluster_cls.assert_not_called()

    def test_shuts_down_the_cluster_when_the_initial_connect_fails(self) -> None:
        # GIVEN Cluster.connect() raises (e.g. the service is still restarting). Regression test
        # for: this previously left the newly constructed cluster running - connect() raising
        # means no Session is ever returned, so none of the callers' `finally:
        # session.cluster.shutdown()` blocks can run, leaking the cluster's driver resources on
        # every such failure.
        validator = _make_persistence_validator(VALID_DATABAG)

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.side_effect = RuntimeError("connection refused")
            # WHEN / THEN
            with pytest.raises(RuntimeError, match="connection refused"):
                validator.prepare()

        # THEN the cluster that failed to connect was still shut down.
        mock_cluster_cls.return_value.shutdown.assert_called_once()


class TestCassandraClientPersistenceValidatorPrepare:
    def test_creates_canary_table_and_returns_state(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG)
        session = SessionStub()

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            state = validator.prepare()

        # THEN
        assert isinstance(state, PersistenceState)
        assert state.ref == 1
        assert state.token
        queries = " ".join(session.executed_queries)
        assert f'"myks"."canary_da7d88bc9ad4_{state.id:019d}"' in queries
        assert "CREATE TABLE IF NOT EXISTS" in queries
        assert "INSERT INTO" in queries
        # The token is written as the row's marker, so checkpoint() can match on it later
        insert_params = session.executed_params[-1]
        assert insert_params[0] == state.token
        # The connection is torn down after use.
        assert session.cluster.shutdown_called

    def test_generates_distinct_identifiers_across_calls(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG)

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = SessionStub()
            # WHEN
            first = validator.prepare()
            second = validator.prepare()

        # THEN
        assert first.id != second.id
        assert first.token != second.token


class TestCassandraClientPersistenceValidatorCheckpoint:
    def test_passes_when_row_count_matches_expected_ref(self) -> None:
        # GIVEN the canary table has exactly the expected number of rows with matching identity
        validator = _make_persistence_validator(VALID_DATABAG)
        session = SessionStub(execute_results=[RowsStub([(2,)])])

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=2))

        # THEN
        assert result.status == "PASS"
        check = next(c for c in result.checks if c.name == "row_count")
        assert check.passed
        assert new_state.id == 42
        assert new_state.ref == 3
        assert new_state.token == TEST_TOKEN
        # A new row is still written to continue the chain
        assert any("INSERT INTO" in q for q in session.executed_queries)

    def test_fails_when_row_count_is_lower_than_expected(self) -> None:
        # GIVEN data loss: fewer matching rows than expected
        validator = _make_persistence_validator(VALID_DATABAG)
        session = SessionStub(execute_results=[RowsStub([(1,)])])

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=7, ref=3))

        # THEN
        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "row_count")
        assert not check.passed
        assert "3" in check.message and "1" in check.message
        # Regression test for: checkpoint() previously wrote a new marker row and advanced `ref`
        # even on FAIL, but ValidatorRunner only carries the returned state forward on PASS, so the
        # actual row count would drift past what a later checkpoint could compare against. On FAIL
        # the returned state must match `expected` unchanged and no INSERT should be issued.
        assert new_state == PersistenceState(token=TEST_TOKEN, id=7, ref=3)
        assert not any("INSERT INTO" in q for q in session.executed_queries)

    def test_fails_when_table_was_dropped_and_recreated_empty(self) -> None:
        # GIVEN a table dropped and recreated from scratch: it starts empty, so the identity-scoped
        # row count for the original (random, unguessable) token is zero - Cassandra has no
        # auto-incrementing row identity to additionally corroborate this (unlike PostgreSQL's
        # SERIAL id), so the token alone is relied on here.
        validator = _make_persistence_validator(VALID_DATABAG)
        session = SessionStub(execute_results=[RowsStub([(0,)])])

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=42, ref=2))

        # THEN
        assert result.status == "FAIL"
        assert new_state == PersistenceState(token=TEST_TOKEN, id=42, ref=2)
        assert not any("INSERT INTO" in q for q in session.executed_queries)

    def test_fails_when_table_is_not_found(self) -> None:
        # GIVEN the canary table doesn't exist (e.g. it was dropped, or prepare() never ran)
        validator = _make_persistence_validator(VALID_DATABAG)
        session = SessionStub(execute_error=InvalidRequest("unconfigured table"))

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            result, new_state = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=7, ref=1))

        # THEN
        assert result.status == "FAIL"
        check = next(c for c in result.checks if c.name == "row_count")
        assert "0" in check.message
        assert new_state == PersistenceState(token=TEST_TOKEN, id=7, ref=1)
        assert not any("INSERT INTO" in q for q in session.executed_queries)

    def test_uses_canary_table_name_from_expected_identifier(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG)
        session = SessionStub(execute_results=[RowsStub([(1,)])])

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=99, ref=1))

        # THEN
        assert any('"myks"."canary_da7d88bc9ad4_{:019d}"'.format(99) in q for q in session.executed_queries)

    def test_filters_row_count_by_token_and_checkpoint_ref_range(self) -> None:
        # GIVEN
        # Regression test for: checkpoint() must count only rows tagged with the per-run token,
        # not every row in the table - a table recreated from scratch with an unrelated but
        # equally-sized set of rows would otherwise still pass. It must also constrain
        # checkpoint_ref to the expected [1, expected.ref] range: without this, rows replaced or
        # corrupted with the same marker but different checkpoint_ref values could still satisfy a
        # plain count and report a false PASS.
        validator = _make_persistence_validator(VALID_DATABAG)
        session = SessionStub(execute_results=[RowsStub([(1,)])])

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=99, ref=1))

        # THEN the row count query and the follow-up insert are both scoped to the same token,
        # which is random per prepare() run so it cannot be reproduced by a recreated table, and
        # the count is additionally range-bound on checkpoint_ref
        select_index = next(i for i, q in enumerate(session.executed_queries) if "COUNT(*)" in q)
        insert_index = next(i for i, q in enumerate(session.executed_queries) if "INSERT INTO" in q)
        assert (
            "WHERE marker = %s AND checkpoint_ref >= 1 AND checkpoint_ref <= %s"
            in session.executed_queries[select_index]
        )
        assert session.executed_params[select_index] == (TEST_TOKEN, 1)
        assert session.executed_params[insert_index][0] == TEST_TOKEN

    def test_rejects_state_without_a_token(self) -> None:
        # GIVEN a state serialised before the token existed (or otherwise restored/malformed)
        # Regression test for: matching on an empty marker would count rows carrying no token at
        # all, silently degrading the identity check instead of failing safely. The base protocol
        # rejects such a state at construction, so it can never reach checkpoint().
        with pytest.raises(ValidationError):
            PersistenceState(id=1, ref=1)

    def test_result_endpoint_and_interface_are_set(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG, endpoint="my-db")
        session = SessionStub(execute_results=[RowsStub([(1,)])])

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            result, _ = validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=1))

        # THEN
        assert result.endpoint == "my-db"
        assert result.interface == "cassandra"
        assert result.level == "deep"

    def test_raises_when_expected_identifier_is_out_of_range(self) -> None:
        # GIVEN a restored/malformed PersistenceState with an out-of-range id.
        validator = _make_persistence_validator(VALID_DATABAG)
        out_of_range_id = 1 << 63  # one past _MAX_CANARY_IDENTIFIER

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = SessionStub()
            # WHEN / THEN
            with pytest.raises(ValueError, match="out of range"):
                validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=out_of_range_id, ref=1))

    def test_raises_when_expected_identifier_is_negative(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG)

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = SessionStub()
            # WHEN / THEN
            with pytest.raises(ValueError, match="out of range"):
                validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=-1, ref=1))

    def test_raises_when_expected_ref_is_zero(self) -> None:
        # GIVEN a restored/malformed PersistenceState with ref=0 (prepare() always returns ref=1).
        # Regression test for: an empty/never-prepared table (actual == 0) satisfied
        # `actual == expected.ref` and reported a false PASS.
        validator = _make_persistence_validator(VALID_DATABAG)

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = SessionStub()
            # WHEN / THEN
            with pytest.raises(ValueError, match="out of range"):
                validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=0))

    def test_raises_when_expected_ref_is_negative(self) -> None:
        # GIVEN
        validator = _make_persistence_validator(VALID_DATABAG)

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = SessionStub()
            # WHEN / THEN
            with pytest.raises(ValueError, match="out of range"):
                validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=1, ref=-1))


class TestCassandraClientPersistenceValidatorCleanup:
    def test_drops_all_discovered_canary_tables(self) -> None:
        # GIVEN two leftover canary tables are discovered in this relation's keyspace
        validator = _make_persistence_validator(VALID_DATABAG)
        table_1 = "canary_da7d88bc9ad4_" + f"{1:019d}"
        table_2 = "canary_da7d88bc9ad4_" + f"{2:019d}"
        session = SessionStub(execute_results=[RowsStub([(table_1,), (table_2,)])])

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            validator.cleanup()

        # THEN
        drop_queries = [q for q in session.executed_queries if "DROP TABLE" in q]
        assert any(table_1 in q for q in drop_queries)
        assert any(table_2 in q for q in drop_queries)
        # THEN dropped identifiers are safely quoted and keyspace-qualified, so a same-named
        # table in another keyspace can't be targeted instead.
        assert all('"myks"."canary_' in q for q in drop_queries)

    def test_scopes_discovery_to_this_relations_keyspace(self) -> None:
        # Regression test for: discovery must be scoped to this relation's own keyspace, not
        # search across every keyspace in the cluster.
        validator = _make_persistence_validator(VALID_DATABAG)
        session = SessionStub(execute_results=[RowsStub([])])

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            validator.cleanup()

        # THEN
        select_query = next(q for q in session.executed_queries if "system_schema.tables" in q)
        assert "keyspace_name = %s" in select_query
        select_index = session.executed_queries.index(select_query)
        assert session.executed_params[select_index] == ("myks",)

    def test_scopes_discovery_to_this_relations_id(self) -> None:
        # Regression test for: matching the bare `canary_` prefix shared by every relation let
        # cleanup drop a concurrent relation's canary tables. The pattern must be scoped to a
        # token derived from this validator's own model+relation_id+unit.
        validator = _make_persistence_validator(VALID_DATABAG, relation_id=7)
        table_from_other_relation = "canary_da7d88bc9ad4_" + f"{1:019d}"
        session = SessionStub(execute_results=[RowsStub([(table_from_other_relation,)])])

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            validator.cleanup()

        # THEN the table discovered for relation_id=0's scope token is not dropped under relation_id=7's scope
        assert not any("DROP TABLE" in q for q in session.executed_queries)

    def test_scopes_discovery_to_this_unit(self) -> None:
        # Regression test for: the runner runs persistence validators on every unit of an
        # application, so two units share model.uuid and relation_id while owning separate canary
        # tables; cleanup for one unit must not drop another unit's canary table.
        validator = _make_persistence_validator(VALID_DATABAG, unit_name="app/1")
        table_from_other_unit = "canary_da7d88bc9ad4_" + f"{1:019d}"
        session = SessionStub(execute_results=[RowsStub([(table_from_other_unit,)])])

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            validator.cleanup()

        # THEN
        assert not any("DROP TABLE" in q for q in session.executed_queries)

    def test_rejects_discovered_tables_that_only_share_the_prefix(self) -> None:
        # GIVEN a hand-created table sharing the canary prefix but not its fixed-width shape
        validator = _make_persistence_validator(VALID_DATABAG)
        backup_table = "canary_da7d88bc9ad4_backup"
        session = SessionStub(execute_results=[RowsStub([(backup_table,)])])

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            validator.cleanup()

        # THEN the unrelated table is not dropped
        assert not any("DROP TABLE" in q for q in session.executed_queries)

    def test_rejects_discovered_tables_with_an_out_of_range_identifier(self) -> None:
        # GIVEN a same-shaped table name whose identifier exceeds what prepare() could produce
        validator = _make_persistence_validator(VALID_DATABAG)
        out_of_range_table = "canary_da7d88bc9ad4_" + "9999999999999999999"  # 19 nines > _MAX_CANARY_IDENTIFIER
        session = SessionStub(execute_results=[RowsStub([(out_of_range_table,)])])

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            validator.cleanup()

        # THEN
        assert not any("DROP TABLE" in q for q in session.executed_queries)

    def test_noop_when_no_credentials_present(self) -> None:
        # GIVEN a databag without any credential fields (e.g. relation already gone)
        validator = _make_persistence_validator({})

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            # WHEN / THEN
            with pytest.raises(PersistenceNotApplicable):
                validator.cleanup()

        # THEN no connection was attempted
        mock_cluster_cls.assert_not_called()

    def test_noop_when_endpoints_present_but_other_required_fields_are_missing(self) -> None:
        # GIVEN a relation with "endpoints" but not yet database/username/password (still mid-setup).
        validator = _make_persistence_validator({"endpoints": "10.1.2.3:9042"})

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            # WHEN / THEN
            with pytest.raises(PersistenceNotApplicable):
                validator.cleanup()

        # THEN no connection was attempted
        mock_cluster_cls.assert_not_called()

    def test_raises_not_applicable_when_modern_relation_is_missing_database(self) -> None:
        # GIVEN a modern, app-scoped relation (has "endpoints" and full credentials) that is
        # missing "database" - mirrors the prepare()/checkpoint() guard: cleanup must not guess
        # at, or drop, a validator-owned keyspace it never created for a misconfigured provider.
        databag = {k: v for k, v in VALID_DATABAG.items() if k != "database"}
        validator = _make_persistence_validator(databag)

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            # WHEN / THEN
            with pytest.raises(PersistenceNotApplicable):
                validator.cleanup()

        # THEN no connection was attempted
        mock_cluster_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Legacy, unit-scoped "cassandra" interface (the only charm currently published for it; see
# CassandraClientPersistenceValidator's class docstring). Unlike the modern tests above - which
# put "endpoints"/"database"/"username"/"password" in the remote application's own databag - these
# tests put only "host"/"native_transport_port"/"username"/"password" on each remote *unit's* own
# databag, with no app-scoped data at all, matching what the real charm actually publishes.
# ---------------------------------------------------------------------------

LEGACY_UNIT_DATABAG: dict[str, str] = {
    "host": "10.1.2.3",
    "native_transport_port": "9042",
    "username": "myuser",
    "password": "mypassword",
}


class TestCassandraClientPersistenceValidatorLegacyInterface:
    def test_prepare_uses_host_and_native_transport_port_as_contact_point(self) -> None:
        # GIVEN a relation with no app-scoped data, only a single legacy unit databag
        validator = _make_legacy_persistence_validator({"cassandra/0": LEGACY_UNIT_DATABAG})
        session = SessionStub()

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            state = validator.prepare()

        # THEN the single legacy unit's host/port were used as the contact point
        assert mock_cluster_cls.call_args.kwargs["contact_points"] == ["10.1.2.3"]
        assert mock_cluster_cls.call_args.kwargs["port"] == 9042
        assert isinstance(state, PersistenceState)
        assert state.ref == 1

    def test_prepare_creates_and_uses_a_dedicated_keyspace_when_no_database_field_exists(self) -> None:
        # GIVEN a legacy relation with no "database" (keyspace) field anywhere
        validator = _make_legacy_persistence_validator({"cassandra/0": LEGACY_UNIT_DATABAG})
        session = SessionStub()

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            validator.prepare()

        # THEN a dedicated keyspace was created before the canary table, and every subsequent
        # query is qualified against that same keyspace rather than any charm-provided one.
        queries = session.executed_queries
        assert "CREATE KEYSPACE IF NOT EXISTS" in queries[0]
        assert all('"canary_da7d88bc9ad4"' in q for q in queries if "canary_" in q.split(".")[0])

    def test_never_merges_fields_from_different_units(self) -> None:
        # GIVEN two legacy units, one with a complete databag and one deliberately incomplete -
        # a naive merge of the two could produce a usable-looking hybrid that isn't real.
        complete = {**LEGACY_UNIT_DATABAG, "host": "10.9.9.9"}
        incomplete = {"host": "10.1.1.1", "native_transport_port": "9042"}  # no username/password
        validator = _make_legacy_persistence_validator({"cassandra/0": incomplete, "cassandra/1": complete})
        session = SessionStub()

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            validator.prepare()

        # THEN the complete unit's own host was used in full, not a hybrid of the two
        assert mock_cluster_cls.call_args.kwargs["contact_points"] == ["10.9.9.9"]

    def test_falls_back_to_lowest_sorted_unit_when_none_are_complete(self) -> None:
        # GIVEN two legacy units, neither with a complete databag
        validator = _make_legacy_persistence_validator(
            {
                "cassandra/1": {"host": "10.2.2.2"},
                "cassandra/0": {"host": "10.1.1.1"},
            }
        )

        # WHEN / THEN a deterministic schema failure is surfaced (missing username/password),
        # rather than silently reaching the driver with an incomplete/merged credential set.
        with pytest.raises(RuntimeError, match="username"):
            validator.prepare()

    def test_cleanup_drops_the_dedicated_keyspace_when_no_database_field_exists(self) -> None:
        # GIVEN
        validator = _make_legacy_persistence_validator({"cassandra/0": LEGACY_UNIT_DATABAG})
        session = SessionStub()

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            validator.cleanup()

        # THEN the whole dedicated keyspace is dropped outright, rather than hunting for
        # individual tables inside a keyspace this validator doesn't actually own.
        assert any("DROP KEYSPACE IF EXISTS" in q and '"canary_da7d88bc9ad4"' in q for q in session.executed_queries)

    def test_cleanup_is_a_noop_when_legacy_fields_are_incomplete(self) -> None:
        # GIVEN a legacy unit databag missing credentials (e.g. relation still mid-setup)
        validator = _make_legacy_persistence_validator({"cassandra/0": {"host": "10.1.2.3"}})

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            # WHEN / THEN
            with pytest.raises(PersistenceNotApplicable):
                validator.cleanup()

        # THEN no connection was attempted
        mock_cluster_cls.assert_not_called()

    def test_resolves_username_and_password_from_a_juju_secret_on_the_unit_databag(self) -> None:
        # GIVEN a legacy unit databag that publishes its credentials as a Juju secret URI
        # (secret-user) instead of plaintext username/password fields. Regression test for: this
        # previously read "username"/"password" directly off the unit databag, so a secret URI
        # would be passed to the driver as the literal credential instead of being resolved.
        app = ApplicationStub()
        unit = UnitStub("cassandra/0")
        secret_uri = "secret:cassandra-creds"
        unit_databag = {
            "host": "10.1.2.3",
            "native_transport_port": "9042",
            "secret-user": secret_uri,
        }
        relation = RelationStub(name="db", id=0, app=app, data={app: {}, unit: unit_databag}, units=frozenset({unit}))
        charm = cast(
            ops.CharmBase,
            make_charm_from_relation_and_secrets(
                relation, {secret_uri: {"username": "myuser", "password": "mypassword"}}
            ),
        )
        validator = CassandraClientPersistenceValidator(charm, cast(ops.Relation, relation))
        session = SessionStub()

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            validator.prepare()

        # THEN the secret was resolved to the real username/password rather than passing the URI
        # itself to the driver.
        auth_provider = mock_cluster_cls.call_args.kwargs["auth_provider"]
        assert auth_provider.username == "myuser"

    def test_treats_a_blank_endpoints_field_as_modern_and_does_not_fall_back_to_unit_databags(self) -> None:
        # GIVEN a relation that publishes an "endpoints" key with a blank value (e.g. mid
        # relation-changed hook ordering) while a related unit also happens to publish legacy
        # host/credentials fields. Regression test for: the modern/legacy discriminator used to
        # check `data.get("endpoints")` truthiness, so a blank "endpoints" value fell through to
        # the unit-databag scan below and could silently treat a modern, app-scoped relation as
        # the unit-scoped legacy interface - bypassing the missing-"database"-keyspace guard and
        # risking a false PASS against a validator-owned keyspace instead of the real one.
        app = ApplicationStub()
        unit = UnitStub("cassandra/0")
        app_databag = {"endpoints": "", "username": "modernuser", "password": "modernpass"}
        unit_databag = dict(LEGACY_UNIT_DATABAG)
        relation = RelationStub(
            name="db", id=0, app=app, data={app: app_databag, unit: unit_databag}, units=frozenset({unit})
        )
        charm = cast(ops.CharmBase, make_charm_from_relation(relation))
        validator = CassandraClientPersistenceValidator(charm, cast(ops.Relation, relation))

        # WHEN / THEN the modern path is taken (blank "endpoints" key still counts as modern), so
        # the missing "database" keyspace guard fires instead of silently using the legacy unit
        # databag's host/credentials.
        with pytest.raises(RuntimeError, match="no 'database' keyspace"):
            validator.prepare()

    def test_shuts_down_the_cluster_when_dedicated_keyspace_creation_fails(self) -> None:
        # GIVEN a legacy relation whose credentials can connect but cannot create the dedicated
        # canary keyspace (e.g. insufficient permissions, or schema propagation failure).
        # Regression test for: this exception used to escape _open_session() before the
        # prepare()/checkpoint()/cleanup() finally blocks could run (they never receive a
        # session to close), leaking the cluster's connection on every such failure.
        validator = _make_legacy_persistence_validator({"cassandra/0": LEGACY_UNIT_DATABAG})
        session = SessionStub(execute_error=RuntimeError("keyspace creation not permitted"))

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN / THEN
            with pytest.raises(RuntimeError, match="keyspace creation not permitted"):
                validator.prepare()

        # THEN the cluster was shut down before the exception propagated
        assert session.cluster.shutdown_called


class TestCassandraClientPersistenceValidatorKeyspaceReplication:
    def test_uses_replication_factor_one_for_a_single_node_cluster(self) -> None:
        # GIVEN a legacy relation (requires a validator-owned keyspace) backed by a single node
        validator = _make_legacy_persistence_validator({"cassandra/0": LEGACY_UNIT_DATABAG})
        session = SessionStub(cluster=ClusterStub(metadata=MetadataStub(hosts=[HostStub()])))

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            validator.prepare()

        # THEN
        create_keyspace_query = next(q for q in session.executed_queries if "CREATE KEYSPACE" in q)
        assert "'class': 'NetworkTopologyStrategy'" in create_keyspace_query
        assert "'datacenter1': 1" in create_keyspace_query

    def test_scales_replication_factor_to_cluster_size_capped_at_three(self) -> None:
        # GIVEN a legacy relation backed by a 5-node cluster. Regression test for: a fixed
        # replication factor of 1 would mean losing the single node owning the canary's only
        # replica reports data loss caused by this validator's own keyspace, even where a
        # properly replicated application keyspace would have survived losing that same node.
        validator = _make_legacy_persistence_validator({"cassandra/0": LEGACY_UNIT_DATABAG})
        five_nodes = MetadataStub(hosts=[HostStub() for _ in range(5)])
        session = SessionStub(cluster=ClusterStub(metadata=five_nodes))

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            validator.prepare()

        # THEN the factor is capped at 3, not scaled all the way up to 5
        create_keyspace_query = next(q for q in session.executed_queries if "CREATE KEYSPACE" in q)
        assert "'datacenter1': 3" in create_keyspace_query

    def test_includes_down_hosts_towards_the_replication_factor(self) -> None:
        # GIVEN a 3-node cluster where one node is currently (possibly transiently) down.
        # Regression test for: this keyspace is only ever created once (`IF NOT EXISTS`), so
        # excluding a transiently-down host from the factor at creation time would permanently
        # under-replicate it relative to the real cluster size.
        validator = _make_legacy_persistence_validator({"cassandra/0": LEGACY_UNIT_DATABAG})
        mixed_hosts = MetadataStub(hosts=[HostStub(is_up=True), HostStub(is_up=True), HostStub(is_up=False)])
        session = SessionStub(cluster=ClusterStub(metadata=mixed_hosts))

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            validator.prepare()

        # THEN all 3 nodes count toward the factor, regardless of current up/down state
        create_keyspace_query = next(q for q in session.executed_queries if "CREATE KEYSPACE" in q)
        assert "'datacenter1': 3" in create_keyspace_query

    def test_sizes_replication_per_datacenter_for_a_multi_dc_cluster(self) -> None:
        # GIVEN a cluster spanning two datacenters with different node counts
        validator = _make_legacy_persistence_validator({"cassandra/0": LEGACY_UNIT_DATABAG})
        multi_dc_hosts = MetadataStub(
            hosts=[
                HostStub(datacenter="dc1"),
                HostStub(datacenter="dc1"),
                HostStub(datacenter="dc2"),
            ]
        )
        session = SessionStub(cluster=ClusterStub(metadata=multi_dc_hosts))

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            validator.prepare()

        # THEN each datacenter gets its own factor, sized from only its own hosts
        create_keyspace_query = next(q for q in session.executed_queries if "CREATE KEYSPACE" in q)
        assert "'class': 'NetworkTopologyStrategy'" in create_keyspace_query
        assert "'dc1': 2" in create_keyspace_query
        assert "'dc2': 1" in create_keyspace_query

    def test_escapes_a_datacenter_name_containing_a_single_quote(self) -> None:
        # GIVEN a datacenter name (as reported by cluster metadata) containing a single quote.
        # Regression test for: interpolating it unescaped into the replication map produces
        # invalid CQL (and a crafted metadata value could otherwise alter the statement). CQL
        # escapes an embedded "'" by doubling it, the same rule PostgreSQL uses for literals.
        validator = _make_legacy_persistence_validator({"cassandra/0": LEGACY_UNIT_DATABAG})
        hosts = MetadataStub(hosts=[HostStub(datacenter="dc-o'brien")])
        session = SessionStub(cluster=ClusterStub(metadata=hosts))

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            validator.prepare()

        # THEN the embedded quote is escaped by doubling it, keeping the statement valid CQL
        create_keyspace_query = next(q for q in session.executed_queries if "CREATE KEYSPACE" in q)
        assert "'dc-o''brien': 1" in create_keyspace_query
