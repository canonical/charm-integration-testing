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

# Fields the legacy, unit-scoped "cassandra" interface actually publishes (see
# _CassandraConnectionMixin._connection_data). Used to pick, per unit, whether that unit's
# databag is usable - never to merge fields across units.
_LEGACY_REQUIRED_FIELDS = ("host", "username", "password")


def _quote_identifier(name: str) -> str:
    """Safely quote a CQL identifier for interpolation into a query string.

    Doubling embedded double-quotes is the standard, connection-independent way to escape a
    Cassandra (CQL) quoted identifier, mirroring PostgreSQL's identifier-quoting rules.
    """
    return '"' + name.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    """Safely quote a CQL string literal for interpolation into a query string.

    Doubling embedded single quotes is CQL's standard escaping for string literals (the same
    rule PostgreSQL uses). Needed anywhere a value from outside this validator's own control -
    e.g. a datacenter name reported by cluster metadata - is interpolated into a query, since an
    unescaped ``'`` would either produce invalid CQL or, worse, let a crafted value alter the
    statement being built.
    """
    return "'" + value.replace("'", "''") + "'"


class _CassandraConnectionMixin:
    """Shared credential-resolution and connection helpers for cassandra validators.

    Kept separate from ``CassandraClientPersistenceValidator`` so a future functional
    (``BaseValidator``) validator for this interface can reuse the same logic without duplicating
    it, mirroring ``_PostgreSQLConnectionMixin`` in ``validators/postgresql_client/validator.py``.
    """

    def _connection_data(self) -> dict[str, str]:
        """Return the connection databag for this relation.

        Prefers the modern, app-scoped convention (``endpoints``/``database``, matching
        ``postgresql_client``/``mongodb_client``/``etcd_client``), resolving any Juju-secret-based
        credentials via ``resolve_secret``. Falls back to the legacy, unit-scoped ``cassandra``
        interface published by the only charm currently available for it (see
        ``CassandraClientPersistenceValidator``'s class docstring): that charm never sets any
        app-scoped relation data, instead publishing ``host``/``native_transport_port``/
        ``username``/``password`` directly on each related unit's own databag, with no
        per-relation keyspace concept at all.

        Mirrors the unit-selection strategy ``MySQLValidator._connection_data`` uses for the
        similarly unit-scoped legacy ``mysql`` interface (``validators/mysql/validator.py``):
        never merge fields from different units - that could silently produce an invalid hybrid,
        e.g. one unit's ``host`` combined with another's credentials. Instead select a single
        unit's databag deterministically: the lowest-sorted unit whose databag has every legacy
        field, falling back to the lowest-sorted unit's databag so an incomplete deployment still
        surfaces a deterministic schema failure rather than a merged, inconsistent one.
        """
        creds = {
            **self.resolve_secret("secret-user", "username", "password"),  # type: ignore[attr-defined]
            **self.resolve_secret("secret-tls", "tls-ca"),  # type: ignore[attr-defined]
        }
        data: dict[str, str] = self.databag | creds  # type: ignore[attr-defined]
        if "endpoints" in data:
            return data

        units = sorted(self.relation.units, key=lambda u: u.name)  # type: ignore[attr-defined]
        unit_databags: list[dict[str, str]] = []
        for unit in units:
            unit_data = dict(self.relation.data.get(unit, {}))  # type: ignore[attr-defined]
            unit_creds = {
                **self.resolve_secret("secret-user", "username", "password", data=unit_data),  # type: ignore[attr-defined]
                **self.resolve_secret("secret-tls", "tls-ca", data=unit_data),  # type: ignore[attr-defined]
            }
            unit_databags.append(unit_data | unit_creds)
        for unit_databag in unit_databags:
            if all(str(unit_databag.get(f, "")).strip() for f in _LEGACY_REQUIRED_FIELDS):
                return unit_databag
        return unit_databags[0] if unit_databags else data

    def _parse_endpoints(self, data: dict[str, str]) -> tuple[list[str], int]:
        """Parse contact points and a shared port out of *data*.

        Prefers a modern, comma-separated "endpoints" field ("host:port, host:port, ..."); falls
        back to the legacy ``cassandra`` interface's single ``host``/``native_transport_port``
        fields when "endpoints" isn't present. ``Cluster`` takes one ``port`` shared by every
        contact point (unlike a list of "host:port" pairs), so every "endpoints" entry that
        specifies a port must agree on the same value; a provider advertising inconsistent or
        malformed (empty) per-entry ports is a misconfiguration this validator must reject rather
        than silently resolve by picking one value, since that could have this validator connect
        to - and validate - a different service than the one the provider actually advertised.

        An entry may be a bracketed IPv6 literal, e.g. "[::1]:9042" - ``partition(":")`` would
        split that on the *first* colon (inside the address itself) rather than the one
        separating host from port, leaving a mangled host and a non-numeric "port" that fails
        ``int()`` before any connection is attempted. ``rpartition(":")`` instead splits on the
        *last* colon, which is always the host/port separator for both plain and bracketed
        entries. ``Cluster`` expects bare contact points (no brackets) for IPv6 literals, so the
        brackets are stripped after splitting.
        """
        if "endpoints" not in data:
            host = data.get("host", "").strip()
            legacy_port = int(data["native_transport_port"]) if data.get("native_transport_port") else 9042
            return ([host] if host else [], legacy_port)
        endpoints = data["endpoints"]

        hosts: list[str] = []
        port: int | None = None
        for entry in (e.strip() for e in endpoints.split(",")):
            if not entry:
                continue
            if ":" in entry:
                host, _, port_str = entry.rpartition(":")
                if not port_str:
                    raise RuntimeError(f"endpoint {entry!r} in 'endpoints' has a malformed, empty port")
                try:
                    entry_port = int(port_str)
                except ValueError:
                    raise RuntimeError(f"endpoint {entry!r} in 'endpoints' has a malformed, non-numeric port") from None
                if port is not None and entry_port != port:
                    raise RuntimeError(
                        f"'endpoints' specifies inconsistent ports ({port} vs {entry_port}); "
                        "every contact point must share one port"
                    )
                port = entry_port
            else:
                # No colon at all: a bare host with no explicit port, not an empty host with an
                # explicit one - rpartition(":") can't distinguish the two (both yield host="",
                # non-empty remainder), so check for a colon up front instead.
                host = entry
            host = host.removeprefix("[").removesuffix("]")
            if host:
                hosts.append(host)
        return hosts, port if port is not None else 9042

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
        hosts, port = self._parse_endpoints(data)
        if not hosts:
            raise RuntimeError("no usable contact points found in 'endpoints'/'host'")
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
        try:
            session = cluster.connect()
        except Exception:
            # Cluster.connect() raising (e.g. the service is still restarting) means no Session
            # was ever returned, so none of the callers' `finally: session.cluster.shutdown()`
            # blocks can run - shut the cluster down here instead, or every failed connection
            # attempt leaks its driver resources (background IO threads, control connection).
            cluster.shutdown()
            raise
        session.default_consistency_level = ConsistencyLevel.QUORUM
        return session


class CassandraClientPersistenceValidator(_CassandraConnectionMixin, BasePersistenceValidator):
    """Reference-style persistence validator for the ``cassandra`` interface, modelled on
    ``PostgreSQLClientPersistenceValidator`` (see SQ103 and ``validators/postgresql_client/validator.py``).
    Registered under the ``cassandra`` interface (not ``cassandra_client``) to match the
    interface actually published by the ``cassandra`` charm on CharmHub - no charm currently
    provides or requires a ``cassandra_client`` interface.

    This validator supports two databag conventions:

    - **Modern** (app-scoped ``endpoints``/``database``/``username``/``password``, matching
      ``postgresql_client``/``mongodb_client``/``etcd_client``), for any future charm that
      publishes this interface that way.
    - **Legacy** (the only charm currently published for this interface, ``cassandra`` revision
      65): a reactive-framework charm that sets no app-scoped relation data at all, instead
      publishing ``host``/``native_transport_port``/``username``/``password`` directly on each
      related unit's own databag, with no per-relation keyspace concept. ``_connection_data``
      selects a single unit's databag deterministically (never merging across units, mirroring
      ``MySQLValidator._connection_data`` for the similarly unit-scoped legacy ``mysql``
      interface), and ``_open_session`` falls back to a dedicated keyspace this validator creates
      and owns (see ``_ensure_canary_keyspace``) when no ``database`` field is published.

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
                # `checkpoint_ref` is the clustering key, and is additionally constrained to the
                # expected [1, expected.ref] range: without this, rows replaced or corrupted with
                # the same marker but different checkpoint_ref values could still satisfy a plain
                # count and report a false PASS (mirroring the PostgreSQL reference
                # implementation's `checkpoint_ref BETWEEN 1 AND %s`).
                rows = session.execute(
                    f"SELECT COUNT(*) FROM {qualified_table} "  # nosec B608
                    "WHERE marker = %s AND checkpoint_ref >= 1 AND checkpoint_ref <= %s",
                    (marker, expected.ref),
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
        data = self._connection_data()
        # Incomplete credentials mean cleanup can't run: raise PersistenceNotApplicable so the
        # runner records a skip (not a successful cleanup) and keeps the tracked state, rather than
        # forgetting orphaned canary data.
        if not self._has_usable_credentials(data):
            raise PersistenceNotApplicable(
                "Relation credentials are incomplete; cleanup cannot remove canary data yet."
            )
        if "endpoints" in data and not data.get("database"):
            # Mirrors the guard in _open_session(): a modern, app-scoped relation missing its
            # "database" keyspace is a misconfiguration, not the unit-scoped legacy interface, so
            # cleanup must not guess at (or drop) a validator-owned keyspace it never created.
            raise PersistenceNotApplicable(
                "Relation published 'endpoints' but no 'database' keyspace; cleanup cannot proceed."
            )
        session = self._connect(data)
        try:
            keyspace = data.get("database")
            if keyspace:
                self._drop_discovered_canary_tables(session, keyspace)
            else:
                # Legacy "cassandra" interface: this validator owns a dedicated keyspace (see
                # _ensure_canary_keyspace) rather than sharing one with the charm, so cleanup drops
                # the whole keyspace outright instead of hunting for individual tables inside it.
                # IF EXISTS makes this safe to call even if prepare() was never reached.
                session.execute(
                    f"DROP KEYSPACE IF EXISTS {_quote_identifier(self._canary_keyspace_name())}"  # nosec B608 - keyspace name is derived from a fixed model/relation/unit hash, not user input
                )
        finally:
            session.cluster.shutdown()

    def _drop_discovered_canary_tables(self, session: Session, keyspace: str) -> None:
        """Discover and drop every canary table this validator instance may have created in
        *keyspace*, matching the fixed-width pattern _canary_table_regex() produces so a
        same-prefixed but unrelated table (e.g. a hand-created "..._backup") is skipped instead
        of destroyed."""
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

    def _require_requires_role(self) -> None:
        if self.role != "requires":
            raise PersistenceNotApplicable(f"Role '{self.role}' is not supported by {self.__class__.__name__}.")

    def _has_usable_credentials(self, data: dict[str, str]) -> bool:
        """Whether *data* has everything _open_session()/_connect() needs: a username/password
        pair, plus at least one usable contact point under either the modern "endpoints" field or
        the legacy "host" field."""
        if not self.validate_schema(["username", "password"], data=data).passed:
            return False
        hosts, _ = self._parse_endpoints(data)
        return bool(hosts)

    def _open_session(self) -> tuple[Session, str]:
        data = self._connection_data()
        # Unlike the functional validator's ValidationCheck-reporting path, these methods have no
        # check list to report a schema failure through, so a missing/blank required field is
        # raised rather than silently reaching the driver with incomplete credentials.
        schema_check = self.validate_schema(["username", "password"], data=data)
        if not schema_check.passed:
            raise RuntimeError(f"Cannot open a connection for {self.endpoint}: {schema_check.message}")
        if "endpoints" in data and not data.get("database"):
            # A modern, app-scoped relation (publishing "endpoints") is expected to also publish
            # "database" - the provider's intended keyspace. Falling back to a validator-owned
            # keyspace here would mean reading/writing an independent keyspace instead of the
            # provider's, letting a misconfigured modern provider produce a false persistence
            # PASS. The dedicated-keyspace fallback below is reserved for the unit-scoped legacy
            # "cassandra" interface, which never has an "endpoints" key at all.
            raise RuntimeError(
                f"Cannot open a connection for {self.endpoint}: relation published 'endpoints' but "
                "no 'database' keyspace"
            )
        session = self._connect(data)
        keyspace = data.get("database")
        if keyspace:
            # Modern convention: the charm already provisioned a per-relation keyspace for us.
            return session, keyspace
        # Legacy "cassandra" interface: no per-relation keyspace exists at all, so this validator
        # owns a dedicated keyspace, scoped the same way its canary tables already are (see
        # _canary_table_prefix), and creates it on demand. If keyspace creation fails (e.g. the
        # credentials lack permission, or schema propagation fails), the session must still be
        # shut down here - this is the last point before the exception escapes the method, past
        # which the prepare()/checkpoint()/cleanup() finally blocks that normally close it can't
        # help, since they never receive the session.
        try:
            return session, self._ensure_canary_keyspace(session)
        except Exception:
            session.cluster.shutdown()
            raise

    def _canary_keyspace_name(self) -> str:
        return f"canary_{self._canary_scope_token()}"

    def _ensure_canary_keyspace(self, session: Session) -> str:
        keyspace = self._canary_keyspace_name()
        session.execute(
            f"CREATE KEYSPACE IF NOT EXISTS {_quote_identifier(keyspace)} "  # nosec B608 - keyspace name is derived from a fixed model/relation/unit hash, not user input
            f"WITH replication = {self._canary_keyspace_replication(session)}"
        )
        return keyspace

    def _canary_keyspace_replication(self, session: Session) -> str:
        """Replication settings for ``_ensure_canary_keyspace``, sized to the live cluster.

        A fixed replication factor of 1 would mean losing (or replacing) the single node that
        owns the canary's only replica makes this validator report data loss caused by its own
        keyspace setup, even on a cluster where a properly replicated application keyspace would
        have survived the same event. ``NetworkTopologyStrategy`` (rather than ``SimpleStrategy``,
        which ignores rack/datacenter placement entirely) sizes a per-datacenter factor from that
        datacenter's node count, capped at 3 since Cassandra sees diminishing resilience/consistency
        benefit beyond that. Node counts include hosts the driver currently reports as down: this
        keyspace is only ever created once (``IF NOT EXISTS``), so sizing it from a transient
        down-count would under-replicate it permanently relative to the real cluster size, and a
        later disruption could then report loss caused by the canary's own under-replication rather
        than by the application's own (properly replicated) data actually being lost.
        """
        host_counts_by_dc: dict[str, int] = {}
        for host in session.cluster.metadata.all_hosts():
            datacenter = getattr(host, "datacenter", None) or "datacenter1"
            host_counts_by_dc[datacenter] = host_counts_by_dc.get(datacenter, 0) + 1
        if not host_counts_by_dc:
            host_counts_by_dc["datacenter1"] = 1
        factors = ", ".join(
            f"{_quote_literal(datacenter)}: {max(1, min(host_count, 3))}"
            for datacenter, host_count in sorted(host_counts_by_dc.items())
        )
        return f"{{'class': 'NetworkTopologyStrategy', {factors}}}"

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
