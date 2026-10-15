# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Build the configuration of the log scrapers that ship Juju logs to a Loki."""

from typing import Any

import yaml

COLLECTOR_VERSION = "0.110.0"
COLLECTOR_IMAGE = f"otel/opentelemetry-collector-contrib:{COLLECTOR_VERSION}"
COLLECTOR_RELEASE_URL = (
    "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/"
    f"v{COLLECTOR_VERSION}/otelcol-contrib_{COLLECTOR_VERSION}_linux_${{ARCH}}.tar.gz"
)

# The Loki charm only exposes the push API, so the loki exporter is used rather than OTLP.
LOKI_PUSH_PATH = "/loki/api/v1/push"

USERDATA_KEY = "cloudinit-userdata"
MACHINE_LOG_GLOB = "/var/log/juju/*.log"
MACHINE_CONFIG_PATH = "/etc/otelcol-scraper/config.yaml"
MACHINE_UNIT_PATH = "/etc/systemd/system/otelcol-scraper.service"
MACHINE_BINARY_PATH = "/usr/local/bin/otelcol-contrib"

KUBERNETES_POD_LOG_ROOT = "/var/log/pods"
KUBERNETES_STORAGE_ROOT = "/var/lib/otelcol-scraper"
KUBERNETES_SCRAPER_NAME = "log-scraper"
KUBERNETES_CONFIG_KEY = "config.yaml"
KUBERNETES_NAMESPACES_KEY = "namespaces"

# Labels derived from the log file path and promoted to resource attributes, so Loki can match on them.
PATH_LABELS = ["juju_model", "juju_application", "juju_unit_number", "juju_machine"]
# Resource attributes the loki exporter turns into Loki index labels.
LOKI_LABELS = ["juju_controller", *PATH_LABELS]
MACHINE_UNIT_FILE = r"^unit-(?P<juju_application>.+)-(?P<juju_unit_number>\d+)\.log$"
MACHINE_MACHINE_FILE = r"^machine-(?P<juju_machine>\d+)\.log$"
# The container parser sets k8s.* resource attributes. Unit pods are named <application>-<number>.
KUBERNETES_POD_REGEX = r"^(?P<juju_application>.+)-(?P<juju_unit_number>\d+)$"
# The namespace of a controller model is controller-<name>, whose Juju model name is "controller".
_MODEL_FROM_NAMESPACE = (
    'EXPR(resource["k8s.namespace.name"] startsWith "controller-" ? "controller" : resource["k8s.namespace.name"])'
)


def _regex_parser(regex: str, parse_from: str) -> dict[str, Any]:
    expression_regex = regex.replace("\\", "\\\\")  # Backslashes are escaped inside expression strings.
    return {
        "type": "regex_parser",
        "parse_from": parse_from,
        "regex": regex,
        # Only parse entries the regex matches: a failed parse is logged per entry, and send_quiet duplicates entries.
        "if": f'{parse_from} matches "{expression_regex}"',
    }


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
    }
    config: dict[str, Any] = {
        "receivers": {"filelog": receiver},
        "exporters": {"loki": {"endpoint": f"{endpoint.rstrip('/')}{LOKI_PUSH_PATH}"}},
        "service": {"pipelines": {"logs": {"receivers": ["filelog"], "exporters": ["loki"]}}},
    }
    config["processors"] = {
        "groupbyattrs": {"keys": PATH_LABELS},
        "resource": {
            "attributes": [{"key": "loki.resource.labels", "value": ",".join(LOKI_LABELS), "action": "insert"}]
        },
    }
    config["service"]["pipelines"]["logs"]["processors"] = ["groupbyattrs", "resource"]
    if not kubernetes:
        receiver["operators"] = [
            _regex_parser(regex, 'attributes["log.file.name"]') for regex in (MACHINE_UNIT_FILE, MACHINE_MACHINE_FILE)
        ]
    else:
        # Offsets are persisted so restarting the scraper to pick up new namespaces neither loses nor repeats logs.
        receiver["storage"] = "file_storage"
        receiver["operators"] = [{"type": "container", "id": "container-parser"}, *KUBERNETES_LABEL_OPERATORS]
        config["extensions"] = {"file_storage": {"directory": "/var/lib/otelcol"}}
        config["service"]["extensions"] = ["file_storage"]
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
            f"curl -fsSL --retry 5 --retry-all-errors -o /tmp/otelcol-scraper.tgz {COLLECTOR_RELEASE_URL}",
            f"tar -xzf /tmp/otelcol-scraper.tgz -C {MACHINE_BINARY_PATH.rsplit('/', 1)[0]} otelcol-contrib",
            "rm -f /tmp/otelcol-scraper.tgz",
            "systemctl daemon-reload",
            "systemctl enable --now otelcol-scraper",
        ],
    }


def merge_userdata(existing: str | None, addition: dict[str, Any]) -> str:
    """Merge *addition* into an existing ``cloudinit-userdata`` value, keeping the user's entries."""
    merged: dict[str, Any] = yaml.safe_load(existing) if existing else {}
    merged = merged or {}
    for key, value in addition.items():
        current = merged.get(key)
        merged[key] = [*current, *value] if isinstance(current, list) else value
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
                            "securityContext": {"runAsUser": 0},
                            "volumeMounts": [
                                {"name": "config", "mountPath": "/etc/otelcol"},
                                {"name": "pods", "mountPath": KUBERNETES_POD_LOG_ROOT, "readOnly": True},
                                {"name": "storage", "mountPath": "/var/lib/otelcol"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "config", "configMap": {"name": KUBERNETES_SCRAPER_NAME}},
                        {"name": "pods", "hostPath": {"path": KUBERNETES_POD_LOG_ROOT}},
                        {
                            "name": "storage",
                            "hostPath": {
                                "path": f"{KUBERNETES_STORAGE_ROOT}/{controller}",
                                "type": "DirectoryOrCreate",
                            },
                        },
                    ],
                },
            },
        },
    }
