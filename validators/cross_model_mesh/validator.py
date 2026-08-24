# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Validator for the cross_model_mesh Juju interface.

The cross_model_mesh interface carries no application traffic itself. It is a
metadata side-channel used by service-mesh charms (e.g. istio-beacon-k8s) to
learn the identity of a cross-model application that should be granted access
through the mesh, so that the mesh can build authorization policies for it.

Interface name:  cross_model_mesh
Endpoint names:  require-cmr-mesh (requirer), provide-cmr-mesh (provider)

Requirer (e.g. catalogue-k8s, grafana-k8s):
    Publishes its own identity into its local application databag on this
    relation as soon as the relation is created:
        cmr_data    JSON-encoded ``{"app_name": ..., "juju_model_name": ...}``

Provider (e.g. istio-beacon-k8s):
    Reads ``cmr_data`` from the remote (requirer) application databag and
    uses it to build mesh authorization policies granting that identity
    access. The provider does not write anything back onto this relation.
"""

import json
import re
import socket
import ssl
import urllib.error
import urllib.request
from typing import TypedDict

from validators.base import (
    BaseValidator,
    ValidationCheck,
    ValidationLevel,
    ValidationResult,
)

# Juju application and model names: lowercase alphanumeric, may contain
# (but not start/end with) hyphens.
_JUJU_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

# The well-known HBONE (mTLS tunnel) port exposed by Istio ambient mesh
# waypoints and ztunnel data-plane components. Used only as a last-resort
# fallback canary port (see ``_check_mesh_data_plane_reachable``): in ambient
# mode this port is served node-locally by ztunnel for any Service ClusterIP
# regardless of whether the destination workload actually has running pods
# behind it, so it does not reliably detect a downed workload on its own.
_AMBIENT_MESH_PORT = 15008

# Standard in-cluster Kubernetes API access, available to every pod via its
# mounted service account. Used to discover the destination application's
# real Kubernetes Service port(s), which does detect a downed workload (an
# empty-endpoints Service refuses connections on its real port, unlike the
# always-listening ambient HBONE port above).
_K8S_API_SERVER = "https://kubernetes.default.svc"
_K8S_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

_DNS_TIMEOUT = 5
_TCP_TIMEOUT = 5
_K8S_API_TIMEOUT = 5


class CMRData(TypedDict):
    app_name: str
    juju_model_name: str


class CrossModelMeshValidator(BaseValidator):
    """Validates the cross_model_mesh relation contract for both relation roles."""

    def validate(self, level: ValidationLevel = "simple") -> ValidationResult:
        if self.role == "requires":
            if level == "simple":
                return self._validate_requires_simple()
            return self._skipped_result_due_to_level(level)

        if self.role == "provides":
            if level == "simple":
                return self._validate_provides_simple()
            if level == "deep":
                return self._validate_provides_deep()
            return self._skipped_result_due_to_level(level)

        return self._skipped_result_due_to_role(level, self.role)

    # ------------------------------------------------------------------
    # Requires role — the app requesting cross-model access publishes its
    # own identity into its local application databag on this relation.
    # ------------------------------------------------------------------

    def _validate_requires_simple(self) -> ValidationResult:
        """L1: The locally-published identity is well-formed and self-consistent.

        The provider does not publish anything back on this relation (see
        module docstring), so there is nothing meaningful to read from the
        remote side. Instead we validate that this application correctly
        published its own contract data, since that is the prerequisite for
        the mesh provider to ever grant it access.
        """
        checks: list[ValidationCheck] = []

        error = self._check_relation_exists("simple")
        if error:
            return error

        cmr_data, schema_check = self._parse_local_cmr_data()
        checks.append(schema_check)
        if not schema_check.passed or cmr_data is None:
            return self._make_result(level="simple", checks=checks)

        checks.append(_check_identity_format(cmr_data))
        if not checks[-1].passed:
            return self._make_result(level="simple", checks=checks)

        checks.append(self._check_self_consistency(cmr_data))

        return self._make_result(level="simple", checks=checks)

    def _parse_local_cmr_data(self) -> tuple[CMRData | None, ValidationCheck]:
        """Read and decode ``cmr_data`` from this application's own databag.

        Unlike ``self.databag`` (which exposes the *remote* application's
        data), the requirer's contribution lives in its own local databag on
        this relation, so it is read directly from ``self.relation.data``.
        """
        local_data = dict(self.relation.data[self.charm.app])
        return _decode_cmr_data(local_data.get("cmr_data", ""), source="local application databag")

    def _check_self_consistency(self, cmr_data: CMRData) -> ValidationCheck:
        """Verify the published identity matches this charm's actual app/model.

        A mismatch here means the requirer would be requesting access under
        the wrong identity, which the mesh provider would then use to build
        authorization policies for the wrong application.
        """
        expected_app = self.charm.app.name
        expected_model = self.charm.model.name
        if cmr_data["app_name"] != expected_app or cmr_data["juju_model_name"] != expected_model:
            return ValidationCheck(
                name="self_consistency",
                passed=False,
                message=(
                    f"Published identity {cmr_data!r} does not match this application "
                    f"({expected_app!r} in model {expected_model!r}). "
                    "Remediation: verify the charm publishes its own app/model name unmodified."
                ),
            )
        return ValidationCheck(
            name="self_consistency",
            passed=True,
            message=f"Published identity matches {expected_app!r} in model {expected_model!r}.",
        )

    # ------------------------------------------------------------------
    # Provides role — the mesh reads the remote requirer's declared
    # identity from this relation's remote application databag.
    # ------------------------------------------------------------------

    def _validate_provides_simple(self) -> ValidationResult:
        """L1: Schema/type validation and reachability of the declared identity."""
        checks: list[ValidationCheck] = []

        error = self._check_relation_exists("simple")
        if error:
            return error

        cmr_data, schema_check = self._parse_remote_cmr_data()
        checks.append(schema_check)
        if not schema_check.passed or cmr_data is None:
            return self._make_result(level="simple", checks=checks)

        checks.append(_check_identity_format(cmr_data))
        if not checks[-1].passed:
            return self._make_result(level="simple", checks=checks)

        checks.append(_check_dns_reachable(cmr_data))

        return self._make_result(level="simple", checks=checks)

    def _validate_provides_deep(self) -> ValidationResult:
        """L2: All L1 checks plus a canary TCP connection across the mesh's data plane."""
        checks: list[ValidationCheck] = []

        error = self._check_relation_exists("deep")
        if error:
            return error

        cmr_data, schema_check = self._parse_remote_cmr_data()
        checks.append(schema_check)
        if not schema_check.passed or cmr_data is None:
            return self._make_result(level="deep", checks=checks)

        checks.append(_check_identity_format(cmr_data))
        if not checks[-1].passed:
            return self._make_result(level="deep", checks=checks)

        dns_check = _check_dns_reachable(cmr_data)
        checks.append(dns_check)
        if not dns_check.passed:
            return self._make_result(level="deep", checks=checks)

        checks.append(_check_mesh_data_plane_reachable(cmr_data))

        return self._make_result(level="deep", checks=checks)

    def _parse_remote_cmr_data(self) -> tuple[CMRData | None, ValidationCheck]:
        """Read and decode ``cmr_data`` from the remote (requirer) application databag."""
        return _decode_cmr_data(self.databag.get("cmr_data", ""), source="remote application databag")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _check_relation_exists(self, level: ValidationLevel) -> ValidationResult | None:
        if not self.relation_exists():
            return self._error_result(level, f"No remote application on relation '{self.endpoint}'.")
        return None


# ---------------------------------------------------------------------------
# Pure helpers — schema decoding and validation
# ---------------------------------------------------------------------------


def _decode_cmr_data(raw: str, *, source: str) -> tuple[CMRData | None, ValidationCheck]:
    """Decode a ``cmr_data`` JSON string into a CMRData dict.

    Returns ``(None, failing_check)`` if the field is missing, not valid
    JSON, or does not contain both ``app_name`` and ``juju_model_name`` as
    non-empty strings.
    """
    if not raw:
        return None, ValidationCheck(
            name="schema",
            passed=False,
            message=f"Missing 'cmr_data' key in {source}.",
        )

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, ValidationCheck(
            name="schema",
            passed=False,
            message=f"Invalid JSON in 'cmr_data' field of {source}: {exc}",
        )

    if not isinstance(decoded, dict):
        return None, ValidationCheck(
            name="schema",
            passed=False,
            message=f"'cmr_data' in {source} must decode to an object, got: {decoded!r}",
        )

    missing = [f for f in ("app_name", "juju_model_name") if not decoded.get(f)]
    if missing or not all(isinstance(decoded.get(f), str) for f in ("app_name", "juju_model_name")):
        return None, ValidationCheck(
            name="schema",
            passed=False,
            message=(
                f"'cmr_data' in {source} is missing or has invalid (non-string/empty) "
                f"field(s): {', '.join(missing) or 'app_name/juju_model_name'}."
            ),
        )

    return (
        CMRData(app_name=decoded["app_name"], juju_model_name=decoded["juju_model_name"]),
        ValidationCheck(name="schema", passed=True, message=f"Found valid cmr_data in {source}."),
    )


def _check_identity_format(cmr_data: CMRData) -> ValidationCheck:
    """Verify app_name and juju_model_name conform to valid Juju naming rules."""
    invalid = [
        f"{field}={value!r}"
        for field, value in (("app_name", cmr_data["app_name"]), ("juju_model_name", cmr_data["juju_model_name"]))
        if not _JUJU_NAME_RE.match(value)
    ]
    if invalid:
        return ValidationCheck(
            name="identity_format",
            passed=False,
            message=(
                f"Invalid Juju name(s) in cmr_data: {', '.join(invalid)}. "
                "Remediation: app/model names must be lowercase alphanumeric with internal hyphens only."
            ),
        )
    return ValidationCheck(
        name="identity_format",
        passed=True,
        message=f"app_name={cmr_data['app_name']!r}, juju_model_name={cmr_data['juju_model_name']!r} are well-formed.",
    )


# ---------------------------------------------------------------------------
# Pure helpers — network
# ---------------------------------------------------------------------------


def _cross_model_dns_name(cmr_data: CMRData) -> str:
    """Return the standard Kubernetes Service FQDN for a Juju k8s application."""
    return f"{cmr_data['app_name']}.{cmr_data['juju_model_name']}.svc.cluster.local"


def _check_dns_reachable(cmr_data: CMRData) -> ValidationCheck:
    """Verify the declared identity resolves to a live Kubernetes workload.

    This is a prerequisite for any mesh authorization to have an effect: if
    the declared application/model cannot be resolved at all, the mesh has
    no addressable target to route traffic to.
    """
    host = _cross_model_dns_name(cmr_data)
    try:
        socket.setdefaulttimeout(_DNS_TIMEOUT)
        socket.gethostbyname(host)
    except OSError as exc:
        return ValidationCheck(
            name="dns_reachable",
            passed=False,
            message=(
                f"Could not resolve {host!r}: {exc}. "
                "Remediation: confirm the requirer application and model names in cmr_data are correct "
                "and that the workload is deployed."
            ),
        )
    return ValidationCheck(name="dns_reachable", passed=True, message=f"Resolved {host!r}.")


def _discover_service_ports(namespace: str, name: str) -> list[int] | None:
    """Best-effort lookup of a Kubernetes Service's declared port(s) via the
    in-cluster API, using the pod's own mounted service account.

    Mesh providers of ``cross_model_mesh`` (e.g. istio-beacon-k8s) inherently
    require broad, cluster-scoped Kubernetes RBAC in order to manage
    authorization policies for a *cross-namespace* identity, so relying on
    this in-cluster access for a same-cluster reachability probe is a
    reasonable assumption for this specific interface's provider role.

    Returns ``None`` (rather than raising) on any failure -- missing service
    account files, RBAC denial, Service not found, API unreachable (e.g. a
    genuinely different Kubernetes cluster) -- so callers can fall back to a
    less discriminating probe instead of failing outright.
    """
    try:
        with open(f"{_K8S_SA_DIR}/token", encoding="utf-8") as fh:
            token = fh.read().strip()
        ctx = ssl.create_default_context(cafile=f"{_K8S_SA_DIR}/ca.crt")
        url = f"{_K8S_API_SERVER}/api/v1/namespaces/{namespace}/services/{name}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, context=ctx, timeout=_K8S_API_TIMEOUT) as response:  # nosec B310
            service = json.loads(response.read())
        ports = [port["port"] for port in service.get("spec", {}).get("ports", []) if "port" in port]
        return ports or None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _check_mesh_data_plane_reachable(cmr_data: CMRData) -> ValidationCheck:
    """Canary: open a TCP connection to the declared identity's workload port.

    Prefers the destination's real Kubernetes Service port (discovered via
    the in-cluster API), since traffic to it is only accepted while the
    workload actually has running pods behind it. Falls back to the
    well-known ambient mesh HBONE port when discovery is unavailable (e.g. a
    genuinely separate Kubernetes cluster); that fallback is a weaker signal,
    as it only confirms a mesh data-plane proxy is listening for the
    destination's address, not that its workload is currently up.
    """
    host = _cross_model_dns_name(cmr_data)
    ports = _discover_service_ports(cmr_data["juju_model_name"], cmr_data["app_name"])
    if ports:
        port = ports[0]
        port_desc = f"application port {port} (discovered via in-cluster Service lookup)"
    else:
        port = _AMBIENT_MESH_PORT
        port_desc = f"ambient mesh HBONE port {port} (best-effort fallback; Service port discovery unavailable)"

    try:
        with socket.create_connection((host, port), timeout=_TCP_TIMEOUT):
            pass
    except OSError as exc:
        return ValidationCheck(
            name="mesh_data_plane_reachable",
            passed=False,
            message=(
                f"Could not reach {host!r} on {port_desc}: {exc}. "
                "Remediation: verify the destination workload has running units and that the mesh's "
                "ambient data plane (e.g. ztunnel) is not blocked by a NetworkPolicy between namespaces."
            ),
        )
    return ValidationCheck(
        name="mesh_data_plane_reachable",
        passed=True,
        message=f"Connected to {host!r} on {port_desc}.",
    )
