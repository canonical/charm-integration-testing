"""
Example charm metadata structure.

This models the key information we need from charmcraft.yaml or metadata.yaml
to build bundles with Z3.
"""

from dataclasses import dataclass
from typing import Literal


@dataclass
class Endpoint:
    """An integration endpoint (provides or requires)."""

    name: str
    interface: str
    role: Literal["provides", "requires"]
    limit: int | None = None  # max number of connections (None = unlimited)
    optional: bool = True  # whether this endpoint must be connected


@dataclass
class Charm:
    """Metadata for a single charm."""

    name: str
    channel: str = "stable"
    endpoints: list[Endpoint] = None

    def __post_init__(self):
        if self.endpoints is None:
            self.endpoints = []


# Example charm definitions
# These represent actual Juju charms with their integration endpoints

postgresql = Charm(
    name="postgresql-k8s",
    channel="14/stable",
    endpoints=[
        Endpoint(name="database", interface="postgresql_client", role="provides"),
        Endpoint(name="certificates", interface="tls-certificates", role="requires", optional=True),
    ],
)

vault = Charm(
    name="vault-k8s",
    channel="stable",
    endpoints=[
        Endpoint(name="vault-kv", interface="vault-kv", role="provides"),
        Endpoint(name="certificates", interface="tls-certificates", role="provides"),
        Endpoint(name="tls-certificates-pki", interface="tls-certificates", role="requires", optional=True),
    ],
)

tls_operator = Charm(
    name="self-signed-certificates",
    channel="stable",
    endpoints=[
        Endpoint(name="certificates", interface="tls-certificates", role="provides"),
    ],
)

jimm = Charm(
    name="juju-jimm-k8s",
    channel="stable",
    endpoints=[
        Endpoint(name="database", interface="postgresql_client", role="requires"),
        Endpoint(name="oauth", interface="oauth", role="requires"),
        Endpoint(name="oauth-tls", interface="oauth", role="requires", optional=True),
        Endpoint(name="vault", interface="vault-kv", role="requires", optional=True),
    ],
)

hydra = Charm(
    name="hydra",
    channel="stable",
    endpoints=[
        Endpoint(name="oauth", interface="oauth", role="provides"),
        Endpoint(name="pg-database", interface="postgresql_client", role="requires"),
    ],
)


# Custom rules (beyond what's in metadata)
# These represent the additional constraints from your rulesets.yaml files


class IntegrationRule:
    """Base class for integration rules."""

    pass


@dataclass
class MutuallyExclusiveRule(IntegrationRule):
    """Only one of these endpoints can be integrated."""

    charm: str
    endpoints: list[str]
    at_least_one: bool = True  # if True, exactly one; if False, at most one


@dataclass
class ConditionalRequirementRule(IntegrationRule):
    """If endpoint A is integrated, endpoint B must/must not be integrated."""

    charm: str
    if_endpoint: str  # trigger endpoint
    then_required: list[str] = None  # must be integrated if trigger is
    then_forbidden: list[str] = None  # must NOT be integrated if trigger is

    def __post_init__(self):
        if self.then_required is None:
            self.then_required = []
        if self.then_forbidden is None:
            self.then_forbidden = []


@dataclass
class SameApplicationRule(IntegrationRule):
    """Two endpoints must integrate with the same application."""

    charm: str
    endpoint1: str
    endpoint2: str


@dataclass
class BridgeRule(IntegrationRule):
    """Resource bridging: if charm A needs X, and has endpoint E integrated to charm B,
    then A gets X from wherever B gets it."""

    charm: str
    resource: str  # e.g., "certificates"
    via_endpoint: str  # the endpoint that provides the bridge


# Example rules for jimm
jimm_rules = [
    # oauth and oauth-tls are mutually exclusive, but one is required
    MutuallyExclusiveRule(charm="juju-jimm-k8s", endpoints=["oauth", "oauth-tls"], at_least_one=True),
]

vault_rules = [
    # If vault provides certificates, it can get them from tls-certificates-pki
    # This is a bridge: apps that get certs from vault can transitively get them from
    # whoever vault is connected to on tls-certificates-pki
    BridgeRule(charm="vault-k8s", resource="certificates", via_endpoint="tls-certificates-pki"),
]
