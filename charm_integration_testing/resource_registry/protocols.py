# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from typing import Protocol, runtime_checkable


@runtime_checkable
class ResourceHandle(Protocol):
    @property
    def resource_id(self) -> str: ...

    @property
    def resource_type(self) -> str: ...

    @property
    def path_segment(self) -> str: ...


class LogCollector(Protocol):
    def supports(self, handle: ResourceHandle) -> bool: ...

    def collect(self, handle: ResourceHandle) -> None: ...
