# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import hashlib
import re
import time
import uuid

import pymysql

from validators.base import (
    BasePersistenceValidator,
    BaseValidator,
    PersistenceNotApplicable,
    PersistenceState,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)
from validators.mysql_client.connection import REQUIRED_CREDENTIAL_FIELDS, _MySQLConnectionMixin


class MySQLClientValidator(_MySQLConnectionMixin, BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)

        if level == "uat":
            return self._skipped_result_due_to_level(level)

        if level == "simple":
            return self._validate_simple()
        elif level == "deep":
            return self._validate_deep()
        else:
            return self._skipped_result_due_to_level(level)

    def _validate_simple(self) -> ValidationResult:
        """L1: Connectivity & auth with read-only canary query."""
        checks: list[ValidationCheck] = []

        # --- 1. Remote app presence ---
        error_result = self._check_relation_exists("simple")
        if error_result:
            return error_result

        # --- 2. Resolve credentials (plain fields or Juju secrets) ---
        creds = self._resolve_credentials()

        # --- 3. Schema check ---
        schema_check = self.validate_schema(REQUIRED_CREDENTIAL_FIELDS, creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="simple", checks=checks)

        # --- 4. Connect ---
        data = self.databag | creds
        try:
            conn = self._connect(data)
            host, port = self._first_endpoint(data)
            checks.append(ValidationCheck(name="connect", passed=True, message=f"Connected to {host}:{port}."))
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=str(exc)))
            return self._make_result(level="simple", checks=checks)

        # --- 5. Canary read-only query ---
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                checks.append(ValidationCheck(name="query", passed=True, message="SELECT 1 OK."))
        except Exception as exc:
            checks.append(ValidationCheck(name="query", passed=False, message=str(exc)))
        else:
            # --- 6. Optional server version check (only when query succeeded) ---
            try:
                with conn.cursor() as cur:
                    version_check = self._check_server_version(cur, data)
                    if version_check is not None:
                        checks.append(version_check)
            except Exception as exc:
                checks.append(ValidationCheck(name="version_consistency", passed=False, message=str(exc)))
        finally:
            conn.close()

        return self._make_result(level="simple", checks=checks)

    def _validate_deep(self) -> ValidationResult:
        """L2: Read/write capability with canary table (create, write, read-verify, cleanup)."""
        timeout_secs = 10
        checks: list[ValidationCheck] = []

        # --- 1. Remote app presence ---
        error_result = self._check_relation_exists("deep")
        if error_result:
            return error_result

        # --- 2. Resolve credentials (plain fields or Juju secrets) ---
        creds = self._resolve_credentials()

        # --- 3. Schema check ---
        schema_check = self.validate_schema(REQUIRED_CREDENTIAL_FIELDS, creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="deep", checks=checks)

        # --- 4. Connect ---
        # Latency is timed from here, not from the top of the function, so that
        # Juju secret/relation-data resolution (steps 1-3) - which can be slow for
        # cross-model relations independent of the database itself - is not counted
        # against the database round-trip budget below.
        start_time = time.monotonic()
        data = self.databag | creds
        try:
            conn = self._connect(data)
            conn.autocommit(True)
            host, port = self._first_endpoint(data)
            checks.append(ValidationCheck(name="connect", passed=True, message=f"Connected to {host}:{port}."))
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=str(exc)))
            return self._make_result(level="deep", checks=checks)

        # --- 5. Canary read-only query ---
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                checks.append(ValidationCheck(name="query", passed=True, message="SELECT 1 OK."))
        except Exception as exc:
            checks.append(ValidationCheck(name="query", passed=False, message=str(exc)))
            conn.close()
            return self._make_result(level="deep", checks=checks)

        # --- 6. Create canary table, write, read-verify, cleanup ---
        canary_table = f"__canary_{uuid.uuid4().hex[:8]}"
        try:
            with conn.cursor() as cur:
                # Create
                cur.execute(  # nosec B608 - table name is UUID-generated
                    f"CREATE TABLE {canary_table} (id INT AUTO_INCREMENT PRIMARY KEY, marker VARCHAR(255) NOT NULL)"
                )

                # Write
                insert_query = f"INSERT INTO {canary_table} (marker) VALUES (%s)"  # nosec B608 - table name is UUID-generated
                cur.execute(insert_query, ("validator-probe",))
                inserted_id = cur.lastrowid

                # Read-verify
                select_query = f"SELECT marker FROM {canary_table} WHERE id = %s"  # nosec B608 - table name is UUID-generated
                cur.execute(select_query, (inserted_id,))
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
        except Exception as exc:
            checks.append(
                ValidationCheck(
                    name="write_read_verify",
                    passed=False,
                    message=str(exc),
                )
            )

        # --- 7. Cleanup: drop canary table ---
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

        # --- 8. Latency check ---
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

    def _check_server_version(self, cur: "pymysql.cursors.Cursor", data: dict[str, str]) -> ValidationCheck | None:
        """Verify the databag `version` field matches the server-reported version. None when absent."""
        expected_version = data.get("version", "").strip()
        if not expected_version:
            return None
        cur.execute("SELECT VERSION()")
        row = cur.fetchone()
        actual_version = str(row[0]) if row else ""
        if actual_version.startswith(expected_version):
            return ValidationCheck(
                name="version_consistency",
                passed=True,
                message=f"Server version '{actual_version}' matches databag 'version' field.",
            )
        return ValidationCheck(
            name="version_consistency",
            passed=False,
            message=f"Server version '{actual_version}' does not match databag 'version' field '{expected_version}'.",
        )


# Table name prefix for persistence-validator canary tables. Kept as a module constant so
# cleanup() (which has no per-call state to work from) can discover every canary table it may have
# created by pattern rather than by identifier.
_CANARY_TABLE_PREFIX = "validator_canary_"

# prepare() masks its identifier to 63 bits, so a genuine canary identifier never exceeds this
# value. cleanup()'s discovery regex only checks a candidate table name's *shape* (prefix + 20
# digits); this bound lets it also reject an out-of-range look-alike that prepare() couldn't have
# produced.
_MAX_CANARY_IDENTIFIER = (1 << 63) - 1
_MAX_CHECKPOINT_REF = (1 << 63) - 1

# MySQL's LIKE treats backslash as the default escape character *inside string literals too*, so a
# backslash escape would have to be written doubled in the SQL text. Use an escape character that
# needs no such doubling instead.
_LIKE_ESCAPE_CHAR = "|"


def _quote_identifier(name: str) -> str:
    """Safely quote a SQL identifier for interpolation into a query string.

    Doubling embedded backticks is the standard way to escape a MySQL identifier, and works
    regardless of whether ANSI_QUOTES is enabled (unlike double-quoting).
    """
    return "`" + name.replace("`", "``") + "`"


class MySQLClientPersistenceValidator(_MySQLConnectionMixin, BasePersistenceValidator):
    """Durability probe for the mysql_client interface, mirroring the postgresql_client reference.

    Each validator instance owns a dedicated canary table named
    ``validator_canary_{scope_token}_{identifier}``, where ``scope_token`` is a fixed-width hash of
    this model's UUID, relation ID and unit name and ``identifier`` is a fixed-width, zero-padded
    value chosen by ``prepare()`` and carried forward by the test harness as ``PersistenceState.id``.
    Both components have a fixed length so ``cleanup()`` can validate the *exact* shape of a
    candidate table name before dropping it. Every row also carries a random ``token`` generated by
    ``prepare()``, so ``checkpoint()`` can detect data loss (or a table silently recreated from
    scratch) by counting only rows tagged with that token rather than trusting a plain ``count(*)``.

    Persistence only applies to the requirer side (the side holding credentials to connect out);
    the provider side raises ``PersistenceNotApplicable``.
    """

    def prepare(self) -> PersistenceState:
        self._require_requires_role()
        # Masked to 63 bits (rather than the full 128-bit uuid4().int) so the canary table name -
        # which also carries a fixed-width scope token - stays within MySQL's 64-character
        # identifier limit, while still leaving far more entropy than a test run could collide on.
        identifier = uuid.uuid4().int & _MAX_CANARY_IDENTIFIER
        table = _quote_identifier(self._canary_table_name(identifier))
        # Random per-run token written to every canary row and matched on by checkpoint(). It must
        # not be derivable from `identifier`/`ref`: those are reproducible, so a recreated table
        # must not be able to reproduce the canary marker from its name or checkpoint sequence.
        token = uuid.uuid4().hex
        conn = self._open_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {table}")  # nosec B608 - identifier is safely quoted
                cur.execute(
                    f"CREATE TABLE {table} (id BIGINT PRIMARY KEY, "  # nosec B608
                    "marker VARCHAR(255) NOT NULL, checkpoint_ref BIGINT NOT NULL, written_at DATETIME)"
                )
                # checkpoint_ref=1 tracks that this row was written at prepare() time (ref=1)
                cur.execute(
                    f"INSERT INTO {table} (id, marker, checkpoint_ref, written_at) "  # nosec B608
                    "VALUES (%s, %s, %s, NOW())",
                    (1, token, 1),
                )
        finally:
            conn.close()
        return PersistenceState(id=identifier, ref=1, token=token)

    def checkpoint(self, expected: PersistenceState) -> tuple[ValidationResult, PersistenceState]:
        self._require_requires_role()
        if not 1 <= expected.ref < _MAX_CHECKPOINT_REF:
            # prepare() always returns ref=1 and checkpoint() only ever advances it, so a
            # restored/malformed PersistenceState outside the signed BIGINT range cannot have
            # come from a real prior run. Reject it before formatting queries or attempting the
            # next insert, which would otherwise overflow the backend column.
            raise ValueError(f"expected.ref {expected.ref} is out of range " f"(expected 1..{_MAX_CHECKPOINT_REF - 1})")
        table_name = self._canary_table_name(expected.id)
        marker = expected.token
        conn = self._open_connection()
        try:
            with conn.cursor() as cur:
                schema = self._resolve_table_schema(cur, table_name)
                if schema is None:
                    matching = 0
                    qualified_table = None
                else:
                    qualified_table = f"{_quote_identifier(schema)}.{_quote_identifier(table_name)}"
                    # Require id = checkpoint_ref, not just a matching row count. Explicit IDs
                    # keep this invariant independent of server AUTO_INCREMENT settings.
                    cur.execute(
                        f"SELECT COUNT(*) FROM {qualified_table} "  # nosec B608
                        "WHERE CAST(marker AS BINARY) = CAST(%s AS BINARY) "
                        "AND id = checkpoint_ref AND checkpoint_ref BETWEEN 1 AND %s",
                        (marker, expected.ref),
                    )
                    row = cur.fetchone()
                    matching = int(row[0]) if row else 0

                passed = matching == expected.ref
                # Only write the next canary row when this checkpoint passed: ValidatorRunner only
                # carries the advanced PersistenceState forward on a PASS result, so writing here
                # unconditionally would grow the actual row count past what the harness will ever
                # compare against again, masking the mismatch behind permanent drift.
                if passed and qualified_table is not None:
                    cur.execute(
                        f"INSERT INTO {qualified_table} (id, marker, checkpoint_ref, written_at) "  # nosec B608
                        "VALUES (%s, %s, %s, NOW())",
                        (expected.ref + 1, marker, expected.ref + 1),
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

        ``cleanup()`` takes no state argument, so every table matching this instance's canary name
        pattern is discovered via ``information_schema`` and dropped. This also mops up a table left
        behind by an interrupted run (e.g. a crash between ``prepare()`` and the next ``cleanup()``).

        Discovery is scoped to a model+relation+unit namespace (see ``_canary_table_prefix``) so
        concurrent relations sharing a database can't drop each other's tables. It does not sweep up
        a stray table from a relation removed and re-added under a new ID - an accepted trade-off,
        since a fresh ``prepare()`` for the new ID starts its own table anyway.

        The ``information_schema`` query only narrows candidates by *prefix*, so every candidate is
        re-checked against ``_canary_table_regex()`` and ``_MAX_CANARY_IDENTIFIER`` before being
        dropped. This rejects a same-prefixed but unrelated table (e.g. a hand-created
        ``..._backup``) that a bare prefix match would otherwise destroy.
        """
        self._require_requires_role()
        # Check the same required fields _open_connection() validates, so a relation that never
        # received credentials is skipped rather than reporting a successful cleanup that removed
        # nothing.
        creds = self._resolve_credentials()
        if not self.validate_schema(REQUIRED_CREDENTIAL_FIELDS, creds).passed:
            raise PersistenceNotApplicable(
                "Relation credentials are incomplete; cleanup cannot remove canary data yet."
            )
        conn = self._open_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_schema, table_name FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' "
                    f"AND table_name LIKE %s ESCAPE '{_LIKE_ESCAPE_CHAR}'",  # nosec B608
                    (f"{self._canary_like_prefix()}%",),
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

    def _open_connection(self) -> "pymysql.connections.Connection":
        creds = self._resolve_credentials()
        data = self.databag | creds
        # Unlike the functional validator's validate(), these methods have no ValidationCheck to
        # report a schema failure through, so incomplete credentials are raised rather than passed
        # to PyMySQL, which would otherwise connect somewhere unintended or fail obscurely.
        schema_check = self.validate_schema(REQUIRED_CREDENTIAL_FIELDS, creds)
        if not schema_check.passed:
            raise RuntimeError(f"Cannot open a connection for {self.endpoint}: {schema_check.message}")
        host, _ = self._first_endpoint(data)
        if not host:
            # validate_schema() only sees the raw "endpoints" string, so a non-blank value that is
            # still unusable once split/stripped (e.g. a leading comma) passes that check but would
            # otherwise reach PyMySQL as an empty host, which it silently resolves to localhost.
            raise RuntimeError(f"Cannot open a connection for {self.endpoint}: first entry in 'endpoints' is blank")
        conn = self._connect(data)
        try:
            # Canary writes are useless if rolled back when the connection closes. DDL commits
            # implicitly in MySQL, but INSERTs do not.
            conn.autocommit(True)
        except Exception:
            conn.close()
            raise
        return conn

    def _canary_table_prefix(self) -> str:
        """Prefix scoped to this model, relation and unit, so cleanup discovery can't cross boundaries.

        ``self.relation_id`` is stable for the lifetime of a given relation, but relation IDs are
        assigned independently per model and can collide numerically across two models relating to
        the same backend. The unit name is also part of the scope: the runner runs persistence
        validators on *every* unit of the application, so two units share both ``model.uuid`` and
        ``relation_id`` while owning separate canary tables. All three are folded into a single
        fixed-width ``scope_token`` so the name stays within MySQL's 64-character identifier limit
        and ``cleanup()`` can validate its exact shape via ``_canary_table_regex()``.
        """
        return f"{_CANARY_TABLE_PREFIX}{self._canary_scope_token()}_"

    def _canary_like_prefix(self) -> str:
        """The discovery prefix with LIKE wildcards escaped.

        The prefix contains underscores, which are single-character wildcards in a LIKE pattern, so
        an unescaped pattern could match unrelated tables (e.g. ``validatorXcanaryY...``).
        """
        prefix = self._canary_table_prefix()
        for char in (_LIKE_ESCAPE_CHAR, "_", "%"):
            prefix = prefix.replace(char, f"{_LIKE_ESCAPE_CHAR}{char}")
        return prefix

    def _canary_scope_token(self) -> str:
        unit_name = self.charm.model.unit.name
        digest_input = f"{self.charm.model.uuid}:{self.relation_id}:{unit_name}".encode()
        return hashlib.sha256(digest_input).hexdigest()[:16]

    def _canary_table_regex(self) -> "re.Pattern[str]":
        """Exact-shape match for this relation's canary tables: prefix + fixed-width digits.

        Used by ``cleanup()`` to reject a table that merely shares the discovery prefix (e.g. a
        hand-created ``..._backup``). Matching this shape alone isn't sufficient - see ``cleanup()``,
        which also checks the captured ``identifier`` against ``_MAX_CANARY_IDENTIFIER``.
        """
        return re.compile(re.escape(self._canary_table_prefix()) + r"(?P<identifier>[0-9]{20})")

    def _canary_table_name(self, identifier: int) -> str:
        # Zero-padded to a fixed 20 digits (prepare() masks identifiers to 63 bits, so never more
        # than 19) so every canary name has the same shape, which _canary_table_regex() relies on.
        # checkpoint() passes back an identifier from a possibly restored/malformed state, so
        # range-check it here too rather than letting MySQL truncate or reject the name.
        if not 0 <= identifier <= _MAX_CANARY_IDENTIFIER:
            raise ValueError(f"canary identifier {identifier} is out of range (expected 0..{_MAX_CANARY_IDENTIFIER})")
        return f"{self._canary_table_prefix()}{identifier:020d}"

    def _resolve_table_schema(self, cur: "pymysql.cursors.Cursor", table_name: str) -> str | None:
        """Resolve the schema holding this validator's canary table, or None when it is gone.

        MySQL has no ``search_path``: an unqualified ``CREATE TABLE`` always lands in the
        connection's default database, so discovery is scoped to ``DATABASE()`` rather than searching
        every schema as the PostgreSQL implementation must.
        """
        cur.execute(
            "SELECT table_schema FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' AND table_name = %s",
            (table_name,),
        )
        row = cur.fetchone()
        return str(row[0]) if row else None
