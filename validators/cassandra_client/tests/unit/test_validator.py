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
from validators.test_utils.helpers import make_charm_from_relation
from validators.test_utils.stubs import (
    ApplicationStub,
    RelationRoleStub,
    RelationStub,
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
            interface_name="cassandra_client",
            role=role,
            local_model_uuid=model_uuid,
            local_unit_name=unit_name,
        ),
    )
    return CassandraClientPersistenceValidator(charm, cast(ops.Relation, relation))


@dataclass
class ClusterStub:
    """Minimal stub for cassandra.cluster.Cluster; tracks whether shutdown() was called."""

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

    def test_filters_row_count_by_token(self) -> None:
        # GIVEN
        # Regression test for: checkpoint() must count only rows tagged with the per-run token,
        # not every row in the table - a table recreated from scratch with an unrelated but
        # equally-sized set of rows would otherwise still pass.
        validator = _make_persistence_validator(VALID_DATABAG)
        session = SessionStub(execute_results=[RowsStub([(1,)])])

        with patch("validators.cassandra_client.validator.Cluster") as mock_cluster_cls:
            mock_cluster_cls.return_value.connect.return_value = session
            # WHEN
            validator.checkpoint(PersistenceState(token=TEST_TOKEN, id=99, ref=1))

        # THEN the row count query and the follow-up insert are both scoped to the same token,
        # which is random per prepare() run so it cannot be reproduced by a recreated table
        select_index = next(i for i, q in enumerate(session.executed_queries) if "COUNT(*)" in q)
        insert_index = next(i for i, q in enumerate(session.executed_queries) if "INSERT INTO" in q)
        assert "WHERE marker = %s" in session.executed_queries[select_index]
        assert session.executed_params[select_index] == (TEST_TOKEN,)
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
        assert result.interface == "cassandra_client"
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
