from pathlib import Path

import yaml


def test_juju_no_proxy_bypasses_kubernetes_service_domains() -> None:
    environments_path = Path(__file__).resolve().parents[3] / "static" / "environments.yaml"
    environments = yaml.safe_load(environments_path.read_text())["environments"]

    for name, environment in environments.items():
        no_proxy = environment.get("model-config", {}).get("juju-no-proxy")
        if no_proxy is None:
            continue

        no_proxy_domains = no_proxy.split(",")
        assert ".svc" in no_proxy_domains, f"{name} does not bypass .svc domains"
        assert ".svc.cluster.local" in no_proxy_domains, f"{name} does not bypass cluster-local domains"
