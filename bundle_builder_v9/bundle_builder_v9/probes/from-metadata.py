"""Assert that required and provided endpoints are connected and limits are respected.

Checks each REQUIRES and PROVIDES endpoint declared in the charm's metadata:
- If ``optional`` is False, the endpoint must have at least one relation.
  Raises ``MissingRelation`` to signal the bundle builder to find a provider.
- If ``limit`` is set to a non-None value, the endpoint must not exceed that
  number of relations. A limit of 0 means the endpoint must not be connected.

``MissingRelation`` is defined inline so this probe has no external dependencies
and can be exec'd by juju-doctor in any Python environment.  Bundle builder
identifies it by duck-typing (``endpoint`` + ``app`` attributes).

Expected ``with:`` shape:

    with:
      charm: some-charm-k8s
      requires:
        database:
          optional: false
          limit: 1
        logging:
          optional: true
          limit: null
      provides:
        metrics-endpoint:
          optional: true
          limit: null
"""

from typing import Dict, Optional


class MissingRelation(Exception):
    """A required endpoint has no relation wired."""

    def __init__(self, app: str, endpoint: str):
        self.app = app
        self.endpoint = endpoint
        super().__init__(f"{app}:{endpoint} has no relation")


def bundle(juju_bundles: Dict[str, Dict], charm: str, requires: Dict[str, Dict], provides: Dict[str, Dict], **kwargs):
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

            for ep_name, ep_meta in {**requires, **provides}.items():
                count = counts.get(ep_name, 0)
                optional = ep_meta.get("optional", False)
                limit: Optional[int] = ep_meta.get("limit")

                if not optional and count == 0:
                    raise MissingRelation(app=app_name, endpoint=ep_name)

                if limit is not None and count > limit:
                    raise Exception(
                        f"Application '{app_name}' (charm '{charm}') endpoint "
                        f"'{ep_name}' has {count} relation(s) but limit is {limit}."
                    )
