# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .protocols import LogCollector, ResourceHandle


class ResourceTeardownWarning(UserWarning):
    """Emitted when a resource teardown step fails during registry cleanup."""


@dataclass
class ResourceEntry:
    handle: ResourceHandle
    destroyer: Callable[[], None] | None
    parent_id: str | None
    created_at: datetime
    collectors: list[LogCollector] = field(default_factory=list)
    logs_collected: bool = False
    destroyed: bool = False


class ResourceRegistry:
    def __init__(
        self,
        global_collectors: list[LogCollector],
        logger: logging.Logger,
    ) -> None:
        self._global_collectors = global_collectors
        self._logger = logger
        # Ordered dict: resource_id -> ResourceEntry
        self._entries: dict[str, ResourceEntry] = {}
        # parent_id -> list of child resource_ids (in registration order)
        self._children: dict[str, list[str]] = defaultdict(list)

    def register(
        self,
        handle: ResourceHandle,
        destroyer: Callable[[], None] | None = None,
        collectors: list[LogCollector] | None = None,
        parent: ResourceHandle | None = None,
    ) -> None:
        parent_id = parent.resource_id if parent is not None else None
        entry = ResourceEntry(
            handle=handle,
            destroyer=destroyer,
            parent_id=parent_id,
            created_at=datetime.utcnow(),
            collectors=list(collectors) if collectors is not None else [],
        )
        self._entries[handle.resource_id] = entry
        if parent_id is not None:
            self._children[parent_id].append(handle.resource_id)
        self._logger.debug(f"ResourceRegistry: registered {handle.resource_type} '{handle.resource_id}'")

    def deregister(self, handle: ResourceHandle) -> None:
        entry = self._entries.pop(handle.resource_id, None)
        if entry is not None and entry.parent_id is not None:
            siblings = self._children.get(entry.parent_id)
            if siblings is not None:
                self._children[entry.parent_id] = [child_id for child_id in siblings if child_id != handle.resource_id]
                if not self._children[entry.parent_id]:
                    self._children.pop(entry.parent_id, None)
        self._children.pop(handle.resource_id, None)
        self._logger.debug(f"ResourceRegistry: deregistered {handle.resource_type} '{handle.resource_id}'")

    def collect_logs(self, handle: ResourceHandle) -> None:
        entry = self._entries.get(handle.resource_id)
        if entry is None:
            self._logger.debug(f"ResourceRegistry: no entry for '{handle.resource_id}', skipping log collection")
            return
        self._logger.debug(f"ResourceRegistry: collecting logs for {handle.resource_type} '{handle.resource_id}'")
        all_collectors = [c for c in entry.collectors if c.supports(handle)] + [
            c for c in self._global_collectors if c.supports(handle)
        ]
        for collector in all_collectors:
            try:
                collector.collect(handle)
            except Exception as exc:
                self._logger.debug(
                    f"ResourceRegistry: collector {type(collector).__name__} failed for "
                    f"'{handle.resource_id}': {exc}"
                )
        entry.logs_collected = True

    def teardown(self, handle: ResourceHandle) -> None:
        """Depth-first teardown: collect and destroy children, then collect and destroy self."""
        entry = self._entries.get(handle.resource_id)
        if entry is None:
            return

        # Collect and destroy children first (depth-first)
        for child_id in list(self._children.get(handle.resource_id, [])):
            child_entry = self._entries.get(child_id)
            if child_entry is not None:
                self.teardown(child_entry.handle)

        # Collect logs for self
        if not entry.logs_collected:
            try:
                self.collect_logs(handle)
            except Exception as exc:
                warnings.warn(
                    f"Log collection failed for '{handle.resource_id}': {exc}",
                    ResourceTeardownWarning,
                    stacklevel=2,
                )

        # Destroy self
        if not entry.destroyed and entry.destroyer is not None:
            try:
                self._logger.debug(f"ResourceRegistry: destroying {handle.resource_type} '{handle.resource_id}'")
                entry.destroyer()
                entry.destroyed = True
            except Exception as exc:
                warnings.warn(
                    f"Destruction failed for '{handle.resource_id}': {exc}",
                    ResourceTeardownWarning,
                    stacklevel=2,
                )

        self.deregister(handle)

    def teardown_all(self) -> None:
        """Tear down all root resources in reverse registration order."""
        root_ids = [resource_id for resource_id, entry in self._entries.items() if entry.parent_id is None]
        self._logger.debug(f"ResourceRegistry: teardown_all starting, {len(root_ids)} root resource(s)")
        for resource_id in reversed(root_ids):
            entry = self._entries.get(resource_id)
            if entry is None:
                continue
            try:
                self.teardown(entry.handle)
            except Exception as exc:
                warnings.warn(
                    f"teardown failed for '{resource_id}': {exc}",
                    ResourceTeardownWarning,
                    stacklevel=2,
                )
        self._logger.debug("ResourceRegistry: teardown_all complete")
