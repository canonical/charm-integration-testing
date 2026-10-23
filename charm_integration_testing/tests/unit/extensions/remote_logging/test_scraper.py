# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import re
from dataclasses import dataclass

import pytest
import yaml
from extensions.remote_logging.scraper import (
    COLLECTOR_IMAGE,
    COLLECTOR_SHA256_BY_ARCH,
    COLLECTOR_STATE_DIRECTORY,
    COLLECTOR_STORAGE_PATH,
    KUBERNETES_POD_REGEX,
    MACHINE_MACHINE_FILE,
    MACHINE_UNIT_FILE,
    kubernetes_daemon_set,
    kubernetes_scraper_config,
    machine_userdata,
    merge_userdata,
    safe_controller_name,
)


def test_merge_userdata_without_existing_value_returns_addition() -> None:
    assert yaml.safe_load(merge_userdata(None, {"postruncmd": ["a"]})) == {"postruncmd": ["a"]}


def test_merge_userdata_appends_to_lists_and_keeps_other_keys() -> None:
    existing = yaml.safe_dump({"postruncmd": ["x"], "packages": ["jq"]})

    merged = yaml.safe_load(merge_userdata(existing, {"postruncmd": ["a"], "write_files": [{"path": "/f"}]}))

    assert merged == {"postruncmd": ["x", "a"], "packages": ["jq"], "write_files": [{"path": "/f"}]}


@pytest.mark.parametrize("existing", ["- item", "plain text", "null"])
def test_merge_userdata_rejects_non_mapping_yaml(existing: str) -> None:
    with pytest.raises(ValueError, match="YAML mapping"):
        merge_userdata(existing, {"postruncmd": ["a"]})


def test_merge_userdata_rejects_list_scalar_conflicts() -> None:
    with pytest.raises(ValueError, match="cannot merge list and non-list"):
        merge_userdata(yaml.safe_dump({"postruncmd": "echo existing"}), {"postruncmd": ["echo new"]})


def test_machine_userdata_uses_only_allowed_cloud_init_keys() -> None:
    # Juju rejects `runcmd`; it requires `preruncmd`/`postruncmd`.
    assert set(machine_userdata("http://host", "ctrl", "model")) == {"write_files", "postruncmd"}


def test_daemon_set_persists_offsets_per_controller() -> None:
    body = kubernetes_daemon_set("ns", "ctrl")

    container = body["spec"]["template"]["spec"]["containers"][0]
    volumes = {volume["name"]: volume for volume in body["spec"]["template"]["spec"]["volumes"]}
    assert volumes["storage"]["hostPath"]["path"] == f"{COLLECTOR_STORAGE_PATH}/ctrl"
    assert volumes["pods"]["hostPath"]["path"] == "/var/log/pods"
    assert {mount["name"]: mount["mountPath"] for mount in container["volumeMounts"]}["storage"] == (
        COLLECTOR_STORAGE_PATH
    )


def test_daemon_set_pins_collector_image_by_digest() -> None:
    body = kubernetes_daemon_set("ns", "ctrl")

    image = body["spec"]["template"]["spec"]["containers"][0]["image"]

    assert image == COLLECTOR_IMAGE
    assert re.fullmatch(r"otel/opentelemetry-collector-contrib@sha256:[0-9a-f]{64}", image)


@pytest.mark.parametrize(
    ("controller", "expected"),
    [
        ("ctrl-1", "ctrl-1"),
        ("CTRL_1", "ctrl-1-"),
        ("a" * 100, "a" * 38 + "-"),
    ],
)
def test_safe_controller_name_is_bounded_and_dns_compatible(controller: str, expected: str) -> None:
    safe = safe_controller_name(controller)

    assert safe.startswith(expected)
    assert len(safe) <= 51
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", safe)


def test_daemon_set_sanitizes_controller_for_host_path() -> None:
    body = kubernetes_daemon_set("ns", "../Ctrl_1")
    volumes = {volume["name"]: volume for volume in body["spec"]["template"]["spec"]["volumes"]}

    assert volumes["storage"]["hostPath"]["path"] == (f"{COLLECTOR_STORAGE_PATH}/{safe_controller_name('../Ctrl_1')}")


def test_daemon_set_hardens_the_root_collector() -> None:
    body = kubernetes_daemon_set("ns", "ctrl")

    security_context = body["spec"]["template"]["spec"]["containers"][0]["securityContext"]

    assert security_context == {
        "runAsUser": 0,
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }


@dataclass(frozen=True)
class LabelParams:
    regex: str
    value: str
    labels: dict[str, str] | None


@pytest.mark.parametrize(
    "params",
    [
        LabelParams(MACHINE_UNIT_FILE, "unit-my-app-3.log", {"juju_application": "my-app", "juju_unit_number": "3"}),
        LabelParams(MACHINE_UNIT_FILE, "machine-3.log", None),
        LabelParams(MACHINE_MACHINE_FILE, "machine-12.log", {"juju_machine": "12"}),
        LabelParams(MACHINE_MACHINE_FILE, "machine-lock.log", None),
        LabelParams(KUBERNETES_POD_REGEX, "target-0", {"juju_application": "target", "juju_unit_number": "0"}),
        LabelParams(KUBERNETES_POD_REGEX, "modeloperator-7c9d5b6f4-x2x9z", None),
    ],
)
def test_label_regexes_extract_application_unit_and_machine(params: LabelParams) -> None:
    match = re.match(params.regex, params.value)

    assert (match.groupdict() if match else None) == params.labels


def test_machine_collector_groups_logs_by_labels_from_the_file_name() -> None:
    userdata = machine_userdata("http://host", "ctrl", "model")

    config = yaml.safe_load(userdata["write_files"][0]["content"])

    assert config["service"]["pipelines"]["logs"]["processors"] == ["groupbyattrs", "resource", "attributes"]
    receiver = config["receivers"]["filelog"]
    assert receiver["resource"] == {"juju_controller": "ctrl", "juju_model": "model"}
    assert receiver["include_file_name"] is True
    assert receiver["include_file_path"] is True
    assert [operator["type"] for operator in receiver["operators"]] == ["regex_parser"] * 2
    assert receiver["operators"][0]["parse_from"] == 'attributes["log.file.name"]'
    assert receiver["storage"] == "file_storage"
    assert config["extensions"] == {"file_storage": {"directory": COLLECTOR_STORAGE_PATH}}
    assert config["service"]["extensions"] == ["file_storage"]


def test_machine_collector_persists_state_and_verifies_the_archive() -> None:
    userdata = machine_userdata("http://host", "ctrl", "model")

    unit = userdata["write_files"][1]["content"]
    install_command = userdata["postruncmd"][0]

    assert f"StateDirectory={COLLECTOR_STATE_DIRECTORY}" in unit
    assert "Unsupported collector architecture: ${ARCH}" in install_command
    assert "exit 1" in install_command
    for architecture, checksum in COLLECTOR_SHA256_BY_ARCH.items():
        assert f"{architecture}) SHA256='{checksum}' ;;" in install_command
    assert install_command.index("curl ") < install_command.index("sha256sum ") < install_command.index("tar ")


def test_kubernetes_collector_derives_the_model_from_the_namespace() -> None:
    config = yaml.safe_load(kubernetes_scraper_config("http://host", "ctrl", ["ctrl-ns"]))

    receiver = config["receivers"]["filelog"]
    assert receiver["resource"] == {"juju_controller": "ctrl"}
    assert [operator["type"] for operator in receiver["operators"]] == ["container", "add", "regex_parser"]
    assert receiver["operators"][1]["value"] == (
        'EXPR(resource["k8s.namespace.name"] == "controller-ctrl" ? "controller" : resource["k8s.namespace.name"])'
    )
    assert receiver["storage"] == "file_storage"
    assert config["extensions"] == {"file_storage": {"directory": COLLECTOR_STORAGE_PATH}}
    assert config["service"]["extensions"] == ["file_storage"]
    assert config["processors"]["groupbyattrs"]["keys"] == [
        "juju_model",
        "juju_application",
        "juju_unit_number",
        "juju_machine",
    ]


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://host", "http://host/loki/api/v1/push"),
        ("http://host/", "http://host/loki/api/v1/push"),
        ("https://host", "https://host/loki/api/v1/push"),
        ("https://host/", "https://host/loki/api/v1/push"),
        ("http://host/loki", "http://host/loki/api/v1/push"),
        ("http://host/loki/", "http://host/loki/api/v1/push"),
        ("https://host/loki", "https://host/loki/api/v1/push"),
        ("https://host/loki/", "https://host/loki/api/v1/push"),
        ("http://host/gateway-loki", "http://host/gateway-loki/loki/api/v1/push"),
        ("https://host/loki/api/v1/push/", "https://host/loki/api/v1/push"),
    ],
)
def test_collector_exports_to_the_loki_push_api_with_juju_labels(endpoint: str, expected: str) -> None:
    config = yaml.safe_load(kubernetes_scraper_config(endpoint, "ctrl", ["ctrl-ns"]))

    assert config["exporters"] == {"loki": {"endpoint": expected}}
    assert config["service"]["pipelines"]["logs"]["exporters"] == ["loki"]
    labels = config["processors"]["resource"]["attributes"][0]
    assert labels["key"] == "loki.resource.labels"
    assert labels["value"].split(",") == [
        "juju_controller",
        "juju_model",
        "juju_application",
        "juju_unit_number",
        "juju_machine",
        "k8s.container.name",
        "k8s.pod.name",
    ]
    assert config["processors"]["resource"]["attributes"][1] == {
        "key": "loki.format",
        "value": "raw",
        "action": "insert",
    }
    assert config["processors"]["attributes"]["actions"] == [
        {"key": "loki.attribute.labels", "value": "log.file.name", "action": "insert"}
    ]
