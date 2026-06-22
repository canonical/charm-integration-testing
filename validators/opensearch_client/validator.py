# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
import tempfile
import uuid
from typing import Any

from opensearchpy import OpenSearch, RequestsHttpConnection

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

_CONNECT_TIMEOUT = 5
_REQUEST_TIMEOUT = 10
_HEALTHY_STATUSES = {"green", "yellow"}
_REQUIRED_FIELDS = ["endpoints", "username", "password"]


class OpenSearchClientValidator(BaseValidator):
    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role != "requires":
            return self._skipped_result_due_to_role(level, self.role)
        if level not in ("simple", "deep"):
            return self._skipped_result_due_to_level(level)
        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")
        if level == "deep":
            return self._validate_deep()
        return self._validate_simple()

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
            return self._make_result(level="simple", checks=checks)

        try:
            client = self._build_client(creds)
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=str(exc)))
            return self._make_result(level="simple", checks=checks)

        try:
            health_check = self._check_cluster_health(client)
            checks.append(health_check)
        finally:
            self._close_client(client)

        return self._make_result(level="simple", checks=checks)

    # ------------------------------------------------------------------
    # L2: Schema + health + canary index (create → index → get → delete)
    # ------------------------------------------------------------------

    def _validate_deep(self) -> ValidationResult:
        """L2: All L1 checks, then write a canary document into the granted index, retrieve it, and delete it."""
        checks: list[ValidationCheck] = []
        creds = self._resolve_credentials()

        schema_check = self.validate_schema(_REQUIRED_FIELDS, creds)
        checks.append(schema_check)
        if not schema_check.passed:
            return self._make_result(level="deep", checks=checks)

        try:
            client = self._build_client(creds)
        except Exception as exc:
            checks.append(ValidationCheck(name="connect", passed=False, message=str(exc)))
            return self._make_result(level="deep", checks=checks)

        try:
            health_check = self._check_cluster_health(client)
            checks.append(health_check)
            if not health_check.passed:
                return self._make_result(level="deep", checks=checks)

            checks.extend(self._check_canary_index(client))
        finally:
            self._close_client(client)

        return self._make_result(level="deep", checks=checks)

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
        except Exception as exc:
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
        except Exception as exc:
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
            except Exception as exc:
                checks.append(ValidationCheck(name="document_get", passed=False, message=str(exc)))
        finally:
            # 3. Delete canary document (always clean up)
            try:
                client.delete(index=index_name, id=doc_id, request_timeout=_REQUEST_TIMEOUT)
                checks.append(ValidationCheck(name="document_delete", passed=True, message="Canary document deleted."))
            except Exception as exc:
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
                    hosts.append({"host": host, "port": 9200})
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

    # ------------------------------------------------------------------
    # CA certificate helpers
    # ------------------------------------------------------------------

    def __init__(self, *args: Any, **kwargs: Any) -> None:
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
