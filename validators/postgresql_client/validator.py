# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import hashlib
import re
import time
import urllib.parse
import uuid

import psycopg2

from validators.base import (
    BasePersistenceValidator,
    BaseValidator,
    PersistenceNotApplicable,
    PersistenceState,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

# Table name prefix for persistence-validator canary tables. Kept as a module constant so
# PostgreSQLClientPersistenceValidator.cleanup() (which has no per-call state to work from) can
# discover every canary table it may have created by pattern rather than by identifier.
_CANARY_TABLE_PREFIX = "validator_canary_"

# prepare() masks its identifier to 63 bits, so a genuine canary identifier never exceeds this
# value. cleanup()'s discovery regex only checks a candidate table name's *shape* (prefix + 20
# digits); this bound lets it also reject an out-of-range look-alike that has the right shape but
# couldn't have been produced by prepare().
_MAX_CANARY_IDENTIFIER = (1 << 63) - 1


def _quote_identifier(name: str) -> str:
    """Safely quote a SQL identifier for interpolation into a query string.

    Doubling embedded double-quotes is the standard, connection-independent way to escape a
    Postgres identifier (unlike string literals, quoted identifiers do not process backslash
    escapes). Used instead of ``psycopg2.extensions.quote_ident``, which requires a live
    connection/cursor instance and so cannot be unit-tested against a stub.
    """
    return '"' + name.replace('"', '""') + '"'


class _PostgreSQLConnectionMixin:
    """Shared credential-resolution and connection helpers for postgresql_client validators.

    Both ``PostgreSQLClientValidator`` (health probe) and ``PostgreSQLClientPersistenceValidator``
    (durability probe) need to resolve the same relation credentials and open the same kind of
    psycopg2 connection, so that logic lives here once instead of being duplicated.
    """

    def _resolve_credentials(self) -> dict[str, str]:
        """Resolve credentials from the relation databag or Juju secrets."""
        return {
            **self.resolve_secret("secret-user", "username", "password", "uris"),  # type: ignore[attr-defined]
            **self.resolve_secret("secret-tls", "tls", "tls-ca"),  # type: ignore[attr-defined]
        }

    def _connect(self, uri: str) -> "psycopg2.extensions.connection":
        """Open a psycopg2 connection using a PostgreSQL URI."""
        return psycopg2.connect(dsn=uri, connect_timeout=5)

    def _safe_uri_string(self, uri: str) -> str:
        parsed = urllib.parse.urlsplit(uri)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        safe_target = f"{host}{port}{parsed.path or ''}"
        return safe_target

    def _check_database_consistency(self, uri: str, expected_db: str) -> ValidationCheck:
        """Verify the database in the URI matches the `database` field in the databag."""
        try:
            parsed = urllib.parse.urlparse(uri)
            uri_db = urllib.parse.unquote(parsed.path.lstrip("/"))
        except Exception as exc:
            return ValidationCheck(name="database_consistency", passed=False, message=f"Could not parse URI: {exc}")
        if uri_db == expected_db:
            return ValidationCheck(
                name="database_consistency",
                passed=True,
                message=f"URI database '{uri_db}' matches databag 'database' field.",
            )
        return ValidationCheck(
            name="database_consistency",
            passed=False,
            message=f"URI database '{uri_db}' does not match databag 'database' field '{expected_db}'.",
        )


class PostgreSQLClientValidator(_PostgreSQLConnectionMixin, BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level == "simple":
            return self._validate_simple()
        elif level == "deep":
            return self._validate_deep()
        else:
            return self._skipped_result_due_to_level(level)

    def _validate_simple(self) -> ValidationResult:
        """L1: Connectivity & Auth with read-only canary query."""
        checks: list[ValidationCheck] = []

        # --- 1. Remote app presence ---
        error_result = self._check_relation_exists("simple")
        if error_result:
            return error_result

        # --- 2. Resolve credentials (plain fields or Juju secrets) ---
        creds = self._resolve_credentials()

        # --- 3. Schema check ---
        schema_check = self.validate_schema(["uris", "database", "username", "password"], creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="simple", checks=checks)

        # --- 4. Database consistency check ---
        data = self.databag | creds
        uri = data["uris"].split(",")[0].strip()
        db_check = self._check_database_consistency(uri, data["database"])
        checks.append(db_check)
        if not db_check.passed:
            return self._make_result(level="simple", checks=checks)

        # --- 5. Connect ---
        try:
            conn = self._connect(uri)
            safe_target = self._safe_uri_string(uri)
            checks.append(ValidationCheck(name="connect", passed=True, message=f"Connected to {safe_target}."))
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=str(exc)))
            return self._make_result(level="simple", checks=checks)

        # --- 6. Canary read-only query ---
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                checks.append(ValidationCheck(name="query", passed=True, message="SELECT 1 OK."))
        except Exception as exc:
            checks.append(ValidationCheck(name="query", passed=False, message=str(exc)))
        else:
            # --- 7. Optional extensions check (only when query succeeded) ---
            try:
                with conn.cursor() as cur:
                    ext_check = self._check_extensions(cur)
                    if ext_check is not None:
                        checks.append(ext_check)
            except Exception as exc:
                checks.append(ValidationCheck(name="extensions", passed=False, message=str(exc)))
        finally:
            conn.close()

        return self._make_result(level="simple", checks=checks)

    def _validate_deep(self) -> ValidationResult:
        """L2: Read/Write capability with canary table (create, write, read-verify, cleanup)."""
        timeout_secs = 10
        checks: list[ValidationCheck] = []

        # --- 1. Remote app presence ---
        error_result = self._check_relation_exists("deep")
        if error_result:
            return error_result

        # --- 2. Resolve credentials (plain fields or Juju secrets) ---
        creds = self._resolve_credentials()

        # --- 3. Schema check ---
        schema_check = self.validate_schema(["uris", "database", "username", "password"], creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="deep", checks=checks)

        # --- 4. Database consistency check ---
        data = self.databag | creds
        uri = data["uris"].split(",")[0].strip()
        db_check = self._check_database_consistency(uri, data["database"])
        checks.append(db_check)
        if not db_check.passed:
            return self._make_result(level="deep", checks=checks)

        # --- 5. Connect ---
        # Latency is timed from here, not from the top of the function, so that
        # Juju secret/relation-data resolution (steps 1-4) - which can be slow for
        # cross-model relations independent of the database itself - is not counted
        # against the database round-trip budget below.
        start_time = time.monotonic()
        try:
            conn = self._connect(uri)
            conn.autocommit = True
            safe_target = self._safe_uri_string(uri)
            checks.append(ValidationCheck(name="connect", passed=True, message=f"Connected to {safe_target}."))
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=str(exc)))
            return self._make_result(level="deep", checks=checks)

        # --- 6. Canary read-only query ---
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                checks.append(ValidationCheck(name="query", passed=True, message="SELECT 1 OK."))
        except Exception as exc:
            checks.append(ValidationCheck(name="query", passed=False, message=str(exc)))
            conn.close()
            return self._make_result(level="deep", checks=checks)

        # --- 7. Optional extensions check ---
        try:
            with conn.cursor() as cur:
                ext_check = self._check_extensions(cur)
                if ext_check is not None:
                    checks.append(ext_check)
                    if not ext_check.passed:
                        conn.close()
                        return self._make_result(level="deep", checks=checks)
        except Exception as exc:
            checks.append(ValidationCheck(name="extensions", passed=False, message=str(exc)))
            conn.close()
            return self._make_result(level="deep", checks=checks)

        # --- 8. Create canary table, write, read-verify, cleanup ---
        canary_table = f"__canary_{uuid.uuid4().hex[:8]}"
        try:
            with conn.cursor() as cur:
                # Create
                cur.execute(f"CREATE TABLE {canary_table} (id SERIAL PRIMARY KEY, marker TEXT NOT NULL)")  # nosec B608 - table name is UUID-generated

                # Write
                cur.execute(f"INSERT INTO {canary_table} (marker) VALUES (%s) RETURNING id", ("validator-probe",))  # nosec B608 - table name is UUID-generated
                row = cur.fetchone()
                inserted_id = row[0] if row else None

                # Read-verify
                if inserted_id is not None:
                    cur.execute(f"SELECT marker FROM {canary_table} WHERE id = %s", (inserted_id,))  # nosec B608 - table name is UUID-generated
                    read_row = cur.fetchone()
                    if read_row and read_row[0] == "validator-probe":
                        checks.append(
                            ValidationCheck(
                                name="write_read_verify",
                                passed=True,
                                message="Successfully wrote, read, and verified test row.",
                            )
                        )
                    else:
                        checks.append(
                            ValidationCheck(
                                name="write_read_verify",
                                passed=False,
                                message="Failed to verify written row.",
                            )
                        )
                else:
                    checks.append(
                        ValidationCheck(
                            name="write_read_verify",
                            passed=False,
                            message="INSERT returned no ID.",
                        )
                    )
        except Exception as exc:
            checks.append(
                ValidationCheck(
                    name="write_read_verify",
                    passed=False,
                    message=str(exc),
                )
            )

        # --- 9. Cleanup: drop canary table ---
        cleanup_passed = False
        cleanup_message = ""
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {canary_table}")  # nosec B608 - table name is UUID-generated
            cleanup_passed = True
            cleanup_message = "Dropped canary table."
        except Exception as exc:  # nosec B110 - best-effort cleanup
            cleanup_message = f"Failed to drop canary table: {exc}"
        finally:
            conn.close()

        checks.append(ValidationCheck(name="cleanup", passed=cleanup_passed, message=cleanup_message))

        # --- 10. Latency check ---
        elapsed = time.monotonic() - start_time
        if elapsed > timeout_secs:
            checks.append(
                ValidationCheck(
                    name="latency",
                    passed=False,
                    message=f"Deep validation took {elapsed:.1f}s, exceeded {timeout_secs}s limit.",
                )
            )
        else:
            checks.append(
                ValidationCheck(
                    name="latency",
                    passed=True,
                    message=f"Deep validation completed in {elapsed:.1f}s.",
                )
            )

        return self._make_result(level="deep", checks=checks)

    def _check_relation_exists(self, level: ValidationLevel) -> ValidationResult | None:
        """Return an ERROR result if the remote app is absent, else None."""
        if not self.relation_exists():
            return self._make_result(
                status="ERROR",
                level=level,
                error=f"No remote application on relation '{self.endpoint}'.",
            )
        return None

    def _check_extensions(self, cur: "psycopg2.extensions.cursor") -> ValidationCheck | None:
        """Verify declared extensions are installed. Returns None when field is absent."""
        extensions_raw = self.databag.get("extensions", "").strip()
        if not extensions_raw:
            return None
        exts = [e.strip() for e in extensions_raw.split(",") if e.strip()]
        missing: list[str] = []
        for ext in exts:
            cur.execute("SELECT COUNT(*) FROM pg_extension WHERE extname = %s", (ext,))
            row = cur.fetchone()
            if not row or row[0] == 0:
                missing.append(ext)
        return ValidationCheck(
            name="extensions",
            passed=not missing,
            message="OK" if not missing else f"Missing: {', '.join(missing)}",
        )


class PostgreSQLClientPersistenceValidator(_PostgreSQLConnectionMixin, BasePersistenceValidator):
    """Reference persistence validator implementation (see SQ103).

    Each validator instance owns a dedicated canary table named
    ``validator_canary_{scope_token}_{identifier}``, where ``scope_token`` is a fixed-width hash of
    this model's UUID, relation ID and unit name (see ``_canary_table_prefix``) and ``identifier``
    is a fixed-width, zero-padded value chosen by ``prepare()`` and carried forward by the caller
    (the test harness) as ``PersistenceState.id``. Both components have a fixed length so
    ``cleanup()`` can validate the *exact* shape of a candidate table name before dropping it.
    Every row also carries a random, unguessable ``token`` generated by ``prepare()`` and carried
    forward as ``PersistenceState.token``, so ``checkpoint()`` can detect data loss (or a table
    silently recreated from scratch) by counting only rows tagged with that token, rather than
    trusting a plain ``count(*)`` that a coincidentally-sized but unrelated table could satisfy.
    The token must be random rather than derived from ``identifier``/``ref``: those are
    reproducible, so a backend that lost the canary data and recreated it from scratch (resetting
    the ``SERIAL`` sequence, for example) would reproduce the same value and pass falsely. ``ref``
    tracks how many tagged rows are expected so far.

    Persistence only applies to the requirer side of the relation (the side holding credentials to
    connect out); the provider side raises ``PersistenceNotApplicable``, mirroring the role check
    ``PostgreSQLClientValidator.validate()`` performs for the functional probe.
    """

    def prepare(self) -> PersistenceState:
        self._require_requires_role()
        # Masked to 63 bits (rather than the full 128-bit uuid4().int) so the canary table name -
        # which also carries a fixed-width scope token - stays within PostgreSQL's 63-byte
        # identifier limit, while still leaving far more entropy than a test run could collide on.
        identifier = uuid.uuid4().int & ((1 << 63) - 1)
        table = self._canary_table_name(identifier)
        # Random, unguessable per-run token written to every canary row and matched on by
        # checkpoint(). It must not be derivable from `identifier`/`ref`: those are reproducible,
        # so a backend that lost the canary data and recreated the table from scratch (resetting
        # the SERIAL sequence) would reproduce the same value and pass falsely.
        token = uuid.uuid4().hex
        conn = self._open_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {table}")  # nosec B608 - table name is UUID-derived, not user input
                cur.execute(
                    f"CREATE TABLE {table} (id SERIAL PRIMARY KEY, marker TEXT NOT NULL, checkpoint_ref BIGINT NOT NULL, written_at TIMESTAMPTZ)"
                )  # nosec B608
                # checkpoint_ref=1 tracks that this row was written at prepare() time (ref=1)
                cur.execute(
                    f"INSERT INTO {table} (marker, checkpoint_ref, written_at) VALUES (%s, %s, now())",  # nosec B608
                    (token, 1),
                )
        finally:
            conn.close()
        return PersistenceState(id=identifier, ref=1, token=token)

    def checkpoint(self, expected: PersistenceState) -> tuple[ValidationResult, PersistenceState]:
        self._require_requires_role()
        if expected.ref < 1:
            # prepare() always returns ref=1 and checkpoint() only ever advances it, so a
            # restored/malformed PersistenceState with ref <= 0 can't have come from a real prior
            # run. Without this check, an empty or partially recreated table (actual == 0) could
            # satisfy `actual == expected.ref` for ref=0 and report a false PASS.
            raise ValueError(f"expected.ref {expected.ref} is out of range (expected >= 1)")
        table_name = self._canary_table_name(expected.id)
        marker = expected.token
        conn = self._open_connection()
        qualified_table: str | None = None
        try:
            with conn.cursor() as cur:
                # CREATE TABLE in prepare() is unqualified, so it resolves through search_path at
                # creation time - which may not match current_schema() here if search_path changed
                # since. Resolve the table's actual schema via information_schema rather than
                # assuming current_schema(), so this always addresses the same table prepare() made.
                schema = self._resolve_table_schema(cur, table_name)
                if schema is None:
                    matching = 0
                else:
                    qualified_table = f"{_quote_identifier(schema)}.{_quote_identifier(table_name)}"
                    # Require id = checkpoint_ref, not just a matching row count: SERIAL ids are
                    # assigned once and never reused, so a row deleted and replaced by a look-alike
                    # (same token, same checkpoint_ref) gets a fresh, out-of-sequence id - it fails
                    # this check even though a bare COUNT(*)/token match would not have caught it.
                    cur.execute(
                        f"SELECT COUNT(*) FROM {qualified_table} WHERE marker = %s "  # nosec B608
                        f"AND id = checkpoint_ref AND checkpoint_ref BETWEEN 1 AND %s",
                        (marker, expected.ref),
                    )
                    row = cur.fetchone()
                    matching = int(row[0]) if row else 0

                passed = matching == expected.ref
                # Only write the next canary row when this checkpoint passed: ValidatorRunner only
                # carries the advanced PersistenceState forward on a PASS result, so writing here
                # unconditionally would grow `actual` past what the harness will ever compare
                # against again - masking the original mismatch behind a permanent drift instead of
                # letting a later checkpoint re-detect the same data loss consistently.
                if passed and qualified_table is not None:
                    next_ref = expected.ref + 1
                    cur.execute(
                        f"INSERT INTO {qualified_table} (marker, checkpoint_ref, written_at) "  # nosec B608
                        f"VALUES (%s, %s, now())",
                        (marker, next_ref),
                    )
        finally:
            conn.close()

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

        ``cleanup()`` takes no state argument (see ``BasePersistenceValidator.cleanup``), so instead
        of dropping one table by identifier, every table matching this validator instance's canary
        name pattern is discovered via ``information_schema`` and dropped. This also mops up a
        canary table left behind by an interrupted run (e.g. a crash between ``prepare()`` and the
        next ``cleanup()``).

        Discovery is scoped to a model+relation+unit namespace (see ``_canary_table_prefix``)
        rather than the bare ``_CANARY_TABLE_PREFIX``: two concurrent ``postgresql_client``
        relations sharing the same database/schema - even across different models, whose relation
        IDs are assigned independently and so can collide numerically - can no longer drop each
        other's canary tables. The unit is part of that namespace because the harness runs
        persistence validators on every unit of the application, and those units share a model UUID
        and relation ID while owning separate canary tables: without it, cleanup on one unit would
        drop another unit's still-active table and make that unit's next ``checkpoint()`` report a
        data loss that never happened. The namespace is stable for the lifetime of a given relation
        on a given unit, so this still finds a table left behind by an interrupted run of *this*
        instance; it does not sweep up a stray table from a relation that was removed and re-added
        under a new ID, which is an accepted trade-off since a fresh ``prepare()`` for the new ID
        starts its own table anyway.

        The initial ``information_schema`` query only narrows candidates by *prefix* (a SQL ``LIKE``
        can't cheaply assert an exact suffix shape), so every candidate is re-checked against
        ``_canary_table_regex()`` - which requires the full ``prefix + fixed-width digits`` shape -
        and against ``_MAX_CANARY_IDENTIFIER`` before being dropped. The regex alone would still
        accept a shape-only look-alike such as ``..._99999999999999999999`` (20 nines), which is
        larger than any identifier ``prepare()`` can produce; the extra bound check rejects that
        too. This rejects a same-prefixed but unrelated table (e.g. a hand-created
        ``validator_canary_<token>_backup``) that a bare prefix match would otherwise destroy.
        """
        self._require_requires_role()
        # Check the same required fields _open_connection() validates, and no-op only when they're
        # genuinely absent; a malformed/unreachable connection still raises and surfaces as a real
        # ERROR. (Checking only "uris"/"secret-user" was a heuristic that didn't match what
        # _open_connection() actually requires, so a relation exposing "uris" before the rest of
        # those fields resolve would raise instead of no-op'ing, turning an in-progress relation
        # into a failed teardown.)
        creds = self._resolve_credentials()
        if not self.validate_schema(["uris", "database", "username", "password"], creds).passed:
            return
        conn = self._open_connection()
        try:
            with conn.cursor() as cur:
                # Escape LIKE metacharacters in the prefix: `_` and `%` are wildcards in a LIKE
                # pattern, and the relation-scoped prefix contains underscores, so without escaping
                # this could match unrelated tables (e.g. `validatorXcanaryY123_456`).
                prefix = self._canary_table_prefix()
                escaped_prefix = prefix.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
                # CREATE TABLE in prepare()/checkpoint() is unqualified, so it resolves through
                # search_path into the first writable schema (which may not be current_schema()).
                # Search all schemas to avoid leaving canary tables behind in other schemas. Also
                # restrict to base tables: a view or foreign table sharing the prefix would make
                # PostgreSQL reject DROP TABLE and abort cleanup, leaving the rest undropped.
                cur.execute(
                    "SELECT table_schema, table_name FROM information_schema.tables "
                    "WHERE table_type = 'BASE TABLE' "
                    "AND table_name LIKE %s ESCAPE '\\'",
                    (f"{escaped_prefix}%",),
                )
                tables = [(row[0], row[1]) for row in cur.fetchall()]
            name_regex = self._canary_table_regex()
            for schema, table in tables:
                match = name_regex.fullmatch(table)
                if not match or int(match.group("identifier")) > _MAX_CANARY_IDENTIFIER:
                    continue
                quoted_table = f"{_quote_identifier(schema)}.{_quote_identifier(table)}"
                with conn.cursor() as cur:
                    cur.execute(f"DROP TABLE IF EXISTS {quoted_table}")  # nosec B608 - identifier is safely quoted
        finally:
            conn.close()

    def _require_requires_role(self) -> None:
        if self.role != "requires":
            raise PersistenceNotApplicable(f"Role '{self.role}' is not supported by {self.__class__.__name__}.")

    def _open_connection(self) -> "psycopg2.extensions.connection":
        creds = self._resolve_credentials()
        data = self.databag | creds
        # Unlike the functional validator's validate()/deep(), these methods have no ValidationCheck
        # to report a schema failure through, so a missing/blank "uris" is raised rather than passed
        # to psycopg2 as an empty dsn: libpq treats dsn="" as "use local/default connection
        # parameters", which would silently create/check a canary against an unintended database.
        schema_check = self.validate_schema(["uris", "database", "username", "password"], creds)
        if not schema_check.passed:
            raise RuntimeError(f"Cannot open a connection for {self.endpoint}: {schema_check.message}")
        uri = data["uris"].split(",")[0].strip()
        if not uri:
            # validate_schema() only sees the raw "uris" string, so a non-blank value that is
            # still unusable once split/stripped (e.g. a leading comma) passes that check but would
            # otherwise reach _connect() as dsn="", which libpq silently treats as "use
            # local/default connection parameters".
            raise RuntimeError(f"Cannot open a connection for {self.endpoint}: first entry in 'uris' is blank")
        # Mirrors the database/URI consistency check _validate_simple()/_validate_deep() perform
        # before connecting: without it, a relation advertising uris=".../other_db" alongside a
        # stale/mismatched "database" field would silently write and verify canary data against
        # the wrong database, allowing persistence validation to pass for the wrong target.
        db_check = self._check_database_consistency(uri, data["database"])
        if not db_check.passed:
            raise RuntimeError(f"Cannot open a connection for {self.endpoint}: {db_check.message}")
        conn = self._connect(uri)
        conn.autocommit = True
        return conn

    def _canary_table_prefix(self) -> str:
        """Prefix scoped to this model, relation and unit, so cleanup discovery can't cross boundaries.

        ``self.relation_id`` is stable for the lifetime of a given relation (it only changes if the
        relation is removed and re-added), but relation IDs are assigned independently per model
        and can collide numerically across two different models relating to the same backend, and
        can themselves be arbitrarily long. The unit name is also part of the scope: the runner
        injects and runs persistence validators on *every* unit of the application, so two units of
        the same application share both ``model.uuid`` and ``relation_id`` while owning separate
        canary tables (see ``cleanup()``). Rather than embedding the raw ``relation_id`` (variable
        length) and a short model hash (narrow collision resistance) in the table name directly,
        all three are folded into a single fixed-width, collision-resistant ``scope_token``: the
        first 16 hex characters (64 bits) of a SHA-256 hash of
        ``f"{model_uuid}:{relation_id}:{unit_name}"``. A fixed-width token, combined with the
        fixed-width identifier written by ``_canary_table_name()``, lets ``cleanup()`` validate the
        *exact* shape of a table name (via ``_canary_table_regex()``) instead of relying on a
        prefix match alone, and keeps the total name within PostgreSQL's 63-byte identifier limit
        regardless of how large ``relation_id`` gets.
        """
        scope_token = self._canary_scope_token()
        return f"{_CANARY_TABLE_PREFIX}{scope_token}_"

    def _canary_scope_token(self) -> str:
        unit_name = self.charm.model.unit.name
        digest_input = f"{self.charm.model.uuid}:{self.relation_id}:{unit_name}".encode()
        return hashlib.sha256(digest_input).hexdigest()[:16]

    def _canary_table_regex(self) -> "re.Pattern[str]":
        """Exact-shape match for this relation's canary tables: prefix + fixed-width digits.

        Used by ``cleanup()`` to reject a table that merely shares the discovery prefix (e.g. a
        hand-created ``validator_canary_<token>_backup``) but doesn't match the fixed-width
        zero-padded identifier suffix ``_canary_table_name()`` always produces. Matching this
        shape alone isn't sufficient though - see ``cleanup()``, which additionally checks the
        captured ``identifier`` group against ``_MAX_CANARY_IDENTIFIER``.
        """
        return re.compile(re.escape(self._canary_table_prefix()) + r"(?P<identifier>[0-9]{20})")

    def _canary_table_name(self, identifier: int) -> str:
        # Zero-padded to a fixed 20 digits (identifier is masked to 63 bits in prepare(), so it
        # never exceeds 19 digits) so every canary table name has the same length and shape,
        # which _canary_table_regex() relies on to reject look-alike, unrelated tables. checkpoint()
        # passes back an identifier from a (possibly restored/malformed) PersistenceState rather
        # than a freshly masked one, so validate the range here too - an out-of-range value would
        # otherwise produce a name PostgreSQL could truncate or reject, causing checkpoint() to
        # silently read/write the wrong table instead of failing safely.
        if not 0 <= identifier <= _MAX_CANARY_IDENTIFIER:
            raise ValueError(
                f"canary identifier {identifier} is out of range " f"(expected 0..{_MAX_CANARY_IDENTIFIER})"
            )
        return f"{self._canary_table_prefix()}{identifier:020d}"

    def _resolve_table_schema(self, cur: "psycopg2.extensions.cursor", table_name: str) -> str | None:
        """Resolve the schema this validator's canary table currently lives in.

        ``CREATE TABLE`` in ``prepare()`` is unqualified, so PostgreSQL resolves it through
        ``search_path`` at creation time. Rather than re-deriving that same resolution later via
        ``pg_table_is_visible()`` - which depends on the *current* connection's ``search_path`` and
        so can disagree with prepare()'s if the path changed in between (e.g. a different role
        default, or a session-level override) - look the table up by its exact name across every
        schema in the database, independent of visibility. ``table_name`` is derived from a random,
        effectively-unique per-run identifier (see ``_canary_table_name``), so in the overwhelming
        common case exactly one table anywhere matches; that unambiguously identifies our canary
        regardless of search_path drift between calls.

        Only if more than one schema happens to contain a same-named table (e.g. a leftover canary
        from an earlier, interrupted run coincidentally reusing this run's random name - vanishingly
        unlikely, but not impossible) is there genuine ambiguity: in that case, fall back to
        ``pg_table_is_visible()`` to pick whichever match the *current* connection's unqualified
        name resolution would actually address, preserving the original tie-breaking behavior.
        """
        cur.execute(
            "SELECT n.nspname, pg_catalog.pg_table_is_visible(c.oid) FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname = %s AND c.relkind = 'r'",
            (table_name,),
        )
        rows = cur.fetchall()
        if not rows:
            return None
        if len(rows) == 1:
            return str(rows[0][0])
        visible = [schema for schema, is_visible in rows if is_visible]
        return visible[0] if visible else str(rows[0][0])
