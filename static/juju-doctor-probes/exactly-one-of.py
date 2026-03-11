"""Assert that exactly one endpoint from a given set has a relation, per app instance.

Raises MissingRelation for each unconnected endpoint if none are integrated.
Raises a plain Exception if more than one are integrated simultaneously.

``MissingRelation`` is defined inline so this probe has no external dependencies
and can be exec'd by juju-doctor in any Python environment.  Bundle builder
identifies it by duck-typing (``endpoint`` + ``app`` attributes).

Expected `with:` shape:

    with:
      charm: canonical-livepatch-server-k8s
      endpoints: [database, database-legacy]
"""

from typing import Dict, List


class MissingRelation(Exception):
    """A required endpoint has no relation wired."""

    def __init__(self, app: str, endpoint: str):
        self.app = app
        self.endpoint = endpoint
        super().__init__(f"{app}:{endpoint} has no relation")


def bundle(juju_bundles: Dict[str, Dict], charm: str, endpoints: List[str], **kwargs):
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
            active = [ep for ep in endpoints if counts.get(ep, 0) > 0]
            if not active:
                # Signal once — bundle builder will expand for one endpoint;
                # Hypothesis then explores which one to wire.
                raise MissingRelation(app=app_name, endpoint=endpoints[0])
            if len(active) > 1:
                raise Exception(
                    f"Application '{app_name}' (charm '{charm}') must have exactly one of "
                    f"{endpoints} connected, but found: {active}."
                )
