"""
Validator for postgresql_client interface.

This validates ANY implementation of the postgresql_client interface contract
defined in charmlibs/interfaces/postgresql_client/interface/v0/schema.py.

In Phase 2, this will be published to:
    charmlibs-validators-postgresql-client

And distributed via PyPI.
"""

from typing import List

import psycopg2
# from charmlibs.interfaces.postgresql_client.v0 import schema

from .schema import schema

from validators.base import BaseValidator, ValidationCheck


class PostgreSQLClientValidator(BaseValidator):
    """
    Validator for postgresql_client interface implementations.

    Enforces compliance with:
    - Schema: charmlibs/interfaces/postgresql_client/interface/v0/schema.py
    - Behavior: Connection, authentication, read/write capabilities
    """

    interface_name = "postgresql_client"

    def _validate_schema(self, relation_data: dict):
        """Validate using postgresql_client schema from charmlibs."""
        return schema.PostgreSQLProviderData.parse_obj(relation_data)

    def _validate_l1(self) -> List[ValidationCheck]:
        """L1: Read-only connectivity and authentication checks (<5 seconds)."""
        checks = []

        try:
            # Parse connection details from validated schema
            endpoints = self.validated_data.endpoints
            host = endpoints.split(":")[0]
            port = 5432
            if ":" in endpoints:
                port = int(endpoints.split(":")[1])

            username = self.validated_data.username
            password = self.validated_data.password
            database = self.validated_data.database

            # Check 1: Connectivity
            def check_connect():
                conn = psycopg2.connect(
                    host=host, port=port, user=username, password=password, database=database, connect_timeout=5
                )
                conn.close()
                return "PASS", f"Successfully connected to {host}:{port}"

            checks.append(self._timed_check("connectivity", check_connect))

            # Check 2: Authentication & Version Query
            def check_auth():
                conn = psycopg2.connect(
                    host=host, port=port, user=username, password=password, database=database, connect_timeout=5
                )
                cursor = conn.cursor()
                cursor.execute("SELECT version()")
                version = cursor.fetchone()[0]
                conn.close()
                return "PASS", f"Authenticated as {username}"

            checks.append(self._timed_check("authentication", check_auth))

            # Check 3: Database Access
            def check_database():
                conn = psycopg2.connect(
                    host=host, port=port, user=username, password=password, database=database, connect_timeout=5
                )
                cursor = conn.cursor()
                cursor.execute("SELECT current_database()")
                db = cursor.fetchone()[0]
                conn.close()
                if db == database:
                    return "PASS", f"Access to database: {db}"
                return "FAIL", f"Expected database {database}, got {db}"

            checks.append(self._timed_check("database_access", check_database))

        except psycopg2.OperationalError as e:
            checks.append(self._error_check("connectivity", str(e)))
        except Exception as e:
            checks.append(self._error_check("validation", str(e)))

        return checks

    def _validate_l2(self) -> List[ValidationCheck]:
        """L2: L1 + Canary data write/read/cleanup (<60 seconds)."""
        from datetime import datetime

        # Start with L1 checks
        checks = self._validate_l1()

        # If L1 failed, don't attempt L2
        if any(c["status"] in ("FAIL", "ERROR") for c in checks):
            return checks

        conn = None
        canary_id = f"_juju_probe_{int(datetime.utcnow().timestamp() * 1000)}"

        try:
            # Parse connection details
            endpoints = self.validated_data.endpoints
            host = endpoints.split(":")[0]
            port = 5432
            if ":" in endpoints:
                port = int(endpoints.split(":")[1])

            username = self.validated_data.username
            password = self.validated_data.password
            database = self.validated_data.database

            conn = psycopg2.connect(
                host=host, port=port, user=username, password=password, database=database, connect_timeout=5
            )
            cursor = conn.cursor()

            # Check 4: Table Creation
            def check_table_creation():
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS _juju_probe_canary (
                        id TEXT PRIMARY KEY,
                        timestamp TIMESTAMP,
                        payload TEXT
                    )
                """)
                conn.commit()
                return "PASS", "Canary table ready"

            checks.append(self._timed_check("table_creation", check_table_creation))

            # Check 5: Write Transaction
            def check_write():
                cursor.execute(
                    "INSERT INTO _juju_probe_canary VALUES (%s, NOW(), %s)", (canary_id, f"test_payload_{canary_id}")
                )
                conn.commit()
                return "PASS", f"Wrote canary: {canary_id}"

            checks.append(self._timed_check("write_transaction", check_write))

            # Check 6: Read Transaction
            def check_read():
                cursor.execute("SELECT payload FROM _juju_probe_canary WHERE id = %s", (canary_id,))
                result = cursor.fetchone()
                if result and result[0] == f"test_payload_{canary_id}":
                    return "PASS", "Canary data verified"
                return "FAIL", "Canary data mismatch"

            checks.append(self._timed_check("read_transaction", check_read))

            # Check 7: Cleanup
            def check_cleanup():
                cursor.execute("DELETE FROM _juju_probe_canary WHERE id = %s", (canary_id,))
                conn.commit()
                return "PASS", "Canary data removed"

            checks.append(self._timed_check("cleanup", check_cleanup))

            conn.close()

        except Exception as e:
            # Attempt cleanup on failure
            if conn:
                try:
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM _juju_probe_canary WHERE id = %s", (canary_id,))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass  # Best effort cleanup

            checks.append(self._error_check("write_read_cycle", str(e)))

        return checks
