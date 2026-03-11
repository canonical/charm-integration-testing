"""Assert that if one endpoint is connected, another endpoint must also be connected.

Raises MissingRelation on ``then_endpoint`` when ``if_endpoint`` is connected
but ``then_endpoint`` is not, signalling the bundle builder to find a provider.

``MissingRelation`` is defined inline so this probe has no external dependencies
and can be exec'd by juju-doctor in any Python environment.  Bundle builder
identifies it by duck-typing (``endpoint`` + ``app`` attributes).

Expected ``with:`` shape:

    with:
      charm: vault-k8s
      if_endpoint: vault-pki
      then_endpoint: tls-certificates-pki
"""

from typing import Dict


class MissingRelation(Exception):
    """A required endpoint has no relation wired."""

    def __init__(self, app: str, endpoint: str):
        self.app = app
        self.endpoint = endpoint
        super().__init__(f"{app}:{endpoint} has no relation")


def bundle(juju_bundles: Dict[str, Dict], charm: str, if_endpoint: str, then_endpoint: str, **kwargs):
    for model_bundle in juju_bundles.values():
        relations = model_bundle.get("relations", [])
        applications = model_bundle.get("applications", {})

        endpoint_counts: Dict[str, Dict[str, int]] = {}
        for rel in relations:
            for side in rel:
                app, _, ep = side.partition(":")
                endpoint_counts.setdefault(app, {})
                endpoint_counts[app][ep] = endpoint_counts[app].get(ep, 0) + 1

        for app_name, app_info in applications.items():
            if app_info.get("charm") != charm:
                continue
            counts = endpoint_counts.get(app_name, {})
            if counts.get(if_endpoint, 0) > 0 and counts.get(then_endpoint, 0) == 0:
                raise MissingRelation(app=app_name, endpoint=then_endpoint)
