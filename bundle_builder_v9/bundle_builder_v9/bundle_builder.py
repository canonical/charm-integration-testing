# Copyright (C) 2026 Canonical Ltd

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.


import importlib.util
import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict
import yaml
from hypothesis import find
from hypothesis import strategies as st
from hypothesis.errors import NoSuchExample

from .bundle import Application, ApplicationEndpoint, Bundle, Integration
from .charm import Charm, CharmChannel, EndpointType
from .charmhub import CharmhubClient
from .exceptions import is_channel_mismatch, is_missing_relation


def _url_to_path(url: str) -> Path:
    """Convert a file:// URL or plain path string to a Path."""
    if url.startswith("file://"):
        return Path(url[7:])
    return Path(url)


def _load_scriptlet(path: Path):
    """Dynamically load a Python scriptlet from disk and return its module."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parse_ruleset_path(path: Path) -> list[tuple]:
    """Parse a ruleset YAML and return (module, with_args) pairs for each probe."""
    data = yaml.safe_load(path.read_text())
    probes = []
    for entry in data.get("probes", []):
        url = entry.get("url")
        if not url:
            continue
        probes.append((_load_scriptlet(_url_to_path(url)), entry.get("with", {})))
    return probes


def _parse_probe_url(url: str) -> list[tuple]:
    """Load a probe URL: .py files are scriptlets, anything else is a ruleset YAML."""
    path = _url_to_path(url)
    if path.suffix.lower() == ".py":
        return [(_load_scriptlet(path), {})]
    return _parse_ruleset_path(path)


class UnresolvableBundleError(Exception):
    def __init__(self, message: str):
        super().__init__(message)


class ApplicationConstraint(BaseModel):
    charm: str
    channel: CharmChannel | None = None
    revision: int | None = None
    base: str | None = None

class IntegrationConstraint(BaseModel):
    model_config = ConfigDict(frozen=True)

    application_1: str
    endpoint_1: str
    application_2: str
    endpoint_2: str


class BundleBuilder:
    charmhub_client: CharmhubClient
    logger: logging.Logger

    def __init__(
        self,
        charmhub_client: CharmhubClient,
        logger: logging.Logger = logging.getLogger(__name__),
    ):
        self.charmhub_client = charmhub_client
        self.logger = logger

    def _initialize_domain(
        self,
        applications: dict[str, ApplicationConstraint],
        integrations: set[IntegrationConstraint],
        platform: str,
        arch: str,
    ) -> "st.SearchStrategy[Bundle]":
        # Get charm for each application
        application_charms = {
            app_name: self.charmhub_client.charm_from_store(
                charm_name=app_constraint.charm,
                ubuntu_arch=arch,
                charm_channel=app_constraint.channel,
                charm_revision=app_constraint.revision,
                ubuntu_version=app_constraint.base,
            )
            for app_name, app_constraint in applications.items()
        }

        # Build the fixed set of user-required integrations
        required_integrations: list[Integration] = []
        for ic in integrations:
            required_integrations.append(
                frozenset(
                    [
                        ApplicationEndpoint(application=ic.application_1, endpoint=ic.endpoint_1),
                        ApplicationEndpoint(application=ic.application_2, endpoint=ic.endpoint_2),
                    ]
                )
            )

        # Find all compatible optional integrations from charm endpoint metadata.
        # An integration is possible when two endpoints on different applications
        # share the same interface and are REQUIRES<->PROVIDES complementary.
        required_set: set[Integration] = set(required_integrations)
        optional_integrations: list[Integration] = []
        app_names = list(application_charms.keys())
        for i, app1 in enumerate(app_names):
            charm1 = application_charms[app1]
            for app2 in app_names[i + 1 :]:
                charm2 = application_charms[app2]
                for ep1_name, ep1 in charm1.endpoints.items():
                    for ep2_name, ep2 in charm2.endpoints.items():
                        if ep1.interface != ep2.interface:
                            continue
                        if (ep1.type == EndpointType.REQUIRES and ep2.type == EndpointType.PROVIDES) or (
                            ep1.type == EndpointType.PROVIDES and ep2.type == EndpointType.REQUIRES
                        ):
                            candidate: Integration = frozenset(
                                [
                                    ApplicationEndpoint(application=app1, endpoint=ep1_name),
                                    ApplicationEndpoint(application=app2, endpoint=ep2_name),
                                ]
                            )
                            if candidate not in required_set:
                                optional_integrations.append(candidate)

        # Strategy for each application: sample one of its available configs
        applications_strategy = st.fixed_dictionaries(
            {
                app_name: st.sampled_from(charm.configs) if charm.configs else st.just({})
                for app_name, charm in application_charms.items()
            }
        )

        # Strategy for relations: always include the user-required integrations,
        # then draw a unique subset of the metadata-compatible optional ones
        if optional_integrations:
            relations_strategy: st.SearchStrategy[list[Integration]] = st.lists(
                st.sampled_from(optional_integrations),
                unique=True,
            ).map(lambda optional: required_integrations + optional)
        else:
            relations_strategy = st.just(required_integrations)

        # Combine into a full bundle strategy
        return st.fixed_dictionaries(
            {
                "applications": applications_strategy,
                "relations": relations_strategy,
            }
        ).map(
            lambda d: Bundle(
                applications={
                    app_name: Application(charm=application_charms[app_name], config=config)
                    for app_name, config in d["applications"].items()
                },
                integrations=set(d["relations"]),
                platform=platform,
                arch=arch,
            )
        )

    def build(
        self,
        applications: dict[str, ApplicationConstraint],
        integrations: set[IntegrationConstraint],
        platform: str,
        arch: str,
        probe_urls: list[str] = [],
    ) -> Bundle:
        """Build a bundle satisfying all constraints and probes.

        Always runs metadata endpoint constraints derived from charm metadata.
        If probe_urls are provided, also runs those probes and expands the charm
        pool as needed to satisfy them.

        Args:
            applications: user-specified application constraints.
            integrations: user-specified integration constraints.
            platform: target platform (kubernetes or machine).
            arch: target architecture.
            probe_urls: optional probe URLs (file:// or github://) to also run.
        """
        # Probe signals captured by _satisfies_probes across runs
        expansion_signals: list = []
        channel_constraints: list = []

        # Ancestry tracking for cycle detection; persists across iterations
        added_by: dict[str, str] = {}

        current_applications = dict(applications)
        current_probes: list[tuple] = []

        def _load_probes() -> list[tuple]:
            """Load all (module, with_args) probe pairs for the current application set."""
            seen: set[str] = set()
            probes: list[tuple] = []
            for constraint in current_applications.values():
                charm_obj = self.charmhub_client.charm_from_store(
                    charm_name=constraint.charm,
                    ubuntu_arch=arch,
                    charm_channel=constraint.channel,
                    charm_revision=constraint.revision,
                    ubuntu_version=constraint.base,
                )
                if charm_obj.name not in seen:
                    seen.add(charm_obj.name)
                    probes.extend(self._metadata_probe_definitions(charm_obj))
                    if charm_obj.ruleset_url:
                        probes.extend(_parse_ruleset_path(_url_to_path(charm_obj.ruleset_url)))
            for url in probe_urls:
                probes.extend(_parse_probe_url(url))
            return probes

        def _satisfies_probes(bundle: Bundle) -> bool:
            expansion_signals.clear()
            channel_constraints.clear()
            bundle_dict = yaml.safe_load(bundle.export())
            juju_bundles = {"model": bundle_dict}
            passed = True
            for module, with_args in current_probes:
                bundle_fn = getattr(module, "bundle", None)
                if bundle_fn is None:
                    continue
                try:
                    bundle_fn(juju_bundles=juju_bundles, **with_args)
                except Exception as exc:
                    passed = False
                    if is_missing_relation(exc):
                        expansion_signals.append(exc)
                    elif is_channel_mismatch(exc):
                        channel_constraints.append(exc)
            return passed

        max_iterations = 10
        for iteration in range(max_iterations):
            self.logger.info(
                f"Fuzz iteration {iteration + 1}/{max_iterations} "
                f"with {len(current_applications)} application(s): {sorted(current_applications)}"
            )
            current_probes[:] = _load_probes()
            strategy = self._initialize_domain(current_applications, integrations, platform, arch)
            try:
                find(strategy, _satisfies_probes)
            except NoSuchExample:
                made_progress = False

                # Re-pin existing apps to satisfy channel constraints (no new charms added)
                for constraint in channel_constraints:
                    if constraint.app in current_applications:
                        old = current_applications[constraint.app]
                        current_applications[constraint.app] = ApplicationConstraint(
                            charm=old.charm,
                            channel=CharmChannel(track=constraint.track, risk="stable", branch=""),
                            revision=None,
                            base=old.base,
                        )
                        self.logger.info(f"Re-pinned '{constraint.app}' to track {constraint.track}/stable")
                        made_progress = True

                # Add new charms to satisfy missing-relation signals
                added = self._expand_applications(
                    current_applications,
                    platform,
                    arch,
                    expansion_signals=expansion_signals,
                    added_by=added_by,
                )
                if added:
                    current_applications.update(added)
                    made_progress = True

                if not made_progress:
                    self.logger.info("No progress made; giving up")
                    break
                continue

            # TODO: minimize the application set before returning
            # (greedily remove non-user-specified applications that aren't needed)
            return find(
                self._initialize_domain(current_applications, integrations, platform, arch),
                _satisfies_probes,
            )

        raise UnresolvableBundleError(
            "No bundle satisfying all probes could be found after expanding the charm pool"
        )

    def _expand_applications(
        self,
        current_applications: dict[str, ApplicationConstraint],
        platform: str,
        arch: str,
        expansion_signals: list | None = None,
        added_by: dict[str, str] | None = None,
    ) -> dict[str, ApplicationConstraint]:
        """Find new charms that can satisfy REQUIRES endpoints signalled by probes.

        When *expansion_signals* is provided (probe-driven mode), each signal
        carries ``app`` and ``endpoint``.  Bundle builder resolves the interface
        from the Charm object it already holds and searches charmhub for a
        provider.

        When *expansion_signals* is empty or None, falls back to static interface
        analysis: discovers REQUIRES endpoints with no provider in the current
        pool and searches charmhub for providers.
        """
        expansion_signals = expansion_signals or []
        if added_by is None:
            added_by = {}

        # Fetch charm metadata for all current applications
        current_charms = {
            app_name: self.charmhub_client.charm_from_store(
                charm_name=constraint.charm,
                ubuntu_arch=arch,
                charm_channel=constraint.channel,
                charm_revision=constraint.revision,
                ubuntu_version=constraint.base,
            )
            for app_name, constraint in current_applications.items()
        }

        current_charm_names = {c.charm for c in current_applications.values()}
        provided_interfaces = {
            endpoint.interface
            for charm in current_charms.values()
            for endpoint in charm.endpoints.values()
            if endpoint.type == EndpointType.PROVIDES
        }
        all_required_interfaces = {
            endpoint.interface
            for charm in current_charms.values()
            for endpoint in charm.endpoints.values()
            if endpoint.type == EndpointType.REQUIRES
        }

        new_applications: dict[str, ApplicationConstraint] = {}
        already_added_charm_names: set[str] = set()

        if expansion_signals:
            # Probe-driven expansion: use signal app+endpoint to derive interface
            for signal in expansion_signals:
                requesting_app = signal.app
                endpoint_name = signal.endpoint

                charm_obj = current_charms.get(requesting_app)
                if charm_obj is None:
                    continue
                ep = charm_obj.endpoints.get(endpoint_name)
                if ep is None:
                    self.logger.debug(f"Signal from '{requesting_app}': unknown endpoint '{endpoint_name}', skipping")
                    continue
                interface = ep.interface

                self.logger.info(f"Expanding for '{requesting_app}:{endpoint_name}' (interface '{interface}')")
                # If the missing endpoint is REQUIRES, find a charm that PROVIDES;
                # if it is PROVIDES, find a charm that REQUIRES.
                if ep.type == EndpointType.REQUIRES:
                    candidates = self.charmhub_client.find_charms(provides=interface, platform=platform)
                else:
                    candidates = self.charmhub_client.find_charms(requires=interface, platform=platform)

                for charm_name in self._scored_candidates(
                    candidates,
                    current_charm_names | already_added_charm_names,
                    all_required_interfaces,
                    provided_interfaces,
                    arch,
                ):
                    if charm_name in {c.charm for c in current_applications.values()}:
                        # Already present — Hypothesis will try wiring it
                        break
                    if self._would_create_cycle(charm_name, requesting_app, current_applications, added_by):
                        self.logger.debug(f"Skipping '{charm_name}': would create cycle for '{requesting_app}'")
                        continue
                    app_name = charm_name
                    suffix = 2
                    while app_name in current_applications or app_name in new_applications:
                        app_name = f"{charm_name}-{suffix}"
                        suffix += 1
                    self.logger.info(f"Adding '{charm_name}' for '{requesting_app}:{endpoint_name}' as '{app_name}'")
                    new_applications[app_name] = ApplicationConstraint(charm=charm_name)
                    added_by[app_name] = requesting_app
                    already_added_charm_names.add(charm_name)
                    break
        else:
            # Static fallback: find REQUIRES interfaces with no provider
            unsatisfied_interfaces = {
                endpoint.interface
                for charm in current_charms.values()
                for endpoint in charm.endpoints.values()
                if endpoint.type == EndpointType.REQUIRES and endpoint.interface not in provided_interfaces
            }

            if not unsatisfied_interfaces:
                return {}

            for interface in sorted(unsatisfied_interfaces):
                self.logger.info(f"Searching charmhub for charms providing interface '{interface}'")
                candidates = self.charmhub_client.find_charms(provides=interface, platform=platform)

                best_charm: str | None = None
                best_score: int = -(10**9)
                for charm_name in candidates:
                    if charm_name in current_charm_names or charm_name in already_added_charm_names:
                        continue
                    try:
                        candidate_charm = self.charmhub_client.charm_from_store(
                            charm_name=charm_name,
                            ubuntu_arch=arch,
                        )
                    except Exception:
                        continue
                    already_provided = sum(
                        1
                        for ep in candidate_charm.endpoints.values()
                        if ep.type == EndpointType.PROVIDES and ep.interface in all_required_interfaces
                    )
                    new_requires = sum(
                        1
                        for ep in candidate_charm.endpoints.values()
                        if ep.type == EndpointType.REQUIRES and ep.interface not in provided_interfaces
                    )
                    score = already_provided - new_requires
                    self.logger.debug(
                        f"  Candidate '{charm_name}': already_provided={already_provided}, "
                        f"new_requires={new_requires}, score={score}"
                    )
                    if score > best_score:
                        best_score = score
                        best_charm = charm_name

                if best_charm is None:
                    self.logger.warning(f"No suitable provider found for interface '{interface}'")
                    continue

                app_name = best_charm
                suffix = 2
                while app_name in current_applications or app_name in new_applications:
                    app_name = f"{best_charm}-{suffix}"
                    suffix += 1
                self.logger.info(
                    f"Adding best-fit provider '{best_charm}' (score={best_score}) "
                    f"for interface '{interface}' as application '{app_name}'"
                )
                new_applications[app_name] = ApplicationConstraint(charm=best_charm)
                already_added_charm_names.add(best_charm)

        return new_applications

    def _scored_candidates(
        self,
        candidates: set[str],
        exclude: set[str],
        all_required_interfaces: set[str],
        provided_interfaces: set[str],
        arch: str,
    ):
        """Yield candidate charm names sorted by fit score (best first)."""
        scored: list[tuple[int, str]] = []
        for charm_name in candidates:
            if charm_name in exclude:
                continue
            try:
                candidate_charm = self.charmhub_client.charm_from_store(
                    charm_name=charm_name,
                    ubuntu_arch=arch,
                )
            except Exception:
                continue
            already_provided = sum(
                1
                for ep in candidate_charm.endpoints.values()
                if ep.type == EndpointType.PROVIDES and ep.interface in all_required_interfaces
            )
            new_requires = sum(
                1
                for ep in candidate_charm.endpoints.values()
                if ep.type == EndpointType.REQUIRES and ep.interface not in provided_interfaces
            )
            scored.append((already_provided - new_requires, charm_name))
        scored.sort(key=lambda x: -x[0])
        for _, name in scored:
            yield name

    @staticmethod
    def _would_create_cycle(
        candidate_charm: str,
        requesting_app: str,
        current_applications: dict[str, ApplicationConstraint],
        added_by: dict[str, str],
    ) -> bool:
        """Return True if adding candidate_charm to satisfy requesting_app would create a cycle."""
        visited: set[str] = set()
        node: str | None = requesting_app
        while node is not None:
            if node in visited:
                break
            visited.add(node)
            if current_applications.get(node, ApplicationConstraint(charm="")).charm == candidate_charm:
                return True
            node = added_by.get(node)
        return False

    def _metadata_probe_definitions(self, charm: Charm) -> list[tuple]:
        """Return (module, with_args) probe pairs for each non-peer endpoint of this charm."""
        module = _load_scriptlet(Path(__file__).parent / "probes" / "from-metadata.py")
        return [
            (module, {"charm": charm.name, "endpoint": ep_name, "optional": ep.optional, "limit": ep.limit})
            for ep_name, ep in charm.endpoints.items()
            if ep.type != EndpointType.PEERS
        ]
