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

"""Probe scriptlet loading, probe-list construction, and bundle checking."""

import importlib.util
import types
from pathlib import Path

import yaml

from .bundle import Bundle
from .charm import Charm, EndpointType

# Probe = (module, with_args): the module exposes a bundle() function;
# with_args are keyword arguments forwarded to it.
Probe = tuple[types.ModuleType, dict]


def load_scriptlet(path: Path) -> types.ModuleType:
    """Dynamically load a Python file as a module."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _url_to_path(url: str) -> Path:
    """Convert a file:// URL or plain path string to a Path."""
    if url.startswith("file://"):
        return Path(url[7:])
    return Path(url)


def _parse_ruleset(path: Path) -> list[Probe]:
    """Parse a ruleset YAML and return one Probe per entry."""
    data = yaml.safe_load(path.read_text())
    probes: list[Probe] = []
    for entry in data.get("probes", []):
        url = entry.get("url")
        if url:
            probes.append((load_scriptlet(_url_to_path(url)), entry.get("with", {})))
    return probes


def parse_probe_url(url: str) -> list[Probe]:
    """Load a probe URL: .py → scriptlet with no extra args; .yaml → ruleset."""
    path = _url_to_path(url)
    if path.suffix.lower() == ".py":
        return [(load_scriptlet(path), {})]
    return _parse_ruleset(path)


def load_probes(charms: dict[str, Charm], extra_probe_urls: list[str] | None = None) -> list[Probe]:
    """Return all probes for the given charm pool.

    For each unique charm:
      - All probes from the charm's ruleset file (if any).
    Extra probe URLs (user-supplied) are appended last.
    Endpoint optionality and limit checks are handled directly in check_bundle.
    """
    seen: set[str] = set()
    probes: list[Probe] = []

    for charm in charms.values():
        if charm.name in seen:
            continue
        seen.add(charm.name)

        if charm.ruleset_url:
            probes.extend(_parse_ruleset(_url_to_path(charm.ruleset_url)))

    for url in (extra_probe_urls or []):
        probes.extend(parse_probe_url(url))

    return probes


class _MissingRelation(Exception):
    def __init__(self, app: str, endpoint: str):
        super().__init__(f"{app}:{endpoint} is required but has no relation")
        self.app = app
        self.endpoint = endpoint


class _ExceedsLimit(Exception):
    def __init__(self, app: str, endpoint: str, limit: int, count: int):
        super().__init__(f"{app}:{endpoint} has {count} relations but limit is {limit}")
        self.app = app
        self.endpoint = endpoint


def _check_metadata_constraints(bundle: Bundle, signals: list) -> bool:
    """Check endpoint optionality and limits directly from the Bundle model."""
    # Count relations per (app, endpoint) pair.
    counts: dict[tuple[str, str], int] = {}
    for integration in bundle.integrations:
        for ep in integration:
            key = (ep.application, ep.endpoint)
            counts[key] = counts.get(key, 0) + 1

    passed = True
    for app_name, application in bundle.applications.items():
        for ep_name, ep in application.charm.endpoints.items():
            if ep.type == EndpointType.PEERS:
                continue
            count = counts.get((app_name, ep_name), 0)
            if not ep.optional and count == 0:
                signals.append(_MissingRelation(app_name, ep_name))
                passed = False
            elif ep.limit is not None and count > ep.limit:
                signals.append(_ExceedsLimit(app_name, ep_name, ep.limit, count))
                passed = False
    return passed


def check_bundle(bundle: Bundle, probes: list[Probe], signals: list) -> bool:
    """Run metadata constraint checks and all probes against the bundle.

    Clears *signals* then appends any exceptions raised by failing checks/probes.
    Returns True only if everything passed.
    """
    signals.clear()
    passed = _check_metadata_constraints(bundle, signals)

    bundle_dict = yaml.safe_load(bundle.export())
    juju_bundles = {"model": bundle_dict}
    for module, with_args in probes:
        fn = getattr(module, "bundle", None)
        if fn is None:
            continue
        try:
            fn(juju_bundles=juju_bundles, **with_args)
        except Exception as exc:
            signals.append(exc)
            passed = False
    return passed
