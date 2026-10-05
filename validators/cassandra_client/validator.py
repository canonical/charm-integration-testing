# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import hashlib
import re
import ssl
import tempfile
import uuid
from datetime import datetime, timezone

from cassandra import ConsistencyLevel, InvalidRequest
from cassandra.auth import PlainTextAuthProvider
from cassandra.cluster import Cluster, Session

from validators.base import (
    BasePersistenceValidator,
    PersistenceNotApplicable,
    PersistenceState,
    ValidationCheck,
    ValidationResult,
)

# Table name prefix for persistence-validator canary tables. Kept as a module constant so
# cleanup() (which has no per-call state to work from) can discover every canary table it may
# have created by pattern rather than by identifier.
#
# Unlike PostgreSQL (63-byte identifier limit), Cassandra caps table names at 48 characters, so
# the prefix/scope-token/identifier widths below are deliberately shorter than the
# PostgreSQLClientPersistenceValidator reference implementation uses, to leave comfortable margin
# under that limit.
_CANARY_TABLE_PREFIX = "canary_"

# prepare() masks its identifier to 63 bits, so a genuine canary identifier never exceeds this
# value and never needs more than 19 decimal digits (2**63 - 1 has 19 digits). cleanup()'s
# discovery regex only checks a candidate table name's *shape* (prefix + scope token + 19 digits);
# this bound lets it also reject an out-of-range look-alike with the right shape that prepare()
# couldn't have produced.
_MAX_CANARY_IDENTIFIER = (1 << 63) - 1
_IDENTIFIER_WIDTH = 19


def _quote_identifier(name: str) -> str:
    """Safely quote a CQL identifier for interpolation into a query string.

    Doubling embedded double-quotes is the standard, connection-independent way to escape a
    Cassandra (CQL) quoted identifier, mirroring PostgreSQL's identifier-quoting rules.
    """
    return '"' + name.replace('"', '""') + '"'


class _CassandraConnectionMixin:
    """Shared credential-resolution and connection helpers for cassandra_client validators.

    Kept separate from ``CassandraClientPersistenceValidator`` so a future functional
    (``BaseValidator``) validator for this interface can reuse the same logic without duplicating
    it, mirroring ``_PostgreSQLConnectionMixin`` in ``validators/postgresql_client/validator.py``.
    """

    def _resolve_credentials(self) -> dict[str, str]:
        """Resolve credentials from the relation databag or Juju secrets."""
        return {
            **self.resolve_secret("secret-user", "username", "password"),  # type: ignore[attr-defined]
            **self.resolve_secret("secret-tls", "tls-ca"),  # type: ignore[attr-defined]
        }

    def _parse_endpoints(self, endpoints: str) -> tuple[list[str], int]:
        """Parse a comma-separated "host:port" list into contact points and a shared port.

        ``Cluster`` takes one ``port`` shared by every contact point (unlike a list of
        "host:port" pairs), so the port from the last entry that specifies one wins; every entry
        is expected to share the same port in practice.
        """
        hosts: list[str] = []
        port = 9042
        for entry in (e.strip() for e in endpoints.split(",")):
            if not entry:
                continue
            host, _, port_str = entry.partition(":")
            if host:
                hosts.append(host)
            if port_str:
                port = int(port_str)
        return hosts, port

    def _build_ssl_context(self, ca_content: str) -> ssl.SSLContext:
        """Build an SSLContext from PEM CA content without leaving a temp file behind.

        ``ssl.create_default_context(cafile=...)`` reads and parses the file synchronously
        before returning, so the temporary file only needs to exist for the duration of that
        call - the ``with`` block's cleanup runs after the call completes but before this method
        returns, unlike the persistent-temp-file approach ``_MongoDBConnectionMixin`` uses (which
        needs the file to still exist later, at actual connection time).
        """
        with tempfile.NamedTemporaryFile(mode="w", suffix=".pem") as ca_file:
            ca_file.write(ca_content)
            ca_file.flush()
            return ssl.create_default_context(cafile=ca_file.name)

    def _connect(self, data: dict[str, str]) -> Session:
        """Open a Cassandra session using relation credentials.

        Callers are responsible for shutting the session's cluster down (``session.cluster.shutdown()``)
        once finished - this mirrors psycopg2's ``conn.close()`` in ``_PostgreSQLConnectionMixin``.
        """
        hosts, port = self._parse_endpoints(data["endpoints"])
        if not hosts:
            raise RuntimeError("no usable contact points found in 'endpoints'")
        auth_provider = PlainTextAuthProvider(username=data["username"], password=data["password"])
        ssl_context = self._build_ssl_context(data["tls-ca"]) if data.get("tls-ca") else None
        cluster = Cluster(
            contact_points=hosts,
            port=port,
            auth_provider=auth_provider,
            ssl_context=ssl_context,
            connect_timeout=5,
            control_connection_timeout=5,
        )
        session = cluster.connect()
        session.default_consistency_level = ConsistencyLevel.QUORUM
        return session


class CassandraClientPersistenceValidator(_CassandraConnectionMixin, BasePersistenceValidator):
    """Reference-style persistence validator for the ``cassandra_client`` interface, modelled on
    ``PostgreSQLClientPersistenceValidator`` (see SQ103 and ``validators/postgresql_client/validator.py``).

    Each validator instance owns a dedicated canary table named
    ``canary_{scope_token}_{identifier}``, where ``scope_token`` is a fixed-width hash of this
    model's UUID, relation ID and unit name (see ``_canary_table_prefix``) and ``identifier`` is a
    fixed-width, zero-padded value chosen by ``prepare()`` and carried forward by the caller (the
    test harness) as ``PersistenceState.id``. Both components have a fixed length so ``cleanup()``
    can validate the *exact* shape of a candidate table name before dropping it. Every row also
    carries a random, unguessable ``token`` generated by ``prepare()`` and carried forward as
    ``PersistenceState.token``, so ``checkpoint()`` can detect data loss (or a table silently
    recreated from scratch) by counting only rows tagged with that token, rather than trusting a
    plain ``count(*)`` that a coincidentally-sized but unrelated table could satisfy. The token
    must be random rather than derived from ``identifier``/``ref``: those are reproducible, so a
    backend that lost the canary data and recreated it from scratch would reproduce the same value
    and pass falsely. ``ref`` tracks how many tagged rows are expected so far.

    Unlike PostgreSQL, Cassandra has no auto-incrementing row identity (no ``SERIAL``), so this
    implementation relies solely on the random ``token`` to detect a dropped-and-recreated table -
    a recreated table starts empty, so its row count for the original token is zero, which already
    fails the ``expected.ref`` comparison. The canary table's primary key is
    ``(marker, checkpoint_ref)`` with ``marker`` (the token) as the partition key, so both the
    identity-scoped row count in ``checkpoint()`` and canary discovery in ``cleanup()`` can rely on
    single-partition reads instead of needing ``ALLOW FILTERING`` or a secondary index.

    Persistence only applies to the requirer side of the relation (the side holding credentials to
    connect out); the provider side raises ``PersistenceNotApplicable``.
    """

    def prepare(self) -> PersistenceState:
        self._require_requires_role()
        # Masked to 63 bits so the canary table name - which also carries a fixed-width scope
        # token - stays within Cassandra's 48-character table name limit, while still leaving far
        # more entropy than a test run could collide on.
        identifier = uuid.uuid4().int & _MAX_CANARY_IDENTIFIER
        # Random, unguessable per-run token written to every canary row and matched on by
        # checkpoint(). It must not be derivable from `identifier`/`ref`: those are reproducible,
        # so a backend that lost the canary data and recreated the table from scratch would
        # reproduce the same value and pass falsely.
        token = uuid.uuid4().hex
        session, keyspace = self._open_session()
        try:
            table = self._qualified_table(keyspace, self._canary_table_name(identifier))
            session.execute(
                f"CREATE TABLE IF NOT EXISTS {table} "  # nosec B608 - table name is derived from a masked random identifier, not user input
                "(marker text, checkpoint_ref int, written_at timestamp, PRIMARY KEY (marker, checkpoint_ref))"
            )
            # checkpoint_ref=1 tracks that this row was written at prepare() time (ref=1).
            session.execute(
                f"INSERT INTO {table} (marker, checkpoint_ref, written_at) VALUES (%s, %s, %s)",  # nosec B608
                (token, 1, datetime.now(timezone.utc)),
            )
        finally:
            session.cluster.shutdown()
        return PersistenceState(id=identifier, ref=1, token=token)

    def checkpoint(self, expected: PersistenceState) -> tuple[ValidationResult, PersistenceState]:
        self._require_requires_role()
        if expected.ref < 1:
            # prepare() always returns ref=1 and checkpoint() only ever advances it, so a
            # restored/malformed PersistenceState with ref <= 0 can't have come from a real prior
            # run. Without this check, an empty or never-prepared table (actual == 0) could
            # satisfy `actual == expected.ref` for ref=0 and report a false PASS.
            raise ValueError(f"expected.ref {expected.ref} is out of range (expected >= 1)")
        # Raises ValueError if expected.id is out of range - expected.id comes from --refs, a
        # (possibly restored/malformed) PersistenceState, not a value prepare() just minted.
        table_name = self._canary_table_name(expected.id)
        marker = expected.token
        session, keyspace = self._open_session()
        qualified_table = self._qualified_table(keyspace, table_name)
        try:
            try:
                # `marker` is the partition key, so this is a single-partition read, not a full
                # table scan - no ALLOW FILTERING needed, unlike a query on a non-key column.
                rows = session.execute(
                    f"SELECT COUNT(*) FROM {qualified_table} WHERE marker = %s",  # nosec B608
                    (marker,),
                )
                row = rows.one()
                matching = int(row[0]) if row else 0
            except InvalidRequest:
                # The table (or keyspace) doesn't exist - e.g. it was dropped, or prepare() never
                # ran for this identifier. Treat as zero matching rows rather than propagating,
                # mirroring the PostgreSQL reference implementation's "table not found" handling.
                matching = 0

            passed = matching == expected.ref
            # Only write the next canary row when this checkpoint passed: ValidatorRunner only
            # carries the advanced PersistenceState forward on a PASS result, so writing here
            # unconditionally would grow `actual` past what the harness will ever compare against
            # again, masking the mismatch behind permanent drift.
            if passed:
                next_ref = expected.ref + 1
                session.execute(
                    f"INSERT INTO {qualified_table} (marker, checkpoint_ref, written_at) "  # nosec B608
                    "VALUES (%s, %s, %s)",
                    (marker, next_ref, datetime.now(timezone.utc)),
                )
        finally:
            session.cluster.shutdown()

        check = ValidationCheck(
            name="row_count",
            passed=passed,
            message=(
                f"Found expected {matching} marked row(s) in '{table_name}'."
                if passed
                else (
                    f"Expected {expected.ref} marked row(s) with matching identity in '{table_name}', "
                    f"found {matching}. Data may have been lost, or the table was recreated without "
                    "the original canary rows."
                )
            ),
        )
        result = self._make_result(level="deep", checks=[check])
        new_state = PersistenceState(id=expected.id, ref=expected.ref + 1, token=expected.token) if passed else expected
        return result, new_state

    def cleanup(self) -> None:
        """Drop every canary table this validator instance (or a prior instance of it) created.

        ``cleanup()`` takes no state argument (see ``BasePersistenceValidator.cleanup``), so every
        table matching this instance's canary name pattern is discovered via ``system_schema.tables``
        and dropped, rather than dropping one table by identifier. This also mops up a table left
        behind by an interrupted run (e.g. a crash between ``prepare()`` and the next ``cleanup()``).

        Discovery is scoped to a model+relation+unit namespace (see ``_canary_table_prefix``) so
        concurrent relations sharing a keyspace can't drop each other's tables. It does not sweep
        up a stray table from a relation removed and re-added under a new ID - an accepted
        trade-off, since a fresh ``prepare()`` for the new ID starts its own table anyway.

        Unlike PostgreSQL's ``information_schema.tables`` (which needs an escaped ``LIKE`` prefix
        match because it has no efficient way to filter server-side on an exact suffix shape),
        ``system_schema.tables`` is keyed by ``(keyspace_name, table_name)`` with ``keyspace_name``
        as the partition key - so this query is already a single-partition read, and every
        candidate name is matched against ``_canary_table_regex()``/``_MAX_CANARY_IDENTIFIER`` in
        Python before being dropped, so a same-prefixed but unrelated table (e.g. a hand-created
        ``..._backup``) is skipped instead of destroyed.
        """
        self._require_requires_role()
        # Incomplete credentials mean cleanup can't run: raise PersistenceNotApplicable so the
        # runner records a skip (not a successful cleanup) and keeps the tracked state, rather than
        # forgetting orphaned canary data.
        creds = self._resolve_credentials()
        if not self.validate_schema(["endpoints", "database", "username", "password"], creds).passed:
            raise PersistenceNotApplicable(
                "Relation credentials are incomplete; cleanup cannot remove canary data yet."
            )
        session, keyspace = self._open_session()
        try:
            rows = session.execute(
                "SELECT table_name FROM system_schema.tables WHERE keyspace_name = %s",
                (keyspace,),
            )
            table_names = [row[0] for row in rows]
            name_regex = self._canary_table_regex()
            for table_name in table_names:
                match = name_regex.fullmatch(table_name)
                if not match or int(match.group("identifier")) > _MAX_CANARY_IDENTIFIER:
                    continue
                qualified_table = self._qualified_table(keyspace, table_name)
                session.execute(f"DROP TABLE IF EXISTS {qualified_table}")  # nosec B608 - identifier came from system_schema and was matched against a fixed shape regex above
        finally:
            session.cluster.shutdown()

    def _require_requires_role(self) -> None:
        if self.role != "requires":
            raise PersistenceNotApplicable(f"Role '{self.role}' is not supported by {self.__class__.__name__}.")

    def _open_session(self) -> tuple[Session, str]:
        creds = self._resolve_credentials()
        data = self.databag | creds
        # Unlike the functional validator's ValidationCheck-reporting path, these methods have no
        # check list to report a schema failure through, so a missing/blank required field is
        # raised rather than silently reaching the driver with incomplete credentials.
        schema_check = self.validate_schema(["endpoints", "database", "username", "password"], creds)
        if not schema_check.passed:
            raise RuntimeError(f"Cannot open a connection for {self.endpoint}: {schema_check.message}")
        session = self._connect(data)
        return session, data["database"]

    def _qualified_table(self, keyspace: str, table_name: str) -> str:
        return f"{_quote_identifier(keyspace)}.{_quote_identifier(table_name)}"

    def _canary_table_prefix(self) -> str:
        """Prefix scoped to this model, relation and unit, so cleanup discovery can't cross boundaries.

        ``self.relation_id`` is stable for the lifetime of a given relation, but relation IDs are
        assigned independently per model and can collide numerically across two different models
        relating to the same backend. The unit name is also part of the scope: the runner runs
        persistence validators on *every* unit of the application, so two units share both
        ``model.uuid`` and ``relation_id`` while owning separate canary tables. All three are
        folded into a single fixed-width ``scope_token`` - the first 12 hex characters (48 bits) of
        a SHA-256 hash of ``f"{model_uuid}:{relation_id}:{unit_name}"`` - kept shorter than
        PostgreSQL's 16-hex-character scope token to leave room for the 19-digit identifier within
        Cassandra's 48-character table name limit, while ``cleanup()`` can still validate its exact
        shape via ``_canary_table_regex()``.
        """
        return f"{_CANARY_TABLE_PREFIX}{self._canary_scope_token()}_"

    def _canary_scope_token(self) -> str:
        unit_name = self.charm.model.unit.name
        digest_input = f"{self.charm.model.uuid}:{self.relation_id}:{unit_name}".encode()
        return hashlib.sha256(digest_input).hexdigest()[:12]

    def _canary_table_regex(self) -> "re.Pattern[str]":
        """Exact-shape match for this relation's canary tables: prefix + fixed-width digits.

        Used by ``cleanup()`` to reject a table that merely shares the discovery prefix (e.g. a
        hand-created ``..._backup``) but doesn't match the fixed-width zero-padded identifier
        suffix ``_canary_table_name()`` always produces. Matching this shape alone isn't sufficient
        - ``cleanup()`` also checks the captured ``identifier`` against ``_MAX_CANARY_IDENTIFIER``.
        """
        return re.compile(re.escape(self._canary_table_prefix()) + rf"(?P<identifier>[0-9]{{{_IDENTIFIER_WIDTH}}})")

    def _canary_table_name(self, identifier: int) -> str:
        # Zero-padded to a fixed 19 digits (prepare() masks identifiers to 63 bits, so never more
        # than 19) so every canary name has the same shape, which _canary_table_regex() relies on.
        # checkpoint() passes back an identifier from a possibly restored/malformed state, so
        # range-check it here too rather than letting Cassandra reject an oversized/odd name.
        if not 0 <= identifier <= _MAX_CANARY_IDENTIFIER:
            raise ValueError(f"canary identifier {identifier} is out of range (expected 0..{_MAX_CANARY_IDENTIFIER})")
        return f"{self._canary_table_prefix()}{identifier:0{_IDENTIFIER_WIDTH}d}"
