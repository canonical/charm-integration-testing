#!/usr/bin/env python3
"""Bundle-builder-x benchmark suite.

Builds 200 single-charm specs + 50 complex scenarios, records results as JSON,
and can compare two result sets (main vs branch) to surface regressions and
improvements.

Usage (from repo root, inside bundle_builder_x poetry env):

    # Run benchmark on current branch:
    cd bundle_builder_x
    poetry run python ../scripts/benchmark_suite.py run \\
        --output ../results/$(git rev-parse --abbrev-ref HEAD).json \\
        --overrides ../static/charm-overrides \\
        --workers 6

    # Run on main (with aggressive hard timeout since it may hang):
    git worktree add /tmp/bb-main main
    cd /tmp/bb-main/bundle_builder_x && poetry install -q
    poetry run python ../scripts/benchmark_suite.py run \\
        --output ../results/main.json \\
        --overrides ../static/charm-overrides \\
        --workers 4 \\
        --hard-timeout 90

    # Compare:
    cd <repo>
    poetry run python scripts/benchmark_suite.py compare \\
        results/main.json results/<branch>.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Spec definitions
# ---------------------------------------------------------------------------

# ── helpers ─────────────────────────────────────────────────────────────────

def _single(name: str, platform: str, channel: str = "latest/stable") -> dict[str, Any]:
    """Minimal single-charm spec dict."""
    plat_short = "k8s" if platform == "kubernetes" else "mach"
    return {
        "id": f"single-{name}-{plat_short}",
        "category": "single-charm",
        "platform": platform,
        "spec": {
            "models": [
                {
                    "name": "target",
                    "platform": platform,
                    "applications": {"target": {"charm": name, "channel": channel}},
                }
            ]
        },
    }


def _multi(spec_id: str, models: list[dict[str, Any]], category: str = "complex") -> dict[str, Any]:
    return {"id": spec_id, "category": category, "platform": "mixed", "spec": {"models": models}}


def _k8s_model(
    name: str,
    apps: dict[str, Any],
    integrations: list[dict[str, Any]] | None = None,
    controller: str | None = None,
) -> dict[str, Any]:
    m: dict[str, Any] = {"name": name, "platform": "kubernetes", "applications": apps}
    if integrations:
        m["integrations"] = integrations
    if controller:
        m["controller"] = controller
    return m


def _machine_model(
    name: str,
    apps: dict[str, Any],
    integrations: list[dict[str, Any]] | None = None,
    controller: str | None = None,
) -> dict[str, Any]:
    m: dict[str, Any] = {"name": name, "platform": "machine", "applications": apps}
    if integrations:
        m["integrations"] = integrations
    if controller:
        m["controller"] = controller
    return m


def _app(charm: str, channel: str = "latest/stable", **kw: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"charm": charm, "channel": channel}
    d.update(kw)
    return d


def _integration(
    app: str, ep: str, remote_app: str, remote_ep: str,
    remote_model: str | None = None,
) -> dict[str, Any]:
    d: dict[str, Any] = {"application": app, "endpoint": ep, "remote_application": remote_app, "remote_endpoint": remote_ep}
    if remote_model:
        d["remote_model"] = remote_model
    return d


# ── 200 single-charm specs ───────────────────────────────────────────────────
# Platform: "k8s" or "kubernetes" in name → kubernetes, else → machine
# Channel: latest/stable for most; specific tracks where the override mandates it.

_K8S_CHARMS = [
    ("alertmanager-k8s", "latest/stable"),
    ("canonical-livepatch-server-k8s", "latest/stable"),
    ("catalogue-k8s", "latest/stable"),
    ("charmed-osm-mariadb-k8s", "latest/stable"),
    ("content-cache-k8s", "latest/stable"),
    ("discourse-k8s", "latest/stable"),
    ("elasticsearch-k8s", "latest/stable"),
    ("glauth-k8s", "latest/stable"),
    ("grafana-agent-k8s", "latest/stable"),
    ("graylog-k8s", "latest/stable"),
    ("istio-beacon-k8s", "latest/stable"),
    ("jenkins-k8s", "latest/stable"),
    ("juju-jimm-k8s", "latest/stable"),
    ("k8s-worker", "latest/stable"),
    ("k8s", "latest/stable"),
    ("kafka-k8s", "latest/stable"),
    ("kubernetes-control-plane", "latest/stable"),
    ("kubernetes-worker", "latest/stable"),
    ("mattermost-k8s", "latest/stable"),
    ("mongodb-k8s", "latest/stable"),
    ("mongos-k8s", "latest/stable"),
    ("mysql-k8s", "8.0/stable"),
    ("mysql-router-k8s", "8.0/stable"),
    ("openfga-k8s", "latest/stable"),
    ("opentelemetry-collector-k8s", "latest/stable"),
    ("pgbouncer-k8s", "latest/stable"),
    ("postgresql-k8s", "14/stable"),
    ("prometheus-k8s", "latest/stable"),
    ("ranger-k8s", "latest/stable"),
    ("redis-k8s", "latest/stable"),
    ("spark-history-server-k8s", "latest/stable"),
    ("spark-integration-hub-k8s", "latest/stable"),
    ("superset-k8s", "latest/stable"),
    ("temporal-admin-k8s", "latest/stable"),
    ("temporal-k8s", "latest/stable"),
    ("temporal-ui-k8s", "latest/stable"),
    ("temporal-worker-k8s", "latest/stable"),
    ("traefik-k8s", "latest/stable"),
    ("trino-k8s", "latest/stable"),
    ("vault-k8s", "latest/stable"),
    ("wordpress-k8s", "latest/stable"),
    ("zookeeper-k8s", "latest/stable"),
    # extra k8s charms not in overrides
    ("grafana-k8s", "latest/stable"),
    ("loki-k8s", "latest/stable"),
    ("tempo-coordinator-k8s", "latest/stable"),
    ("tempo-worker-k8s", "latest/stable"),
    ("self-signed-certificates", "latest/stable"),
    ("hydra", "latest/stable"),
    ("kratos", "latest/stable"),
    ("identity-platform-login-ui-operator", "latest/stable"),
    ("dex-auth", "latest/stable"),
    ("istio-gateway", "latest/stable"),
    ("istio-pilot", "latest/stable"),
    ("mlflow-server", "latest/stable"),
    ("s3-integrator", "latest/stable"),
    ("parca-k8s", "latest/stable"),
    ("cos-proxy", "latest/stable"),
    ("opa-k8s", "latest/stable"),
    ("nginx-ingress-integrator", "latest/stable"),
]

_MACHINE_CHARMS = [
    ("aar", "latest/stable"),
    ("admission-webhook", "latest/stable"),
    ("ams", "latest/stable"),
    ("ams-load-balancer", "latest/stable"),
    ("ams-lxd", "latest/stable"),
    ("anbox-cloud-dashboard", "latest/stable"),
    ("anbox-stream-agent", "latest/stable"),
    ("anbox-stream-gateway", "latest/stable"),
    ("aodh", "latest/stable"),
    ("apache2", "latest/stable"),
    ("arangodb", "latest/stable"),
    ("argo-controller", "latest/stable"),
    ("canal", "latest/stable"),
    ("canonical-livepatch-server", "latest/stable"),
    ("ceilometer-agent", "latest/stable"),
    ("ceph-dashboard", "latest/stable"),
    ("ceph-mon", "latest/stable"),
    ("ceph-osd", "latest/stable"),
    ("ceph-radosgw", "latest/stable"),
    ("ceph-rbd-mirror", "latest/stable"),
    ("charmed-etcd", "latest/stable"),
    ("chrony", "latest/stable"),
    ("cilium", "latest/stable"),
    ("cinder", "latest/stable"),
    ("containerd", "latest/stable"),
    ("containers-flannel", "latest/stable"),
    ("data-integrator", "latest/stable"),
    ("dex-auth", "latest/stable"),
    ("docker-registry", "latest/stable"),
    ("envoy", "latest/stable"),
    ("etcd", "3.4/stable"),
    ("feast-integrator", "latest/stable"),
    ("feast-ui", "latest/stable"),
    ("flannel", "latest/stable"),
    ("glance", "latest/stable"),
    ("hacluster", "latest/stable"),
    ("haproxy", "latest/stable"),
    ("heat", "latest/stable"),
    ("keystone", "latest/stable"),
    ("lxd", "5.0/stable"),
    ("maas-agent", "latest/stable"),
    ("maas-rack-controller", "latest/stable"),
    ("maas-region-controller", "latest/stable"),
    ("manila", "latest/stable"),
    ("memcached", "latest/stable"),
    ("microceph", "latest/stable"),
    ("mysql", "8.0/stable"),
    ("mysql-router", "8.0/stable"),
    ("nats", "latest/stable"),
    ("netbox", "latest/stable"),
    ("neutron-gateway", "latest/stable"),
    ("nova-cloud-controller", "latest/stable"),
    ("nova-compute", "latest/stable"),
    ("nrpe", "latest/stable"),
    ("octavia", "latest/stable"),
    ("openstack-dashboard", "latest/stable"),
    ("opensearch", "latest/stable"),
    ("pgbouncer", "latest/stable"),
    ("postgresql", "14/stable"),
    ("rabbitmq-server", "3.9/stable"),
    ("redis", "latest/stable"),
    ("rsyslog", "latest/stable"),
    ("rsyslog-forwarder-ha", "latest/stable"),
    ("s3-integrator", "latest/stable"),
    ("self-signed-certificates", "latest/stable"),
    ("slurmctld", "latest/stable"),
    ("slurmd", "latest/stable"),
    ("slurmdbd", "latest/stable"),
    ("slurmrestd", "latest/stable"),
    ("smtp-integrator", "latest/stable"),
    ("telegraf", "latest/stable"),
    ("ubuntu-advantage", "latest/stable"),
    ("unbound", "latest/stable"),
    ("vault", "latest/stable"),
    ("wazuh-agent", "latest/stable"),
    ("wazuh-dashboard", "latest/stable"),
    ("wazuh-indexer", "latest/stable"),
    ("wazuh-server", "latest/stable"),
    ("zookeeper", "3/stable"),
    # Extra machine charms not in overrides
    ("kafka", "3/stable"),
    ("opensearch-dashboards", "2/stable"),
    ("temporal-server", "latest/stable"),
    ("temporal-ui", "latest/stable"),
    ("temporal-worker", "latest/stable"),
    ("sysconfig", "latest/stable"),
    ("hardware-observer", "latest/stable"),
    ("landscape-client", "latest/stable"),
    ("ubuntu-pro", "latest/stable"),
    ("prometheus-scrape-config-k8s", "latest/stable"),
]

SINGLE_CHARM_SPECS: list[dict[str, Any]] = []
for charm, channel in _K8S_CHARMS:
    SINGLE_CHARM_SPECS.append(_single(charm, "kubernetes", channel))
for charm, channel in _MACHINE_CHARMS:
    # skip anything that's clearly a k8s charm accidentally in this list
    if "k8s" not in charm and "kubernetes" not in charm:
        SINGLE_CHARM_SPECS.append(_single(charm, "machine", channel))

# Pad with alternate-channel k8s variants to reach 200
_EXTRA_K8S: list[tuple[str, str]] = [
    ("prometheus-scrape-config-k8s", "latest/stable"),
    ("prometheus-pushgateway-k8s", "latest/stable"),
    ("alertmanager-k8s", "2/stable"),
    ("grafana-k8s", "latest/edge"),
    ("loki-k8s", "latest/edge"),
    ("tempo-coordinator-k8s", "latest/edge"),
    ("mysql-k8s", "latest/stable"),
    ("postgresql-k8s", "16/stable"),
    ("vault-k8s", "latest/edge"),
    ("openfga-k8s", "latest/edge"),
    ("traefik-k8s", "latest/edge"),
    ("redis-k8s", "latest/edge"),
    ("catalogue-k8s", "latest/edge"),
    ("discourse-k8s", "latest/edge"),
    ("mattermost-k8s", "latest/edge"),
    ("elasticsearch-k8s", "latest/edge"),
    ("wordpress-k8s", "latest/edge"),
    ("content-cache-k8s", "latest/edge"),
    ("grafana-agent-k8s", "latest/edge"),
    ("kafka-k8s", "latest/edge"),
    ("mongodb-k8s", "latest/edge"),
    ("postgresql-k8s", "15/stable"),
    ("mysql-k8s", "8.0/edge"),
    ("opentelemetry-collector-k8s", "latest/edge"),
    ("tempo-worker-k8s", "latest/edge"),
]
_idx = len(SINGLE_CHARM_SPECS)
for charm, channel in _EXTRA_K8S:
    if _idx >= 200:
        break
    spec_id = f"single-{charm}-{channel.replace('/', '-')}-k8s"
    SINGLE_CHARM_SPECS.append({
        "id": spec_id,
        "category": "single-charm",
        "platform": "kubernetes",
        "spec": {
            "models": [{
                "name": "target",
                "platform": "kubernetes",
                "applications": {"target": {"charm": charm, "channel": channel}},
            }]
        },
    })
    _idx += 1

# Pad with alternate-channel machine variants if still under 200
_EXTRA_MACHINE: list[tuple[str, str]] = [
    ("postgresql", "16/stable"),
    ("mysql", "latest/stable"),
    ("kafka", "latest/stable"),
    ("zookeeper", "latest/stable"),
    ("vault", "latest/edge"),
    ("opensearch", "2/stable"),
    ("rabbitmq-server", "latest/stable"),
    ("nrpe", "latest/edge"),
    ("telegraf", "latest/edge"),
    ("haproxy", "latest/edge"),
    ("hacluster", "latest/edge"),
    ("ceph-mon", "quincy/stable"),
    ("ceph-osd", "quincy/stable"),
    ("neutron-gateway", "latest/edge"),
    ("nova-compute", "latest/edge"),
    ("nova-cloud-controller", "latest/edge"),
    ("openstack-dashboard", "latest/edge"),
    ("keystone", "latest/edge"),
    ("cinder", "latest/edge"),
    ("glance", "latest/edge"),
    ("postgresql", "15/stable"),
    ("mysql", "8.0/edge"),
    ("opensearch", "latest/stable"),
    ("etcd", "latest/stable"),
    ("kafka", "latest/edge"),
    ("charmed-etcd", "latest/edge"),
    ("microceph", "quincy/stable"),
    ("ceph-dashboard", "latest/edge"),
]
for charm, channel in _EXTRA_MACHINE:
    if _idx >= 200:
        break
    spec_id = f"single-{charm}-{channel.replace('/', '-')}-mach"
    SINGLE_CHARM_SPECS.append({
        "id": spec_id,
        "category": "single-charm",
        "platform": "machine",
        "spec": {
            "models": [{
                "name": "target",
                "platform": "machine",
                "applications": {"target": {"charm": charm, "channel": channel}},
            }]
        },
    })
    _idx += 1

SINGLE_CHARM_SPECS = SINGLE_CHARM_SPECS[:200]


# ── 50 complex scenarios ─────────────────────────────────────────────────────

COMPLEX_SCENARIOS: list[dict[str, Any]] = [

    # ── COS observability stack (k8s) ──────────────────────────────────────
    _multi("cos-full-stack", [_k8s_model("cos", {
        "prometheus": _app("prometheus-k8s"),
        "grafana": _app("grafana-k8s"),
        "loki": _app("loki-k8s"),
        "alertmanager": _app("alertmanager-k8s"),
    }, integrations=[
        _integration("grafana", "grafana-source", "prometheus", "grafana-source"),
        _integration("grafana", "grafana-source", "loki", "grafana-source"),
        _integration("prometheus", "alertmanager", "alertmanager", "alerting"),
        _integration("grafana", "grafana-dashboard", "alertmanager", "grafana-dashboard"),
    ])], category="multi-app"),

    # ── OTC self-monitoring (the original hang scenario) ──────────────────
    _multi("otc-self-monitor", [_k8s_model("target", {
        "otelcol": _app("opentelemetry-collector-k8s"),
    })], category="multi-app"),

    # ── OTC + mysql-k8s ───────────────────────────────────────────────────
    _multi("otc-mysql", [_k8s_model("target", {
        "otelcol": _app("opentelemetry-collector-k8s"),
        "mysql": _app("mysql-k8s", channel="8.0/stable"),
    }, integrations=[
        _integration("mysql", "tracing", "otelcol", "receive-traces"),
    ])], category="multi-app"),

    # ── OTC + alertmanager ────────────────────────────────────────────────
    _multi("otc-alertmanager", [_k8s_model("target", {
        "otelcol": _app("opentelemetry-collector-k8s"),
        "alertmanager": _app("alertmanager-k8s"),
    }, integrations=[
        _integration("alertmanager", "tracing", "otelcol", "receive-traces"),
    ])], category="multi-app"),

    # ── OTC + openfga-k8s ─────────────────────────────────────────────────
    _multi("otc-openfga", [_k8s_model("target", {
        "otelcol": _app("opentelemetry-collector-k8s"),
        "openfga": _app("openfga-k8s"),
    }, integrations=[
        _integration("openfga", "tracing", "otelcol", "receive-traces"),
    ])], category="multi-app"),

    # ── Identity platform stack ───────────────────────────────────────────
    _multi("identity-platform", [_k8s_model("idp", {
        "hydra": _app("hydra"),
        "kratos": _app("kratos"),
        "login-ui": _app("identity-platform-login-ui-operator"),
        "dex": _app("dex-auth"),
        "traefik": _app("traefik-k8s"),
    }, integrations=[
        _integration("login-ui", "hydra-endpoint-info", "hydra", "hydra-endpoint-info"),
        _integration("login-ui", "kratos-info", "kratos", "kratos-info"),
        _integration("dex", "dex-oidc-config", "hydra", "dex-oidc-config"),
    ])], category="multi-app"),

    # ── MySQL cluster (primary + router) ─────────────────────────────────
    _multi("mysql-cluster-k8s", [_k8s_model("data", {
        "mysql": _app("mysql-k8s", channel="8.0/stable"),
        "router": _app("mysql-router-k8s", channel="8.0/stable"),
    }, integrations=[
        _integration("router", "backend-database", "mysql", "database"),
    ])], category="multi-app"),

    # ── PostgreSQL + pgbouncer ────────────────────────────────────────────
    _multi("postgres-pgbouncer-k8s", [_k8s_model("data", {
        "pg": _app("postgresql-k8s", channel="14/stable"),
        "pgb": _app("pgbouncer-k8s"),
    }, integrations=[
        _integration("pgb", "backend-database", "pg", "database"),
    ])], category="multi-app"),

    # ── Temporal full stack ───────────────────────────────────────────────
    _multi("temporal-stack-k8s", [_k8s_model("temporal", {
        "server": _app("temporal-k8s"),
        "admin": _app("temporal-admin-k8s"),
        "ui": _app("temporal-ui-k8s"),
        "worker": _app("temporal-worker-k8s"),
    }, integrations=[
        _integration("admin", "temporal", "server", "temporal"),
        _integration("ui", "temporal", "server", "temporal"),
        _integration("worker", "temporal", "server", "temporal"),
    ])], category="multi-app"),

    # ── Kafka + Zookeeper (machine) ───────────────────────────────────────
    _multi("kafka-zookeeper", [_machine_model("kafka", {
        "kafka": _app("kafka", channel="3/stable"),
        "zk": _app("zookeeper", channel="3/stable"),
    }, integrations=[
        _integration("kafka", "zookeeper", "zk", "zookeeper"),
    ])], category="multi-app"),

    # ── Ceph cluster (machine) ────────────────────────────────────────────
    _multi("ceph-cluster", [_machine_model("storage", {
        "ceph-mon": _app("ceph-mon", channel="quincy/stable"),
        "ceph-osd": _app("ceph-osd", channel="quincy/stable"),
        "radosgw": _app("ceph-radosgw", channel="quincy/stable"),
    }, integrations=[
        _integration("ceph-osd", "mon", "ceph-mon", "osd"),
        _integration("radosgw", "mon", "ceph-mon", "radosgw"),
    ])], category="multi-app"),

    # ── Vault + self-signed-certs ─────────────────────────────────────────
    _multi("vault-certs-k8s", [_k8s_model("security", {
        "vault": _app("vault-k8s"),
        "ssc": _app("self-signed-certificates"),
    })], category="multi-app"),

    # ── OpenFGA + postgresql ──────────────────────────────────────────────
    _multi("openfga-postgres-k8s", [_k8s_model("fga", {
        "openfga": _app("openfga-k8s"),
        "pg": _app("postgresql-k8s", channel="14/stable"),
    }, integrations=[
        _integration("openfga", "database", "pg", "database"),
    ])], category="multi-app"),

    # ── Discourse + redis ─────────────────────────────────────────────────
    _multi("discourse-stack-k8s", [_k8s_model("forum", {
        "discourse": _app("discourse-k8s"),
        "redis": _app("redis-k8s"),
        "pg": _app("postgresql-k8s", channel="14/stable"),
    }, integrations=[
        _integration("discourse", "database", "pg", "database"),
        _integration("discourse", "redis", "redis", "redis"),
    ])], category="multi-app"),

    # ── OpenStack nova stack (machine) ────────────────────────────────────
    _multi("openstack-nova", [_machine_model("openstack", {
        "nova-cc": _app("nova-cloud-controller"),
        "nova-compute": _app("nova-compute"),
        "keystone": _app("keystone"),
        "mysql": _app("mysql", channel="8.0/stable"),
        "rabbitmq": _app("rabbitmq-server", channel="3.9/stable"),
    }, integrations=[
        _integration("nova-cc", "shared-db", "mysql", "shared-db"),
        _integration("nova-cc", "amqp", "rabbitmq", "amqp"),
        _integration("keystone", "shared-db", "mysql", "shared-db"),
        _integration("nova-cc", "identity-service", "keystone", "identity-service"),
        _integration("nova-compute", "cloud-compute", "nova-cc", "cloud-compute"),
        _integration("nova-compute", "amqp", "rabbitmq", "amqp"),
    ])], category="multi-app"),

    # ── HA PostgreSQL pair (machine) ──────────────────────────────────────
    _multi("postgres-ha", [_machine_model("data", {
        "pg": _app("postgresql", channel="14/stable"),
        "hacluster": _app("hacluster"),
    }, integrations=[
        _integration("pg", "ha", "hacluster", "ha"),
    ])], category="multi-app"),

    # ── CMR: app-model sends metrics to cos-model ─────────────────────────
    _multi("cmr-metrics-to-cos", [
        _k8s_model("cos", {
            "prometheus": _app("prometheus-k8s"),
            "grafana": _app("grafana-k8s"),
        }, integrations=[
            _integration("grafana", "grafana-source", "prometheus", "grafana-source"),
        ], controller="ctrl"),
        _k8s_model("apps", {
            "mysql": _app("mysql-k8s", channel="8.0/stable"),
        }, integrations=[
            _integration("mysql", "metrics-endpoint", "prometheus", "metrics-endpoint",
                         remote_model="cos"),
        ]),
    ], category="cmr"),

    # ── CMR: app-model sends traces to cos-model ──────────────────────────
    _multi("cmr-traces-to-cos", [
        _k8s_model("cos", {
            "tempo": _app("tempo-coordinator-k8s"),
        }, controller="ctrl"),
        _k8s_model("apps", {
            "mysql": _app("mysql-k8s", channel="8.0/stable"),
            "pg": _app("postgresql-k8s", channel="14/stable"),
        }, integrations=[
            _integration("mysql", "tracing", "tempo", "tracing",
                         remote_model="cos"),
            _integration("pg", "tracing", "tempo", "tracing",
                         remote_model="cos"),
        ]),
    ], category="cmr"),

    # ── CMR: logs from app-model to cos-model loki ────────────────────────
    _multi("cmr-logs-to-loki", [
        _k8s_model("cos", {
            "loki": _app("loki-k8s"),
            "grafana": _app("grafana-k8s"),
        }, integrations=[
            _integration("grafana", "grafana-source", "loki", "grafana-source"),
        ], controller="ctrl"),
        _k8s_model("apps", {
            "discourse": _app("discourse-k8s"),
        }, integrations=[
            _integration("discourse", "logging", "loki", "logging",
                         remote_model="cos"),
        ]),
    ], category="cmr"),

    # ── CMR: certs from security-model to app-model ───────────────────────
    _multi("cmr-certs-provider", [
        _k8s_model("security", {
            "ssc": _app("self-signed-certificates"),
        }, controller="ctrl"),
        _k8s_model("apps", {
            "traefik": _app("traefik-k8s"),
            "catalogue": _app("catalogue-k8s"),
        }, integrations=[
            _integration("traefik", "certificates", "ssc", "certificates",
                         remote_model="security"),
        ]),
    ], category="cmr"),

    # ── CMR: database from data-model to app-model ────────────────────────
    _multi("cmr-database-cross-model", [
        _k8s_model("data", {
            "pg": _app("postgresql-k8s", channel="14/stable"),
        }, controller="ctrl"),
        _k8s_model("apps", {
            "openfga": _app("openfga-k8s"),
            "discourse": _app("discourse-k8s"),
        }, integrations=[
            _integration("openfga", "database", "pg", "database", remote_model="data"),
            _integration("discourse", "database", "pg", "database", remote_model="data"),
        ]),
    ], category="cmr"),

    # ── CMR: 3-model chain (data → apps → cos) ────────────────────────────
    _multi("cmr-three-model-chain", [
        _k8s_model("data", {
            "pg": _app("postgresql-k8s", channel="14/stable"),
        }, controller="ctrl"),
        _k8s_model("apps", {
            "openfga": _app("openfga-k8s"),
        }, integrations=[
            _integration("openfga", "database", "pg", "database", remote_model="data"),
        ], controller="ctrl2"),
        _k8s_model("cos", {
            "prometheus": _app("prometheus-k8s"),
        }, integrations=[
            _integration("prometheus", "metrics-endpoint", "openfga", "metrics-endpoint",
                         remote_model="apps"),
        ]),
    ], category="cmr"),

    # ── CMR: grafana in cos-model consuming dashboards from app-model ─────
    _multi("cmr-grafana-dashboards", [
        _k8s_model("cos", {
            "grafana": _app("grafana-k8s"),
        }, controller="ctrl"),
        _k8s_model("apps", {
            "mysql": _app("mysql-k8s", channel="8.0/stable"),
            "alertmanager": _app("alertmanager-k8s"),
        }, integrations=[
            _integration("mysql", "grafana-dashboard", "grafana", "grafana-dashboard",
                         remote_model="cos"),
            _integration("alertmanager", "grafana-dashboard", "grafana", "grafana-dashboard",
                         remote_model="cos"),
        ]),
    ], category="cmr"),

    # ── CMR: alertmanager in cos-model, app in app-model ─────────────────
    _multi("cmr-alertmanager-remote", [
        _k8s_model("cos", {
            "alertmanager": _app("alertmanager-k8s"),
        }, controller="ctrl"),
        _k8s_model("apps", {
            "mysql": _app("mysql-k8s", channel="8.0/stable"),
        }, integrations=[
            _integration("mysql", "alerting", "alertmanager", "alerting",
                         remote_model="cos"),
        ]),
    ], category="cmr"),

    # ── CMR: vault in security-model, app consumes cert ──────────────────
    _multi("cmr-vault-certs", [
        _k8s_model("security", {
            "vault": _app("vault-k8s"),
        }, controller="ctrl"),
        _k8s_model("apps", {
            "discourse": _app("discourse-k8s"),
            "redis": _app("redis-k8s"),
        }, integrations=[
            _integration("discourse", "certificates", "vault", "provide-vault-token",
                         remote_model="security"),
        ]),
    ], category="cmr"),

    # ── CMR: s3-integrator in storage-model, spark uses it ───────────────
    _multi("cmr-s3-spark", [
        _k8s_model("storage", {
            "s3": _app("s3-integrator"),
        }, controller="ctrl"),
        _k8s_model("compute", {
            "spark-hub": _app("spark-integration-hub-k8s"),
            "spark-history": _app("spark-history-server-k8s"),
        }, integrations=[
            _integration("spark-history", "s3", "s3", "s3-credentials", remote_model="storage"),
        ]),
    ], category="cmr"),

    # ── Machine-model CMR: postgres in data providing to app in apps ──────
    _multi("cmr-machine-postgres", [
        _machine_model("data", {
            "pg": _app("postgresql", channel="14/stable"),
        }, controller="ctrl"),
        _machine_model("apps", {
            "netbox": _app("netbox"),
        }, integrations=[
            _integration("netbox", "database", "pg", "database", remote_model="data"),
        ]),
    ], category="cmr"),

    # ── Limit-1 scenario: two consumers need dedicated cert providers ─────
    _multi("limit1-certs-two-consumers", [_k8s_model("target", {
        "ssc": _app("self-signed-certificates"),
        "traefik": _app("traefik-k8s"),
        "mysql": _app("mysql-k8s", channel="8.0/stable"),
    }, integrations=[
        _integration("traefik", "certificates", "ssc", "certificates"),
        _integration("mysql", "certificates", "ssc", "certificates"),
    ])], category="limit-capacity"),

    # ── Three consumers, one unlimited tracing provider ───────────────────
    _multi("unlimited-tracing-three-consumers", [_k8s_model("target", {
        "otelcol": _app("opentelemetry-collector-k8s"),
        "mysql": _app("mysql-k8s", channel="8.0/stable"),
        "pg": _app("postgresql-k8s", channel="14/stable"),
        "discourse": _app("discourse-k8s"),
    }, integrations=[
        _integration("mysql", "tracing", "otelcol", "receive-traces"),
        _integration("pg", "tracing", "otelcol", "receive-traces"),
        _integration("discourse", "tracing", "otelcol", "receive-traces"),
    ])], category="limit-capacity"),

    # ── Cyclic dep: dex-auth ↔ hydra ─────────────────────────────────────
    _multi("cyclic-dex-hydra", [_k8s_model("idp", {
        "dex": _app("dex-auth"),
        "hydra": _app("hydra"),
    }, integrations=[
        _integration("dex", "dex-oidc-config", "hydra", "dex-oidc-config"),
    ])], category="cyclic"),

    # ── At-least-one: login-ui needs kratos or hydra ──────────────────────
    _multi("at-least-one-login-ui", [_k8s_model("idp", {
        "login-ui": _app("identity-platform-login-ui-operator"),
        "kratos": _app("kratos"),
    }, integrations=[
        _integration("login-ui", "kratos-info", "kratos", "kratos-info"),
    ])], category="at-least-one"),

    # ── Grafana agent pulling from multiple sources ───────────────────────
    _multi("grafana-agent-multi-source", [_k8s_model("target", {
        "agent": _app("grafana-agent-k8s"),
        "mysql": _app("mysql-k8s", channel="8.0/stable"),
        "pg": _app("postgresql-k8s", channel="14/stable"),
        "redis": _app("redis-k8s"),
    }, integrations=[
        _integration("agent", "metrics-endpoint", "mysql", "metrics-endpoint"),
        _integration("agent", "metrics-endpoint", "pg", "metrics-endpoint"),
        _integration("agent", "metrics-endpoint", "redis", "metrics-endpoint"),
    ])], category="multi-app"),

    # ── Ingress stack ─────────────────────────────────────────────────────
    _multi("ingress-stack-k8s", [_k8s_model("target", {
        "traefik": _app("traefik-k8s"),
        "ssc": _app("self-signed-certificates"),
        "catalogue": _app("catalogue-k8s"),
    }, integrations=[
        _integration("traefik", "certificates", "ssc", "certificates"),
        _integration("catalogue", "ingress", "traefik", "ingress"),
    ])], category="multi-app"),

    # ── GlAuth + LDAP consumers ───────────────────────────────────────────
    _multi("glauth-stack", [_k8s_model("idp", {
        "glauth": _app("glauth-k8s"),
        "pg": _app("postgresql-k8s", channel="14/stable"),
    }, integrations=[
        _integration("glauth", "pg", "pg", "database"),
    ])], category="multi-app"),

    # ── Loki distributed (loki + s3) ─────────────────────────────────────
    _multi("loki-s3-backend", [_k8s_model("cos", {
        "loki": _app("loki-k8s"),
        "s3": _app("s3-integrator"),
    }, integrations=[
        _integration("loki", "s3", "s3", "s3-credentials"),
    ])], category="multi-app"),

    # ── Tempo distributed (coordinator + worker + s3) ────────────────────
    _multi("tempo-distributed", [_k8s_model("cos", {
        "tempo": _app("tempo-coordinator-k8s"),
        "worker": _app("tempo-worker-k8s"),
        "s3": _app("s3-integrator"),
    }, integrations=[
        _integration("tempo", "tempo-cluster", "worker", "tempo-cluster"),
        _integration("tempo", "s3", "s3", "s3-credentials"),
    ])], category="multi-app"),

    # ── Wazuh stack (machine) ─────────────────────────────────────────────
    _multi("wazuh-stack", [_machine_model("wazuh", {
        "indexer": _app("wazuh-indexer"),
        "server": _app("wazuh-server"),
        "dashboard": _app("wazuh-dashboard"),
    }, integrations=[
        _integration("server", "cluster-api", "indexer", "cluster-api"),
        _integration("dashboard", "cluster-api", "indexer", "cluster-api"),
        _integration("server", "juju-info", "indexer", "juju-info"),
    ])], category="multi-app"),

    # ── Spark + MLflow (k8s) ──────────────────────────────────────────────
    _multi("spark-mlflow-k8s", [_k8s_model("ml", {
        "spark-hub": _app("spark-integration-hub-k8s"),
        "spark-history": _app("spark-history-server-k8s"),
        "s3": _app("s3-integrator"),
    }, integrations=[
        _integration("spark-history", "s3", "s3", "s3-credentials"),
        _integration("spark-hub", "s3-credentials", "s3", "s3-credentials"),
    ])], category="multi-app"),

    # ── MongoDB cluster + mongos ───────────────────────────────────────────
    _multi("mongodb-mongos-k8s", [_k8s_model("data", {
        "mongodb": _app("mongodb-k8s"),
        "mongos": _app("mongos-k8s"),
    }, integrations=[
        _integration("mongos", "backend-database", "mongodb", "database-scoped"),
    ])], category="multi-app"),

    # ── Content-cache-k8s (deep dependency chain) ─────────────────────────
    _multi("content-cache-stack", [_k8s_model("target", {
        "cc": _app("content-cache-k8s"),
    })], category="multi-app"),

    # ── Full COS + app-model (k8s) ────────────────────────────────────────
    _multi("full-cos-with-app", [
        _k8s_model("cos", {
            "prometheus": _app("prometheus-k8s"),
            "grafana": _app("grafana-k8s"),
            "loki": _app("loki-k8s"),
            "alertmanager": _app("alertmanager-k8s"),
            "tempo": _app("tempo-coordinator-k8s"),
            "s3": _app("s3-integrator"),
        }, integrations=[
            _integration("grafana", "grafana-source", "prometheus", "grafana-source"),
            _integration("grafana", "grafana-source", "loki", "grafana-source"),
            _integration("prometheus", "alertmanager", "alertmanager", "alerting"),
            _integration("tempo", "s3", "s3", "s3-credentials"),
        ], controller="ctrl"),
        _k8s_model("apps", {
            "mysql": _app("mysql-k8s", channel="8.0/stable"),
            "pg": _app("postgresql-k8s", channel="14/stable"),
        }, integrations=[
            _integration("mysql", "metrics-endpoint", "prometheus", "metrics-endpoint",
                         remote_model="cos"),
            _integration("pg", "metrics-endpoint", "prometheus", "metrics-endpoint",
                         remote_model="cos"),
            _integration("mysql", "tracing", "tempo", "tracing",
                         remote_model="cos"),
        ]),
    ], category="cmr"),

    # ── Charmed Kubernetes control plane ─────────────────────────────────
    _multi("ck-control-plane", [_machine_model("ck", {
        "kcp": _app("kubernetes-control-plane", channel="1.31/stable"),
        "etcd": _app("charmed-etcd", channel="latest/stable"),
        "containerd": _app("containerd"),
    }, integrations=[
        _integration("kcp", "etcd", "etcd", "db"),
        _integration("kcp", "container-runtime", "containerd", "containerd"),
    ])], category="multi-app"),

    # ── Charmed Kubernetes worker ─────────────────────────────────────────
    _multi("ck-worker", [_machine_model("ck", {
        "kcp": _app("kubernetes-control-plane", channel="1.31/stable"),
        "kw": _app("kubernetes-worker", channel="1.31/stable"),
        "containerd": _app("containerd"),
        "flannel": _app("flannel"),
    }, integrations=[
        _integration("kw", "kube-control", "kcp", "kube-control"),
        _integration("kw", "container-runtime", "containerd", "containerd"),
        _integration("kw", "cni", "flannel", "cni"),
        _integration("kcp", "cni", "flannel", "cni"),
    ])], category="multi-app"),

    # ── SLURM cluster (machine) ───────────────────────────────────────────
    _multi("slurm-cluster", [_machine_model("hpc", {
        "slurmctld": _app("slurmctld"),
        "slurmd": _app("slurmd"),
        "slurmdbd": _app("slurmdbd"),
        "slurmrestd": _app("slurmrestd"),
    }, integrations=[
        _integration("slurmd", "slurmctld", "slurmctld", "slurmd"),
        _integration("slurmdbd", "slurmctld", "slurmctld", "slurmdbd"),
        _integration("slurmrestd", "slurmctld", "slurmctld", "slurmrestd"),
    ])], category="multi-app"),

    # ── Superset + metadata db (k8s) ──────────────────────────────────────
    _multi("superset-stack", [_k8s_model("bi", {
        "superset": _app("superset-k8s"),
        "pg": _app("postgresql-k8s", channel="14/stable"),
        "redis": _app("redis-k8s"),
    }, integrations=[
        _integration("superset", "db", "pg", "database"),
        _integration("superset", "redis", "redis", "redis"),
    ])], category="multi-app"),

    # ── Trino + data connectors (k8s) ─────────────────────────────────────
    _multi("trino-stack", [_k8s_model("query", {
        "trino": _app("trino-k8s"),
        "pg": _app("postgresql-k8s", channel="14/stable"),
        "s3": _app("s3-integrator"),
    }, integrations=[
        _integration("trino", "db", "pg", "database"),
        _integration("trino", "s3", "s3", "s3-credentials"),
    ])], category="multi-app"),

    # ── Ranger + Trino + auth (k8s) ───────────────────────────────────────
    _multi("ranger-trino-stack", [_k8s_model("access", {
        "ranger": _app("ranger-k8s"),
        "trino": _app("trino-k8s"),
        "pg": _app("postgresql-k8s", channel="14/stable"),
    }, integrations=[
        _integration("ranger", "db", "pg", "database"),
        _integration("trino", "ranger", "ranger", "trino"),
    ])], category="multi-app"),

    # ── MLflow + postgres + s3 (k8s) ──────────────────────────────────────
    _multi("mlflow-stack", [_k8s_model("ml", {
        "mlflow": _app("mlflow-server"),
        "pg": _app("postgresql-k8s", channel="14/stable"),
        "s3": _app("s3-integrator"),
    }, integrations=[
        _integration("mlflow", "relational-db", "pg", "database"),
        _integration("mlflow", "object-storage", "s3", "s3-credentials"),
    ])], category="multi-app"),

    # ── JIMM (Juju controller) + oauth + openfga ──────────────────────────
    _multi("jimm-stack", [_k8s_model("ctrl", {
        "jimm": _app("juju-jimm-k8s"),
        "openfga": _app("openfga-k8s"),
        "pg": _app("postgresql-k8s", channel="14/stable"),
    }, integrations=[
        _integration("jimm", "openfga", "openfga", "openfga"),
        _integration("jimm", "database", "pg", "database"),
        _integration("openfga", "database", "pg", "database"),
    ])], category="multi-app"),

    # ── Discourse + full CMS stack (k8s) ─────────────────────────────────
    _multi("discourse-full-stack", [_k8s_model("web", {
        "discourse": _app("discourse-k8s"),
        "pg": _app("postgresql-k8s", channel="14/stable"),
        "redis": _app("redis-k8s"),
        "traefik": _app("traefik-k8s"),
        "ssc": _app("self-signed-certificates"),
    }, integrations=[
        _integration("discourse", "database", "pg", "database"),
        _integration("discourse", "redis", "redis", "redis"),
        _integration("discourse", "ingress", "traefik", "ingress"),
        _integration("traefik", "certificates", "ssc", "certificates"),
    ])], category="multi-app"),

    # ── OTC + loki + grafana agent (full send-side) ───────────────────────
    _multi("otc-loki-agentstack", [_k8s_model("target", {
        "otelcol": _app("opentelemetry-collector-k8s"),
        "loki": _app("loki-k8s"),
        "ga": _app("grafana-agent-k8s"),
    }, integrations=[
        _integration("ga", "send-remote-write", "otelcol", "receive-remote-write"),
    ])], category="multi-app"),

    # ── Netbox (machine) ──────────────────────────────────────────────────
    _multi("netbox-stack", [_machine_model("netbox", {
        "netbox": _app("netbox"),
        "pg": _app("postgresql", channel="14/stable"),
        "redis": _app("redis"),
    }, integrations=[
        _integration("netbox", "database", "pg", "database"),
        _integration("netbox", "redis", "redis", "redis"),
    ])], category="multi-app"),

    # ── Feast stack (k8s) ─────────────────────────────────────────────────
    _multi("feast-stack", [_k8s_model("ml", {
        "feast": _app("feast-integrator"),
        "feast-ui": _app("feast-ui"),
        "pg": _app("postgresql-k8s", channel="14/stable"),
        "s3": _app("s3-integrator"),
    }, integrations=[
        _integration("feast", "database", "pg", "database"),
        _integration("feast", "object-storage", "s3", "s3-credentials"),
    ])], category="multi-app"),

    # ── HA MySQL (machine) ────────────────────────────────────────────────
    _multi("mysql-ha-machine", [_machine_model("data", {
        "mysql": _app("mysql", channel="8.0/stable"),
        "hacluster": _app("hacluster"),
    }, integrations=[
        _integration("mysql", "ha", "hacluster", "ha"),
    ])], category="multi-app"),

    # ── Mattermost + DB + S3 (k8s) ───────────────────────────────────────
    _multi("mattermost-stack", [_k8s_model("chat", {
        "mm": _app("mattermost-k8s"),
        "pg": _app("postgresql-k8s", channel="14/stable"),
        "s3": _app("s3-integrator"),
    }, integrations=[
        _integration("mm", "database", "pg", "database"),
        _integration("mm", "object-storage", "s3", "s3-credentials"),
    ])], category="multi-app"),

    # ── Jenkins + agents (k8s) ────────────────────────────────────────────
    _multi("jenkins-k8s-stack", [_k8s_model("ci", {
        "jenkins": _app("jenkins-k8s"),
        "pg": _app("postgresql-k8s", channel="14/stable"),
    }, integrations=[
        _integration("jenkins", "database", "pg", "database"),
    ])], category="multi-app"),

    # ── WordPress + MySQL + Traefik (k8s) ────────────────────────────────
    _multi("wordpress-stack-k8s", [_k8s_model("web", {
        "wp": _app("wordpress-k8s"),
        "mysql": _app("mysql-k8s", channel="8.0/stable"),
        "traefik": _app("traefik-k8s"),
        "ssc": _app("self-signed-certificates"),
    }, integrations=[
        _integration("wp", "database", "mysql", "database"),
        _integration("wp", "ingress", "traefik", "ingress"),
        _integration("traefik", "certificates", "ssc", "certificates"),
    ])], category="multi-app"),

    # ── OTC + pgbouncer (deep chain test) ────────────────────────────────
    _multi("otc-pgbouncer-chain", [_k8s_model("target", {
        "otelcol": _app("opentelemetry-collector-k8s"),
        "pgb": _app("pgbouncer-k8s"),
        "pg": _app("postgresql-k8s", channel="14/stable"),
    }, integrations=[
        _integration("pgb", "backend-database", "pg", "database"),
        _integration("pgb", "tracing", "otelcol", "receive-traces"),
        _integration("pg", "tracing", "otelcol", "receive-traces"),
    ])], category="multi-app"),
]

# Trim to exactly 50
COMPLEX_SCENARIOS = COMPLEX_SCENARIOS[:50]
assert len(COMPLEX_SCENARIOS) == 50, f"Expected 50 scenarios, got {len(COMPLEX_SCENARIOS)}"

ALL_SPECS = SINGLE_CHARM_SPECS + COMPLEX_SCENARIOS


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

WORKER_SCRIPT = Path(__file__).parent / "_bb_worker.py"


def run_one_spec(
    spec_entry: dict[str, Any],
    *,
    overrides_dir: str | None,
    charmhub_url: str | None,
    python: str,
    hard_timeout: int,
    verbose: bool = False,
) -> dict[str, Any]:
    """Run a single spec in a subprocess with a hard wall-clock timeout."""
    import yaml

    payload = json.dumps({
        "spec_yaml": yaml.dump(spec_entry["spec"]),
        "overrides_dir": overrides_dir,
        "charmhub_url": charmhub_url,
    })

    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            [python, str(WORKER_SCRIPT)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=hard_timeout,
        )
        elapsed = time.perf_counter() - t0
        if proc.returncode != 0:
            return {
                **spec_entry,
                "status": "ERROR",
                "elapsed_s": round(elapsed, 3),
                "n_apps": 0,
                "n_integrations": 0,
                "error": (proc.stderr or proc.stdout or "non-zero exit")[:500],
            }
        result = json.loads(proc.stdout.strip())
        return {**spec_entry, **result, "elapsed_s": round(elapsed, 3)}
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - t0
        return {
            **spec_entry,
            "status": "TIMEOUT",
            "elapsed_s": round(elapsed, 3),
            "n_apps": 0,
            "n_integrations": 0,
            "error": f"Hard timeout after {hard_timeout}s",
        }
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        return {
            **spec_entry,
            "status": "ERROR",
            "elapsed_s": round(elapsed, 3),
            "n_apps": 0,
            "n_integrations": 0,
            "error": repr(exc)[:300],
        }


def run_all(
    specs: list[dict[str, Any]],
    *,
    overrides_dir: str | None,
    charmhub_url: str | None,
    python: str,
    hard_timeout: int,
    workers: int,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = [{}] * len(specs)
    total = len(specs)
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_idx = {
            pool.submit(
                run_one_spec,
                spec,
                overrides_dir=overrides_dir,
                charmhub_url=charmhub_url,
                python=python,
                hard_timeout=hard_timeout,
                verbose=verbose,
            ): i
            for i, spec in enumerate(specs)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            result = future.result()
            results[idx] = result
            done += 1
            status = result.get("status", "?")
            elapsed = result.get("elapsed_s", 0)
            icon = "✓" if status == "SAT" else ("✗" if status == "TIMEOUT" else "~")
            print(
                f"  {icon} [{done:>3}/{total}] {result['id']:<55} {status:<16} {elapsed:>6.1f}s",
                flush=True,
            )
    return results


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

_STATUS_RANK = {"SAT": 0, "UNSAT": 1, "SOLVER_TIMEOUT": 2, "ERROR": 3, "TIMEOUT": 4, "": 5}


def _rank(s: str) -> int:
    return _STATUS_RANK.get(s, 5)


def compare(
    results_a: list[dict[str, Any]],
    results_b: list[dict[str, Any]],
    label_a: str = "A",
    label_b: str = "B",
) -> None:
    by_id_a = {r["id"]: r for r in results_a}
    by_id_b = {r["id"]: r for r in results_b}
    all_ids = sorted(set(by_id_a) | set(by_id_b))

    regressions: list[str] = []
    improvements: list[str] = []
    slowdowns: list[str] = []
    speedups: list[str] = []
    only_in_a: list[str] = []
    only_in_b: list[str] = []
    same: int = 0

    rows: list[tuple[str, str, str, str, str, str]] = []  # id, status_a, status_b, t_a, t_b, verdict

    for spec_id in all_ids:
        ra = by_id_a.get(spec_id)
        rb = by_id_b.get(spec_id)

        if ra is None:
            only_in_b.append(spec_id)
            rows.append((spec_id, "—", rb["status"], "—", f"{rb['elapsed_s']:.1f}s", "ONLY_IN_B"))
            continue
        if rb is None:
            only_in_a.append(spec_id)
            rows.append((spec_id, ra["status"], "—", f"{ra['elapsed_s']:.1f}s", "—", "ONLY_IN_A"))
            continue

        sa, sb = ra["status"], rb["status"]
        ta, tb = ra["elapsed_s"], rb["elapsed_s"]

        rank_a, rank_b = _rank(sa), _rank(sb)

        if rank_b > rank_a:
            verdict = "REGRESSION ⛔"
            regressions.append(spec_id)
        elif rank_b < rank_a:
            verdict = "IMPROVEMENT ✅"
            improvements.append(spec_id)
        elif sa == "SAT" and sb == "SAT":
            speedup = ta / tb if tb > 0 else float("inf")
            if speedup >= 2.0:
                verdict = f"FASTER {speedup:.1f}× ⚡"
                speedups.append(spec_id)
            elif speedup <= 0.5:
                verdict = f"SLOWER {1/speedup:.1f}× 🐢"
                slowdowns.append(spec_id)
            else:
                verdict = "same"
                same += 1
        else:
            verdict = "same"
            same += 1

        rows.append((spec_id, sa, sb, f"{ta:.1f}s", f"{tb:.1f}s", verdict))

    # ── Print table ──────────────────────────────────────────────────────
    col_w = [55, 16, 16, 8, 8, 22]
    header = ("Spec", label_a, label_b, "Time A", "Time B", "Verdict")
    sep = "  ".join("-" * w for w in col_w)
    fmt = "  ".join(f"{{:<{w}}}" for w in col_w)

    print()
    print("=" * 130)
    print(f"  Bundle-builder-x benchmark comparison: {label_a}  vs  {label_b}")
    print("=" * 130)
    print(fmt.format(*header))
    print(sep)

    for row in rows:
        is_notable = row[5] not in ("same",) and "ONLY" not in row[5]
        line = fmt.format(*row)
        if is_notable:
            print(f"  {line}")
        else:
            if "--verbose" in sys.argv:
                print(f"  {line}")

    print(sep)
    print()

    # ── Summary ──────────────────────────────────────────────────────────
    total = len(all_ids)
    sat_a = sum(1 for r in results_a if r.get("status") == "SAT")
    sat_b = sum(1 for r in results_b if r.get("status") == "SAT")
    timeout_a = sum(1 for r in results_a if r.get("status") in ("TIMEOUT", "SOLVER_TIMEOUT"))
    timeout_b = sum(1 for r in results_b if r.get("status") in ("TIMEOUT", "SOLVER_TIMEOUT"))

    def med_time(results: list[dict[str, Any]], status: str = "SAT") -> float:
        times = sorted(r["elapsed_s"] for r in results if r.get("status") == status)
        if not times:
            return 0.0
        mid = len(times) // 2
        return times[mid]

    def p95_time(results: list[dict[str, Any]], status: str = "SAT") -> float:
        times = sorted(r["elapsed_s"] for r in results if r.get("status") == status)
        if not times:
            return 0.0
        idx = int(len(times) * 0.95)
        return times[min(idx, len(times) - 1)]

    print(f"  Total specs:        {total}")
    print(f"  SAT:                {label_a}={sat_a}  {label_b}={sat_b}")
    print(f"  Timeouts:           {label_a}={timeout_a}  {label_b}={timeout_b}")
    print(f"  Median SAT time:    {label_a}={med_time(results_a):.2f}s  {label_b}={med_time(results_b):.2f}s")
    print(f"  p95 SAT time:       {label_a}={p95_time(results_a):.2f}s  {label_b}={p95_time(results_b):.2f}s")
    print()
    print(f"  Regressions  ({len(regressions)}): {regressions or 'none'}")
    print(f"  Improvements ({len(improvements)}): {improvements or 'none'}")
    print(f"  Speedups ≥2× ({len(speedups)}): {speedups or 'none'}")
    print(f"  Slowdowns    ({len(slowdowns)}): {slowdowns or 'none'}")
    print(f"  Same:               {same}")

    if regressions:
        print()
        print("  ⛔  REGRESSIONS DETECTED — branch has worse results than baseline.")
        sys.exit(1)
    else:
        print()
        print("  ✅  No regressions detected.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> None:
    import subprocess as sp
    git_branch = sp.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, cwd=Path(__file__).parent.parent,
    ).stdout.strip()
    git_commit = sp.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, cwd=Path(__file__).parent.parent,
    ).stdout.strip()

    specs = ALL_SPECS
    if args.category:
        specs = [s for s in specs if s.get("category") == args.category]
    if args.limit:
        specs = specs[: args.limit]

    print(f"Running {len(specs)} specs on branch '{git_branch}' ({git_commit})")
    print(f"  hard_timeout={args.hard_timeout}s  workers={args.workers}")
    print(f"  overrides={args.overrides}  charmhub_url={args.charmhub_url or '(env/default)'}")
    print()

    results = run_all(
        specs,
        overrides_dir=str(args.overrides) if args.overrides else None,
        charmhub_url=args.charmhub_url,
        python=sys.executable,
        hard_timeout=args.hard_timeout,
        workers=args.workers,
        verbose=args.verbose,
    )

    output = {
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "git_branch": git_branch,
            "git_commit": git_commit,
            "hard_timeout": args.hard_timeout,
            "n_specs": len(results),
        },
        "results": results,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2))
    print(f"\nResults written to {out_path}")

    sat = sum(1 for r in results if r.get("status") == "SAT")
    unsat = sum(1 for r in results if r.get("status") == "UNSAT")
    timeout = sum(1 for r in results if r.get("status") in ("TIMEOUT", "SOLVER_TIMEOUT"))
    error = sum(1 for r in results if r.get("status") == "ERROR")
    print(f"  SAT={sat}  UNSAT={unsat}  TIMEOUT={timeout}  ERROR={error}")


def cmd_compare(args: argparse.Namespace) -> None:
    path_a, path_b = Path(args.file_a), Path(args.file_b)
    data_a = json.loads(path_a.read_text())
    data_b = json.loads(path_b.read_text())

    label_a = data_a["metadata"].get("git_branch", path_a.stem)
    label_b = data_b["metadata"].get("git_branch", path_b.stem)

    compare(data_a["results"], data_b["results"], label_a=label_a, label_b=label_b)


def cmd_list(args: argparse.Namespace) -> None:
    for s in ALL_SPECS:
        print(f"{s['id']:<60} {s['category']:<18} {s['platform']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=textwrap.dedent(__doc__ or ""),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # run
    run_p = sub.add_parser("run", help="Run the benchmark suite and save results to JSON.")
    run_p.add_argument("--output", required=True, help="Output JSON path.")
    run_p.add_argument("--overrides", type=Path, default=None,
                       help="Path to charm-overrides directory.")
    run_p.add_argument("--charmhub-url", dest="charmhub_url", default=None,
                       help="Override Charmhub API URL (for nginx proxy cache).")
    run_p.add_argument("--workers", type=int, default=4,
                       help="Parallel worker count (default: 4).")
    run_p.add_argument("--hard-timeout", type=int, default=120, dest="hard_timeout",
                       help="Per-spec wall-clock timeout in seconds (default: 120). "
                            "Use 60-90 for main branch to avoid hour-long hangs.")
    run_p.add_argument("--category", default=None,
                       help="Only run specs in this category.")
    run_p.add_argument("--limit", type=int, default=None,
                       help="Only run the first N specs.")
    run_p.add_argument("--verbose", action="store_true")

    # compare
    cmp_p = sub.add_parser("compare", help="Compare two result JSON files.")
    cmp_p.add_argument("file_a", help="Baseline results (e.g. main.json).")
    cmp_p.add_argument("file_b", help="Branch results.")
    cmp_p.add_argument("--verbose", action="store_true",
                       help="Show all rows, not just notable ones.")

    # list
    sub.add_parser("list", help="List all spec IDs.")

    args = parser.parse_args()
    if args.command == "run":
        cmd_run(args)
    elif args.command == "compare":
        cmd_compare(args)
    elif args.command == "list":
        cmd_list(args)


if __name__ == "__main__":
    main()
