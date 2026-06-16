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

import os
import tempfile
import uuid

from opensearchpy import OpenSearch, RequestsHttpConnection
from opensearchpy.exceptions import ConnectionError as OSConnectionError
from opensearchpy.exceptions import OpenSearchException

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
    ValidationResultStatus,
)

_CONNECT_TIMEOUT = 5
_REQUEST_TIMEOUT = 10
_HEALTHY_STATUSES = {"green", "yellow"}
_REQUIRED_FIELDS = ["endpoints", "username", "password"]


class OpenSearchClientValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level == "uat":
            return self._skipped_result_due_to_level(level)
        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")
        if level == "simple":
            return self._validate_simple()
        if level == "deep":
            return self._validate_deep()
        return self._skipped_result_due_to_level(level)

    # ------------------------------------------------------------------
    # L1: Schema + cluster connectivity + health
    # ------------------------------------------------------------------

    def _validate_simple(self) -> ValidationResult:
        """L1: Resolve credentials, connect to cluster, confirm health is green or yellow."""
        checks: list[ValidationCheck] = []
        creds = self._resolve_credentials()

        schema_check = self.validate_schema(_REQUIRED_FIELDS, creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._build_result("simple", checks)

        client = self._build_client(creds)
        try:
            health_check = self._check_cluster_health(client)
            checks.append(health_check)
        finally:
            self._close_client(client)

        return self._build_result("simple", checks)

    # ------------------------------------------------------------------
    # L2: Schema + health + canary index (create → index → get → delete)
    # ------------------------------------------------------------------

    def _validate_deep(self) -> ValidationResult:
        """L2: All L1 checks, then create a canary index, write and retrieve a document."""
        checks: list[ValidationCheck] = []
        creds = self._resolve_credentials()

        schema_check = self.validate_schema(_REQUIRED_FIELDS, creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._build_result("deep", checks)

        client = self._build_client(creds)
        try:
            health_check = self._check_cluster_health(client)
            checks.append(health_check)
            if not health_check.passed:
                return self._build_result("deep", checks)

            checks.extend(self._check_canary_index(client))
        finally:
            self._close_client(client)

        return self._build_result("deep", checks)

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------

    def _check_cluster_health(self, client: OpenSearch) -> ValidationCheck:
        """GET /_cluster/health and confirm status is green or yellow."""
        try:
            health = client.cluster.health(request_timeout=_REQUEST_TIMEOUT)
            status = health.get("status", "unknown")
            if status in _HEALTHY_STATUSES:
                return ValidationCheck(
                    name="cluster_health",
                    passed=True,
                    message=f"Cluster health is '{status}'.",
                )
            return ValidationCheck(
                name="cluster_health",
                passed=False,
                message=f"Cluster health is '{status}'; expected green or yellow.",
            )
        except (OSConnectionError, OpenSearchException, Exception) as exc:
            return ValidationCheck(name="cluster_health", passed=False, message=str(exc))

    def _check_canary_index(self, client: OpenSearch) -> list[ValidationCheck]:
        """Write a canary document into the granted index, retrieve it, then delete it."""
        checks: list[ValidationCheck] = []
        index_name = self.databag.get("index", "")
        if not index_name:
            return [ValidationCheck(name="index_document", passed=False, message="No 'index' in relation databag.")]
        doc_id = f"validator-canary-{uuid.uuid4().hex[:8]}"
        doc_body = {"validator": "opensearch_client", "canary": True}

        # 1. Index document
        try:
            client.index(index=index_name, id=doc_id, body=doc_body, request_timeout=_REQUEST_TIMEOUT)
            checks.append(ValidationCheck(name="index_document", passed=True, message="Document indexed."))
        except (OpenSearchException, Exception) as exc:
            checks.append(ValidationCheck(name="index_document", passed=False, message=str(exc)))
            return checks

        try:
            # 2. Retrieve document
            try:
                result = client.get(index=index_name, id=doc_id, request_timeout=_REQUEST_TIMEOUT)
                retrieved = result.get("_source", {})
                if retrieved.get("canary") is True:
                    checks.append(
                        ValidationCheck(name="document_get", passed=True, message="Document retrieved and verified.")
                    )
                else:
                    checks.append(
                        ValidationCheck(
                            name="document_get",
                            passed=False,
                            message=f"Retrieved document does not match: {retrieved}",
                        )
                    )
            except (OpenSearchException, Exception) as exc:
                checks.append(ValidationCheck(name="document_get", passed=False, message=str(exc)))
        finally:
            # 3. Delete canary document (always clean up)
            try:
                client.delete(index=index_name, id=doc_id, request_timeout=_REQUEST_TIMEOUT)
                checks.append(ValidationCheck(name="document_delete", passed=True, message="Canary document deleted."))
            except (OpenSearchException, Exception) as exc:
                checks.append(ValidationCheck(name="document_delete", passed=False, message=str(exc)))

        return checks

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_credentials(self) -> dict[str, str]:
        """Resolve username, password, and optional tls-ca from the relation databag."""
        return {
            **self.resolve_secret("secret-user", "username", "password"),
            **self.resolve_secret("secret-tls", "tls-ca"),
        }

    def _build_client(self, creds: dict[str, str]) -> OpenSearch:
        """Construct an OpenSearch client from relation data."""
        raw_endpoints = (creds.get("endpoints") or self.databag.get("endpoints", "")).strip()
        hosts = []
        for ep in raw_endpoints.split(","):
            ep = ep.strip()
            if not ep:
                continue
            if ":" in ep:
                host, port_str = ep.rsplit(":", 1)
                try:
                    hosts.append({"host": host, "port": int(port_str)})
                except ValueError:
                    hosts.append({"host": ep, "port": 9200})
            else:
                hosts.append({"host": ep, "port": 9200})

        ca_certs = self._write_ca_file(creds.get("tls-ca"))
        use_ssl = ca_certs is not None

        try:
            return OpenSearch(
                hosts=hosts,
                http_auth=(creds.get("username", ""), creds.get("password", "")),
                use_ssl=use_ssl,
                verify_certs=use_ssl,
                ca_certs=ca_certs,
                connection_class=RequestsHttpConnection,
                timeout=_CONNECT_TIMEOUT,
            )
        except Exception:
            self._remove_ca_file()
            raise

    def _close_client(self, client: OpenSearch) -> None:
        """Close the OpenSearch client transport."""
        try:
            client.close()
        except Exception:  # nosec B110
            pass
        self._remove_ca_file()

    def _build_result(self, level: ValidationLevel, checks: list[ValidationCheck]) -> ValidationResult:
        status: ValidationResultStatus = "PASS" if all(c.passed for c in checks) else "FAIL"
        return self._make_result(status=status, level=level, checks=checks)

    # ------------------------------------------------------------------
    # CA certificate helpers
    # ------------------------------------------------------------------

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._ca_file_path: str | None = None

    def _write_ca_file(self, ca_content: str | None) -> str | None:
        """Write CA cert to a temp file; return the path, or None if no cert provided."""
        if not ca_content:
            return None
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".pem") as f:
            f.write(ca_content)
            self._ca_file_path = f.name
        return self._ca_file_path

    def _remove_ca_file(self) -> None:
        if self._ca_file_path and os.path.exists(self._ca_file_path):
            os.remove(self._ca_file_path)
            self._ca_file_path = None
