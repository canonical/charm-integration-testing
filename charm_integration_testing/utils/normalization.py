# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

import re
from typing import Any

# Exceptions for numeric normalization - common technical terms that contain numbers
# These should not be normalized by _normalize_numeric_sequences
NUMERIC_NORMALIZATION_EXCEPTIONS = [
    "k8s",
    "K8s",
    "K8S",
    "s3",
    "S3",
]


def _convert_to_string(value: Any) -> str:
    """Convert a value to a string.

    Args:
        value: The value to convert (can be bytes, str, or any object)

    Returns:
        String representation of the value
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _normalize_numeric_sequences(text: str) -> str:
    """Replace all numeric sequences with 'XXX'.

    This normalizes timestamps, IP addresses, and other variable numeric data.
    Excludes common technical terms that contain numbers (e.g., k8s, s3).

    Args:
        text: The text to normalize

    Returns:
        Text with all numeric sequences replaced, except for exceptions
    """
    # Store exceptions temporarily with placeholders to protect them
    # We need to track the actual matches to preserve case
    placeholder_to_original: dict[str, str] = {}

    def replace_with_placeholder(match: re.Match[str], exception_idx: int) -> str:
        """Replace a match with a placeholder and store the original."""
        original = match.group(0)
        placeholder = f"__NUMERIC_EXCEPTION_{chr(65 + exception_idx)}__"
        placeholder_to_original[placeholder] = original
        return placeholder

    # Replace exceptions with placeholders
    for i, exception in enumerate(NUMERIC_NORMALIZATION_EXCEPTIONS):
        # Find all matches with case-insensitive search using word boundaries
        # to avoid partial matches (e.g., 's3' in 's3210' should not match)
        text = re.sub(
            r"\b" + re.escape(exception) + r"\b",
            lambda match: replace_with_placeholder(match, i),
            text,
            flags=re.IGNORECASE,
        )

    # Apply numeric normalization
    text = re.sub(r"\d+", "XXX", text)

    # Restore exceptions with their original case
    for placeholder, original in placeholder_to_original.items():
        text = text.replace(placeholder, original)

    return text


def _normalize_ip_addresses(text: str) -> str:
    """Replace IPv4 and IPv6 addresses with '<IP>'.

    Matches both IPv4 (e.g., 192.168.1.1) and IPv6 (e.g., 2001:db8::1) addresses.
    IPv6 addresses can be in full or compressed form.

    Args:
        text: The text containing IP addresses

    Returns:
        Text with IP addresses replaced
    """
    # IPv6 pattern from https://stackoverflow.com/questions/53497/regular-expression-that-matches-valid-ipv6-addresses
    # The pattern needs to be bounded to match complete addresses, not partial matches
    ipv6_pattern = (
        r"(?:^|(?<=\s)|(?<=\[))"  # Start of string, after whitespace, or after [
        r"("
        r"([0-9a-fA-F]{1,4}:){7,7}[0-9a-fA-F]{1,4}|"  # 1:2:3:4:5:6:7:8
        r"([0-9a-fA-F]{1,4}:){1,7}:|"  # 1::                              1:2:3:4:5:6:7::
        r"([0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}|"  # 1::8             1:2:3:4:5:6::8  1:2:3:4:5:6::8
        r"([0-9a-fA-F]{1,4}:){1,5}(:[0-9a-fA-F]{1,4}){1,2}|"  # 1::7:8           1:2:3:4:5::7:8  1:2:3:4:5::8
        r"([0-9a-fA-F]{1,4}:){1,4}(:[0-9a-fA-F]{1,4}){1,3}|"  # 1::6:7:8         1:2:3:4::6:7:8  1:2:3:4::8
        r"([0-9a-fA-F]{1,4}:){1,3}(:[0-9a-fA-F]{1,4}){1,4}|"  # 1::5:6:7:8       1:2:3::5:6:7:8  1:2:3::8
        r"([0-9a-fA-F]{1,4}:){1,2}(:[0-9a-fA-F]{1,4}){1,5}|"  # 1::4:5:6:7:8     1:2::4:5:6:7:8  1:2::8
        r"[0-9a-fA-F]{1,4}:((:[0-9a-fA-F]{1,4}){1,6})|"  # 1::3:4:5:6:7:8   1::3:4:5:6:7:8  1::8
        r":((:[0-9a-fA-F]{1,4}){1,7}|:)|"  # ::2:3:4:5:6:7:8  ::2:3:4:5:6:7:8 ::8       ::
        r"fe80:(:[0-9a-fA-F]{0,4}){0,4}%[0-9a-zA-Z]{1,}|"  # fe80::7:8%eth0   fe80::7:8%1     (link-local IPv6 addresses with zone index)
        r"::(ffff(:0{1,4}){0,1}:){0,1}"
        r"((25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9])\.){3,3}"
        r"(25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9])|"  # ::255.255.255.255   ::ffff:255.255.255.255  ::ffff:0:255.255.255.255  (IPv4-mapped IPv6 addresses and IPv4-translated addresses)
        r"([0-9a-fA-F]{1,4}:){1,4}:"
        r"((25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9])\.){3,3}"
        r"(25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9])"  # 2001:db8:3:4::192.0.2.33  64:ff9b::192.0.2.33 (IPv4-Embedded IPv6 Address)
        r")"
        r"(?=\]|:|/|\s|$)"  # Followed by ], :, /, whitespace, or end of string
    )
    text = re.sub(ipv6_pattern, "<IP>", text)

    # IPv4 pattern: four groups of 1-3 digits separated by dots
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<IP>", text)

    return text


def _normalize_timestamps(text: str) -> str:
    """Replace timestamps with '<TIMESTAMP>'.

    Matches common timestamp formats:
    - ISO 8601: 2024-12-11T10:30:45, 2024-12-11T10:30:45Z, 2024-12-11T10:30:45.123Z
    - Date only: 2024-12-11
    - Time with microseconds: 10:30:45.123456
    - Unix timestamps are handled by numeric normalization

    Args:
        text: The text containing timestamps

    Returns:
        Text with timestamps replaced
    """
    # ISO 8601 with optional timezone and microseconds
    text = re.sub(
        r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b",
        "<TIMESTAMP>",
        text,
    )

    # Date only (YYYY-MM-DD)
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "<TIMESTAMP>", text)

    # Time with optional microseconds (HH:MM:SS or HH:MM:SS.ffffff)
    text = re.sub(r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b", "<TIMESTAMP>", text)

    return text


def _normalize_pod_names(text: str) -> str:
    """Normalize Kubernetes pod names to remove dynamic suffixes.

    Pod names in Kubernetes logs often appear as pod=<podName>_<namespace>(<uid>)
    This function replaces the entire pod reference with pod=<POD>.
    See: https://github.com/canonical/kubernetes-dqlite/blob/136e88e2c4309776ff735a990003ecb5e541dc94/pkg/kubelet/kuberuntime/kuberuntime_manager.go#L925
    See: https://github.com/kubernetes/kubernetes/blob/df11db1c0f08fab3c0baee1e5ce6efbf816af7f1/pkg/kubelet/util/format/pod.go#L36

    Args:
        text: The text containing pod names

    Returns:
        Text with pod references normalized
    """
    return re.sub(
        r"pod=[a-z0-9.-]+_[a-z0-9.-]+\([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\)",
        "pod=<POD>",
        text,
    )


def _normalize_uuids(text: str) -> str:
    """Replace UUIDs with '<UUID>'.

    Matches standard UUID format: 8-4-4-4-12 hex digits.

    Args:
        text: The text containing UUIDs

    Returns:
        Text with UUIDs replaced
    """
    return re.sub(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", "<UUID>", text)


def _normalize_temp_files(text: str) -> str:
    """Normalize temporary file paths with random suffixes.

    Matches patterns like /tmp<alphanumeric> (e.g., /tmp5d7rg3qj, /tmp_test123)
    commonly generated by mkstemp and similar functions.

    Args:
        text: The text containing temp file paths

    Returns:
        Text with temp file suffixes normalized
    """
    # Note on nosec: not creating a temp file; just matching "/tmp..." in a string for normalization
    return re.sub(r"/tmp[a-zA-Z0-9_]+", "/tmp<TEMP>", text)  # nosec B108


def _normalize_minio_probe_urls(text: str) -> str:
    """Normalize MinIO probe-bsign URLs with random nonces.

    MinIO health checks use probe-bsign-<random> patterns for authentication
    and connectivity testing. This normalizes these URLs for consistent metadata.

    Args:
        text: The text containing MinIO probe URLs

    Returns:
        Text with probe-bsign nonces normalized
    """
    return re.sub(r"probe-bsign-[a-z0-9]+", "probe-bsign-<NONCE>", text)


def _normalize_oci_image_digests(text: str) -> str:
    """Normalize OCI image digests.

    OCI/Docker images use digests matching this grammar:
      digest                ::= algorithm ":" encoded
      algorithm             ::= algorithm-component (algorithm-separator algorithm-component)*
      algorithm-component   ::= [a-z0-9]+
      algorithm-separator   ::= [+._-]
      encoded               ::= [a-zA-Z0-9=_-]+

    Only matches registered algorithms: sha256, sha512, blake3

    See: https://github.com/opencontainers/image-spec/blob/main/descriptor.md#digests
    See: https://github.com/opencontainers/image-spec/blob/main/descriptor.md#registered-algorithms

    Args:
        text: The text containing OCI image references

    Returns:
        Text with image digests normalized
    """
    return re.sub(r"((?:sha256|sha512|blake3)(?:[+._-][a-z0-9]+)*):[a-zA-Z0-9=_-]+", r"\1:<DIGEST>", text)


def _normalize_container_names(text: str) -> str:
    """Normalize container names in error messages.

    Matches the pattern container=<name> and replaces the name with <CONTAINER>.
    This groups errors that occur in different containers but are otherwise identical.

    Args:
        text: The text containing container references

    Returns:
        Text with container names normalized
    """
    return re.sub(r"container=[a-z0-9-]+", r"container=<CONTAINER>", text)


def _normalize_hook_failure_apps(text: str) -> str:
    """Normalize application and endpoint names in hook failure messages.

    Matches the pattern 'hook failed: "..." for app:endpoint' and replaces
    the application and endpoint with placeholders while keeping the hook name intact.

    Args:
        text: The text containing hook failure messages

    Returns:
        Text with app/endpoint names normalized
    """
    return re.sub(r'(hook failed: "[^"]+") for [a-z0-9-]+:[a-z0-9-]+', r"\1 for <APP>:<ENDPOINT>", text)


def _normalize_k8s_cluster_urls(text: str) -> str:
    """Normalize Kubernetes cluster DNS names by replacing service and namespace.

    Matches service DNS names of the form:
      <service>.<namespace>.svc.cluster.local
    and replaces the service and namespace with placeholders while keeping
    the 'svc.cluster.local' suffix intact.

    For example:
      tempo-coordinator-k8s.ryan-stg.svc.cluster.local:4317
    becomes:
      <SERVICE>.<NAMESPACE>.svc.cluster.local:4317

    Args:
        text: The text containing Kubernetes cluster DNS names

    Returns:
        Text with cluster DNS service and namespace names replaced
    """
    return re.sub(
        r"[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9-]*\.svc\.cluster\.local",
        "<SERVICE>.<NAMESPACE>.svc.cluster.local",
        text,
    )


def _normalize_relation_version_apps(text: str) -> str:
    """Normalize application names in relation version error messages.

    Matches the pattern 'versions not found for apps: app-name' and replaces
    the application name with a placeholder while keeping the relation name intact.

    This error is raised by the serialized-data-interface library:
    https://github.com/canonical/serialized-data-interface/blob/8ab9b715898db535c087b795bcefb6d17ea9e025/serialized_data_interface/errors.py#L120

    Args:
        text: The text containing relation version error messages

    Returns:
        Text with app names normalized
    """
    return re.sub(r"(versions not found for apps:) [a-z0-9-]+", r"\1 <APP>", text)


def _truncate_string(text: str, max_length: int) -> str:
    """Truncate a string to a maximum length.

    Args:
        text: The text to truncate
        max_length: Maximum character count

    Returns:
        Truncated text with ellipsis if needed
    """
    if len(text) > max_length:
        return f"{text[:max_length - 3]}..."
    return text


def normalize_string(message: Any, max_length: int = 150) -> str:
    """Normalize a string for use as execution metadata.

    Applies multiple normalizations to create consistent metadata across test runs:
    - Converts bytes/objects to strings
    - Normalizes Kubernetes pod names
    - Normalizes UUIDs
    - Normalizes temporary file paths
    - Normalizes MinIO probe URLs
    - Normalizes OCI image digests
    - Normalizes IP addresses (IPv4 and IPv6)
    - Normalizes timestamps
    - Normalizes container names
    - Normalizes hook failure app/endpoint names
    - Normalizes relation version app names
    - Normalizes Kubernetes cluster DNS names (svc.cluster.local)
    - Replaces all numeric sequences (except technical terms like k8s, s3)
    - Truncates to maximum length

    Args:
        message: The value to normalize (can be bytes, str, or any object)
        max_length: Maximum length of the resulting string (default: 150)

    Returns:
        Normalized string suitable for execution metadata
    """
    text = _convert_to_string(message)
    text = _normalize_pod_names(text)
    text = _normalize_uuids(text)
    text = _normalize_temp_files(text)
    text = _normalize_minio_probe_urls(text)
    text = _normalize_oci_image_digests(text)
    text = _normalize_ip_addresses(text)
    text = _normalize_timestamps(text)
    text = _normalize_container_names(text)
    text = _normalize_hook_failure_apps(text)
    text = _normalize_relation_version_apps(text)
    text = _normalize_k8s_cluster_urls(text)
    text = _normalize_numeric_sequences(text)
    text = _truncate_string(text, max_length)
    return text


def normalize_string_multiline(message: Any, max_length: int = 150) -> list[str]:
    """Normalize a multi-line string for use as execution metadata.
    Splits the input into lines and normalizes each line individually.

    Args:
        message: The value to normalize (can be bytes, str, or any object)
        max_length: Maximum length of each resulting line (default: 150)

    Returns:
        List of normalized strings suitable for execution metadata
    """
    text = _convert_to_string(message)
    lines = text.splitlines()
    normalized_lines = [normalize_string(line, max_length) for line in lines]
    return normalized_lines
