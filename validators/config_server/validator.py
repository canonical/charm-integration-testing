# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Validator for the config-server Juju interface.

The config-server interface connects mongodb-k8s (provider/config-server role)
to mongos-k8s (requirer/router role) in a sharded MongoDB deployment.

Interface name:  config-server
Endpoint name:   cluster (on both sides)
Provider:        mongodb-k8s  — publishes cluster connectivity info
Requirer:        mongos-k8s   — requests a database and reports its role

Provider app databag fields (written by mongodb-k8s / config-server):
    config-server-db    Replicaset seed URI: {replicaSet}/{host}:27017,...
                        May be stored as a Juju secret (value = secret URI).
    key-file            Internal cluster keyfile for MongoDB auth.
                        May be stored as a Juju secret.
    username            Username for the mongos router user.
    password            Password for the mongos router user.
                        Credentials may be stored under a ``secret-user`` key.
    int-ca-secret       Internal TLS CA PEM content (optional, Juju secret).
    ext-ca-secret       External TLS CA PEM content (optional, Juju secret).

Requirer app databag fields (written by mongos-k8s):
    database            Requested database name.
    extra-user-roles    Comma-separated MongoDB roles (e.g. ``admin``).
"""

import re
import socket
import time
from typing import Any
from urllib.parse import quote_plus

from pymongo import MongoClient
from pymongo.errors import PyMongoError

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
    ValidationResultStatus,
)

# Pattern: {replicaSetName}/{host}:{port}[,{host}:{port}...]
# The replicaset name and hostnames may contain letters, digits, hyphens, dots,
# and underscores.  At least one host:port pair must follow the slash.
_CONFIG_SERVER_DB_RE = re.compile(r"^[A-Za-z0-9_\-]+/[A-Za-z0-9_\-\.]+:\d+(?:,[A-Za-z0-9_\-\.]+:\d+)*$")

_CONNECT_TIMEOUT_MS = 5_000
_SERVER_SELECTION_TIMEOUT_MS = 5_000
_SOCKET_TIMEOUT_MS = 10_000


class ConfigServerValidator(BaseValidator):
    """Validates the config-server relation contract for MongoDB sharding."""

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role == "requires":
            if level == "simple":
                return self._validate_requires_simple()
            if level == "deep":
                return self._validate_requires_deep()
            return self._skipped_result_due_to_level(level)

        if self.role == "provides":
            if level == "simple":
                return self._validate_provides_simple()
            return self._skipped_result_due_to_level(level)

        return self._skipped_result_due_to_role(level, self.role)

    # ------------------------------------------------------------------
    # Requires role — mongos-k8s reads config-server provider databag
    # ------------------------------------------------------------------

    def _validate_requires_simple(self) -> ValidationResult:
        """L1: Schema, config-server-db format, and TCP reachability."""
        checks: list[ValidationCheck] = []

        error = self._check_relation_exists("simple")
        if error:
            return error

        creds = self._resolve_credentials()
        extra = self._resolve_extra_secrets()
        merged = {**creds, **extra}

        schema_check = self.validate_schema(
            ["config-server-db", "key-file", "username", "password"],
            merged,
        )
        checks.append(schema_check)
        if not schema_check.passed:
            return self._build_result("simple", checks)

        config_server_db = merged.get("config-server-db", "")

        format_check = _check_config_server_db_format(config_server_db)
        checks.append(format_check)
        if not format_check.passed:
            return self._build_result("simple", checks)

        hosts = _parse_hosts(config_server_db)
        checks.append(_tcp_connectivity_check(hosts))

        return self._build_result("simple", checks)

    def _validate_requires_deep(self) -> ValidationResult:
        """L2: All L1 checks + authenticated pymongo ping and database list."""
        start_time = time.monotonic()
        timeout_secs = 15
        checks: list[ValidationCheck] = []

        error = self._check_relation_exists("deep")
        if error:
            return error

        creds = self._resolve_credentials()
        extra = self._resolve_extra_secrets()
        merged = {**creds, **extra}

        schema_check = self.validate_schema(
            ["config-server-db", "key-file", "username", "password"],
            merged,
        )
        checks.append(schema_check)
        if not schema_check.passed:
            return self._build_result("deep", checks)

        config_server_db = merged.get("config-server-db", "")

        format_check = _check_config_server_db_format(config_server_db)
        checks.append(format_check)
        if not format_check.passed:
            return self._build_result("deep", checks)

        hosts = _parse_hosts(config_server_db)
        tcp_check = _tcp_connectivity_check(hosts)
        checks.append(tcp_check)
        if not tcp_check.passed:
            return self._build_result("deep", checks)

        replica_set = _parse_replica_set(config_server_db)
        username = merged.get("username", "")
        password = merged.get("password", "")

        client: MongoClient[Any] | None = None
        try:
            client = _build_mongo_client(hosts, replica_set, username, password)

            ping_check = _ping_check(client, hosts[0])
            checks.append(ping_check)
            if not ping_check.passed:
                return self._build_result("deep", checks)

            list_check = _list_databases_check(client)
            checks.append(list_check)
        finally:
            if client is not None:
                client.close()

        elapsed = time.monotonic() - start_time
        checks.append(
            ValidationCheck(
                name="latency",
                passed=elapsed <= timeout_secs,
                message=(
                    f"Deep validation completed in {elapsed:.1f}s."
                    if elapsed <= timeout_secs
                    else f"Deep validation took {elapsed:.1f}s, exceeded {timeout_secs}s limit."
                ),
            )
        )

        return self._build_result("deep", checks)

    # ------------------------------------------------------------------
    # Provides role — config-server reads mongos requirer databag
    # ------------------------------------------------------------------

    def _validate_provides_simple(self) -> ValidationResult:
        """L1: Verify the requirer (mongos) has published required fields."""
        checks: list[ValidationCheck] = []

        error = self._check_relation_exists("simple")
        if error:
            return error

        schema_check = self.validate_schema(["database"])
        checks.append(schema_check)
        if not schema_check.passed:
            return self._build_result("simple", checks)

        if "extra-user-roles" in self.databag:
            roles_check = _check_extra_user_roles(self.databag["extra-user-roles"])
            checks.append(roles_check)

        return self._build_result("simple", checks)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _check_relation_exists(self, level: ValidationLevel) -> ValidationResult | None:
        if not self.relation_exists():
            return self._make_result(
                status="ERROR",
                level=level,
                error=f"No remote application on relation '{self.endpoint}'.",
            )
        return None

    def _resolve_credentials(self) -> dict[str, str]:
        """Resolve username/password from the relation databag or a Juju secret.

        The config-server provider publishes credentials via the standard
        ``secret-user`` key (a Juju secret URI whose content contains
        ``username`` and ``password``), or as plain fields in the databag.
        """
        return self.resolve_secret("secret-user", "username", "password")

    def _resolve_extra_secrets(self) -> dict[str, str]:
        """Resolve config-server-db and key-file from the relation databag or a Juju secret.

        ``data_platform_libs`` bundles ``additional_secret_fields`` under a
        ``secret-extra`` pointer in the provider app databag.  The secret
        content contains ``config-server-db`` and ``key-file`` as keys.
        """
        return self.resolve_secret("secret-extra", "config-server-db", "key-file")

    def _build_result(self, level: ValidationLevel, checks: list[ValidationCheck]) -> ValidationResult:
        status: ValidationResultStatus = "PASS" if all(c.passed for c in checks) else "FAIL"
        return self._make_result(status=status, level=level, checks=checks)


# ---------------------------------------------------------------------------
# Pure helpers — format validation
# ---------------------------------------------------------------------------


def _check_config_server_db_format(config_server_db: str) -> ValidationCheck:
    """Validate the ``config-server-db`` value matches ``{replicaSet}/{host}:{port},...``.

    An invalid format prevents mongos from locating the config server and is
    a hard prerequisite for all further checks.  Remediation: verify that
    mongodb-k8s has fully initialised and the relation data has been written.
    """
    if not config_server_db:
        return ValidationCheck(
            name="config_server_db_format",
            passed=False,
            message="'config-server-db' is empty. Ensure mongodb-k8s has completed initialisation.",
        )
    if not _CONFIG_SERVER_DB_RE.match(config_server_db):
        return ValidationCheck(
            name="config_server_db_format",
            passed=False,
            message=(
                f"'config-server-db' value {config_server_db!r} does not match the expected "
                "format '{replicaSetName}/{host}:{port},...'. "
                "Remediation: check that mongodb-k8s is active/idle and the cluster relation is established."
            ),
        )
    return ValidationCheck(
        name="config_server_db_format",
        passed=True,
        message=f"Format OK: {config_server_db!r}.",
    )


def _check_extra_user_roles(roles_raw: str) -> ValidationCheck:
    """Validate that ``extra-user-roles`` is non-empty and contains valid role tokens."""
    roles = [r.strip() for r in roles_raw.split(",") if r.strip()]
    if not roles:
        return ValidationCheck(
            name="extra_user_roles",
            passed=False,
            message=(
                "'extra-user-roles' is empty or missing. "
                "Remediation: ensure mongos-k8s sets a non-empty roles list (e.g. 'admin')."
            ),
        )
    return ValidationCheck(
        name="extra_user_roles",
        passed=True,
        message=f"Roles: {', '.join(roles)}.",
    )


# ---------------------------------------------------------------------------
# Pure helpers — network
# ---------------------------------------------------------------------------


def _parse_hosts(config_server_db: str) -> list[tuple[str, int]]:
    """Return a list of ``(host, port)`` tuples from a config-server-db string.

    Input format: ``{replicaSet}/{host}:{port}[,{host}:{port}...]``
    """
    _, _, hosts_part = config_server_db.partition("/")
    result: list[tuple[str, int]] = []
    for token in hosts_part.split(","):
        token = token.strip()
        if ":" in token:
            host, _, port_str = token.rpartition(":")
            try:
                result.append((host, int(port_str)))
            except ValueError:
                pass
    return result


def _parse_replica_set(config_server_db: str) -> str:
    """Extract the replica-set name from a config-server-db string."""
    return config_server_db.split("/")[0]


def _tcp_connectivity_check(hosts: list[tuple[str, int]]) -> ValidationCheck:
    """TCP-ping every config-server host; report a single pass/fail check.

    A FAIL here means mongos cannot reach the config server at the network
    level.  Remediation: verify DNS resolution and network policies between
    the mongos and mongodb-k8s pods.
    """
    errors: list[str] = []
    for host, port in hosts:
        try:
            with socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT_MS / 1000):
                pass
        except OSError as exc:
            errors.append(f"{host}:{port} — {exc}")
    if errors:
        return ValidationCheck(
            name="tcp_connect",
            passed=False,
            message=(
                f"Cannot reach config-server host(s): {'; '.join(errors)}. "
                "Remediation: check network policies and DNS between mongos-k8s and mongodb-k8s pods."
            ),
        )
    return ValidationCheck(
        name="tcp_connect",
        passed=True,
        message=f"TCP reachable: {len(hosts)} host(s).",
    )


# ---------------------------------------------------------------------------
# Pure helpers — pymongo
# ---------------------------------------------------------------------------


def _build_mongo_client(
    hosts: list[tuple[str, int]],
    replica_set: str,
    username: str,
    password: str,
) -> "MongoClient[Any]":
    """Build a pymongo MongoClient for the config-server replica set."""
    host_list = [f"{h}:{p}" for h, p in hosts]
    uri = (
        f"mongodb://{quote_plus(username)}:{quote_plus(password)}"
        f"@{','.join(host_list)}/admin"
        f"?replicaSet={replica_set}&authSource=admin"
    )
    return MongoClient(
        uri,
        serverSelectionTimeoutMS=_SERVER_SELECTION_TIMEOUT_MS,
        connectTimeoutMS=_CONNECT_TIMEOUT_MS,
        socketTimeoutMS=_SOCKET_TIMEOUT_MS,
        directConnection=False,
        appname="config-server-validator",
    )


def _ping_check(client: "MongoClient[Any]", primary_host: tuple[str, int]) -> ValidationCheck:
    """Issue a MongoDB ``ping`` command to verify authentication and connectivity."""
    host_str = f"{primary_host[0]}:{primary_host[1]}"
    try:
        client.admin.command("ping")
        return ValidationCheck(
            name="ping",
            passed=True,
            message=f"MongoDB ping succeeded (replica set seed: {host_str}).",
        )
    except PyMongoError as exc:
        return ValidationCheck(
            name="ping",
            passed=False,
            message=(
                f"MongoDB ping failed against {host_str}: {exc}. "
                "Remediation: verify credentials and that the config-server replica set is healthy."
            ),
        )


def _list_databases_check(client: "MongoClient[Any]") -> ValidationCheck:
    """List databases as a canary read to verify end-to-end access."""
    try:
        db_names = client.list_database_names()
        return ValidationCheck(
            name="list_databases",
            passed=True,
            message=f"Listed {len(db_names)} database(s): {', '.join(sorted(db_names)) or '(none)'}.",
        )
    except PyMongoError as exc:
        return ValidationCheck(
            name="list_databases",
            passed=False,
            message=(
                f"listDatabases failed: {exc}. "
                "Remediation: verify the mongos router user has the 'admin' or 'listDatabases' privilege."
            ),
        )
