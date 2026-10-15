# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Build the configuration of the log scrapers that ship Juju logs to a Loki."""

import hashlib
import re
from typing import Any

import yaml

COLLECTOR_VERSION = "0.110.0"
COLLECTOR_IMAGE = f"otel/opentelemetry-collector-contrib:{COLLECTOR_VERSION}"
COLLECTOR_RELEASE_URL = (
    "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/"
    f"v{COLLECTOR_VERSION}/otelcol-contrib_{COLLECTOR_VERSION}_linux_${{ARCH}}.tar.gz"
)
COLLECTOR_SHA256_BY_ARCH = {
    "amd64": "f4d5de6062b8b0e66a9049cdc0c90de9f79bb1cfcc1a19c74518ddb0a8698e2e",
    "arm64": "e349432fa08d80113de7575d3698d6b48dda8947d829b893d7299e4389dde99a",
}

# The Loki charm only exposes the push API, so the loki exporter is used rather than OTLP.
LOKI_PUSH_PATH = "/loki/api/v1/push"

USERDATA_KEY = "cloudinit-userdata"
MACHINE_LOG_GLOB = "/var/log/juju/*.log"
MACHINE_CONFIG_PATH = "/etc/otelcol-scraper/config.yaml"
MACHINE_UNIT_PATH = "/etc/systemd/system/otelcol-scraper.service"
MACHINE_BINARY_PATH = "/usr/local/bin/otelcol-contrib"
COLLECTOR_STATE_DIRECTORY = "otelcol-scraper"
COLLECTOR_STORAGE_PATH = f"/var/lib/{COLLECTOR_STATE_DIRECTORY}"

KUBERNETES_POD_LOG_ROOT = "/var/log/pods"
KUBERNETES_SCRAPER_NAME = "log-scraper"
KUBERNETES_CONFIG_KEY = "config.yaml"
KUBERNETES_NAMESPACES_KEY = "namespaces"
KUBERNETES_NAMESPACE_PREFIX = "log-scraper-"
_KUBERNETES_NAME_MAX_LENGTH = 63
_CONTROLLER_NAME_MAX_LENGTH = _KUBERNETES_NAME_MAX_LENGTH - len(KUBERNETES_NAMESPACE_PREFIX)
_CONTROLLER_NAME_HASH_LENGTH = 12
_CONTROLLER_NAME_PREFIX_LENGTH = _CONTROLLER_NAME_MAX_LENGTH - _CONTROLLER_NAME_HASH_LENGTH - 1

# Labels derived from the log file path and promoted to resource attributes, so Loki can match on them.
PATH_LABELS = ["juju_model", "juju_application", "juju_unit_number", "juju_machine"]
# Resource attributes the loki exporter turns into Loki index labels.
LOKI_LABELS = ["juju_controller", *PATH_LABELS, "k8s.container.name", "k8s.pod.name"]
# Record attributes promoted to Loki index labels. Pod UIDs and full file paths are left out for cardinality.
LOKI_ATTRIBUTE_LABELS = ["log.file.name"]
MACHINE_UNIT_FILE = r"^unit-(?P<juju_application>.+)-(?P<juju_unit_number>\d+)\.log$"
MACHINE_MACHINE_FILE = r"^machine-(?P<juju_machine>\d+)\.log$"
# The container parser sets k8s.* resource attributes. Unit pods are named <application>-<number>.
KUBERNETES_POD_REGEX = r"^(?P<juju_application>.+)-(?P<juju_unit_number>\d+)$"
# The namespace of a controller model is controller-<name>, whose Juju model name is "controller".
_MODEL_FROM_NAMESPACE = (
    'EXPR(resource["k8s.namespace.name"] startsWith "controller-" ? "controller" : resource["k8s.namespace.name"])'
)
_KUBERNETES_NAME_INVALID_CHARS = re.compile(r"[^a-z0-9-]+")


def safe_controller_name(controller: str) -> str:
    """Return a bounded, DNS-compatible controller identifier that stays distinct after normalization."""
    normalized = _KUBERNETES_NAME_INVALID_CHARS.sub("-", controller.lower()).strip("-")
    if controller and normalized == controller and len(normalized) <= _CONTROLLER_NAME_MAX_LENGTH:
        return normalized
    suffix = hashlib.sha256(controller.encode()).hexdigest()[:_CONTROLLER_NAME_HASH_LENGTH]
    prefix = normalized[:_CONTROLLER_NAME_PREFIX_LENGTH].rstrip("-") or "controller"
    return f"{prefix}-{suffix}"


def _regex_parser(regex: str, parse_from: str) -> dict[str, Any]:
    expression_regex = regex.replace("\\", "\\\\")  # Backslashes are escaped inside expression strings.
    return {
        "type": "regex_parser",
        "parse_from": parse_from,
        "regex": regex,
        # Only parse entries the regex matches: a failed parse is logged per entry, and send_quiet duplicates entries.
        "if": f'{parse_from} matches "{expression_regex}"',
    }


def _loki_push_endpoint(endpoint: str) -> str:
    base_url = endpoint.rstrip("/")
    if base_url.endswith(LOKI_PUSH_PATH):
        return base_url
    if base_url.endswith("/loki"):
        return f"{base_url}/api/v1/push"
    return f"{base_url}{LOKI_PUSH_PATH}"


KUBERNETES_LABEL_OPERATORS: list[dict[str, Any]] = [
    {"type": "add", "field": "resource.juju_model", "value": _MODEL_FROM_NAMESPACE},
    _regex_parser(KUBERNETES_POD_REGEX, 'resource["k8s.pod.name"]'),
]


def _config(endpoint: str, include: list[str], resource: dict[str, str], *, kubernetes: bool) -> dict[str, Any]:
    receiver: dict[str, Any] = {
        "include": include,
        "start_at": "beginning",
        "include_file_path": True,
        "resource": resource,
        "storage": "file_storage",
    }
    config: dict[str, Any] = {
        "receivers": {"filelog": receiver},
        "exporters": {"loki": {"endpoint": _loki_push_endpoint(endpoint)}},
        "extensions": {"file_storage": {"directory": COLLECTOR_STORAGE_PATH}},
        "service": {
            "extensions": ["file_storage"],
            "pipelines": {"logs": {"receivers": ["filelog"], "exporters": ["loki"]}},
        },
    }
    config["processors"] = {
        "groupbyattrs": {"keys": PATH_LABELS},
        "resource": {
            "attributes": [
                {"key": "loki.resource.labels", "value": ",".join(LOKI_LABELS), "action": "insert"},
                {"key": "loki.format", "value": "raw", "action": "insert"},
            ]
        },
        "attributes": {
            "actions": [{"key": "loki.attribute.labels", "value": ",".join(LOKI_ATTRIBUTE_LABELS), "action": "insert"}]
        },
    }
    config["service"]["pipelines"]["logs"]["processors"] = ["groupbyattrs", "resource", "attributes"]
    if not kubernetes:
        receiver["operators"] = [
            _regex_parser(regex, 'attributes["log.file.name"]') for regex in (MACHINE_UNIT_FILE, MACHINE_MACHINE_FILE)
        ]
    else:
        # Offsets are persisted so restarting the scraper to pick up new namespaces neither loses nor repeats logs.
        receiver["operators"] = [{"type": "container", "id": "container-parser"}, *KUBERNETES_LABEL_OPERATORS]
    return config


def machine_userdata(endpoint: str, controller: str, model: str) -> dict[str, Any]:
    """Return cloud-init data that runs a collector shipping ``/var/log/juju`` to *endpoint*."""
    config = _config(
        endpoint,
        [MACHINE_LOG_GLOB],
        {"juju_controller": controller, "juju_model": model},
        kubernetes=False,
    )
    unit = "\n".join(
        [
            "[Unit]",
            "Description=Juju log scraper",
            "After=network-online.target",
            "[Service]",
            f"ExecStart={MACHINE_BINARY_PATH} --config {MACHINE_CONFIG_PATH}",
            "Restart=always",
            f"StateDirectory={COLLECTOR_STATE_DIRECTORY}",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )
    return {
        "write_files": [
            {"path": MACHINE_CONFIG_PATH, "permissions": "0644", "content": yaml.safe_dump(config, sort_keys=False)},
            {"path": MACHINE_UNIT_PATH, "permissions": "0644", "content": unit},
        ],
        "postruncmd": [
            'ARCH="$(dpkg --print-architecture)"; '
            'case "${ARCH}" in '
            + " ".join(
                f"{architecture}) SHA256='{checksum}' ;;" for architecture, checksum in COLLECTOR_SHA256_BY_ARCH.items()
            )
            + " *) echo \"Unsupported collector architecture: ${ARCH}\" >&2; exit 1 ;; esac; "
            f"curl -fsSL --retry 5 --retry-all-errors -o /tmp/otelcol-scraper.tgz {COLLECTOR_RELEASE_URL} && "
            'printf "%s  %s\\n" "${SHA256}" /tmp/otelcol-scraper.tgz | sha256sum --check --strict - && '
            f"tar -xzf /tmp/otelcol-scraper.tgz -C {MACHINE_BINARY_PATH.rsplit('/', 1)[0]} otelcol-contrib && "
            "rm -f /tmp/otelcol-scraper.tgz",
            "systemctl daemon-reload",
            "systemctl enable --now otelcol-scraper",
        ],
    }


def merge_userdata(existing: str | None, addition: dict[str, Any]) -> str:
    """Merge *addition* into an existing ``cloudinit-userdata`` value, keeping the user's entries."""
    loaded = yaml.safe_load(existing) if existing else {}
    if loaded is not None and not isinstance(loaded, dict):
        raise ValueError("cloudinit-userdata must contain a YAML mapping.")
    merged: dict[str, Any] = loaded or {}
    for key, value in addition.items():
        if key not in merged:
            merged[key] = value
            continue
        current = merged.get(key)
        if isinstance(current, list) and isinstance(value, list):
            merged[key] = [*current, *value]
        elif isinstance(current, list) != isinstance(value, list):
            raise ValueError(f"cloudinit-userdata key '{key}' cannot merge list and non-list values.")
        else:
            merged[key] = value
    return yaml.safe_dump(merged, sort_keys=False)


def kubernetes_include_globs(namespaces: list[str]) -> list[str]:
    # Pod log directories are named <namespace>_<pod>_<uid>; namespaces cannot contain underscores.
    return [f"{KUBERNETES_POD_LOG_ROOT}/{namespace}_*/*/*.log" for namespace in sorted(namespaces)]


def kubernetes_scraper_config(endpoint: str, controller: str, namespaces: list[str]) -> str:
    return yaml.safe_dump(
        _config(
            endpoint,
            kubernetes_include_globs(namespaces),
            {"juju_controller": controller},
            kubernetes=True,
        ),
        sort_keys=False,
    )


def kubernetes_config_map(namespace: str, endpoint: str, controller: str, scraped: list[str]) -> dict[str, Any]:
    config = kubernetes_scraper_config(endpoint, controller, scraped)
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": KUBERNETES_SCRAPER_NAME, "namespace": namespace},
        "data": {KUBERNETES_CONFIG_KEY: config, KUBERNETES_NAMESPACES_KEY: "\n".join(sorted(scraped))},
    }


def kubernetes_daemon_set(namespace: str, controller: str) -> dict[str, Any]:
    labels = {"app": KUBERNETES_SCRAPER_NAME}
    return {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {"name": KUBERNETES_SCRAPER_NAME, "namespace": namespace, "labels": labels},
        "spec": {
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "containers": [
                        {
                            "name": "otelcol",
                            "image": COLLECTOR_IMAGE,
                            "args": [f"--config=/etc/otelcol/{KUBERNETES_CONFIG_KEY}"],
                            "securityContext": {
                                "runAsUser": 0,
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [
                                {"name": "config", "mountPath": "/etc/otelcol"},
                                {"name": "pods", "mountPath": KUBERNETES_POD_LOG_ROOT, "readOnly": True},
                                {"name": "storage", "mountPath": COLLECTOR_STORAGE_PATH},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "config", "configMap": {"name": KUBERNETES_SCRAPER_NAME}},
                        {"name": "pods", "hostPath": {"path": KUBERNETES_POD_LOG_ROOT}},
                        {
                            "name": "storage",
                            "hostPath": {
                                "path": f"{COLLECTOR_STORAGE_PATH}/{safe_controller_name(controller)}",
                                "type": "DirectoryOrCreate",
                            },
                        },
                    ],
                },
            },
        },
    }
