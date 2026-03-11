"""Verify a single endpoint's connection and limit constraints.

with:
  charm: some-charm-k8s
  endpoint: database
  optional: false   # default: true
  limit: null       # default: null (no limit enforced)
"""
from typing import Dict, Optional


class MissingRelation(Exception):
    def __init__(self, app: str, endpoint: str):
        self.app = app
        self.endpoint = endpoint
        super().__init__(f"{app}:{endpoint} is not related to anything")


def bundle(
    juju_bundles: Dict[str, Dict],
    charm: str,
    endpoint: str,
    optional: bool = True,
    limit: Optional[int] = None,
    **kwargs,
) -> None:
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
            count = endpoint_counts.get(app_name, {}).get(endpoint, 0)
            if not optional and count == 0:
                raise MissingRelation(app=app_name, endpoint=endpoint)
            if limit is not None and count > limit:
                raise Exception(
                    f"Application '{app_name}' endpoint '{endpoint}' "
                    f"has {count} relation(s) but limit is {limit}."
                )
