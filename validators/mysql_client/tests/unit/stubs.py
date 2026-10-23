# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Connection/cursor stubs shared by the functional and persistence validator tests."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CursorStub:
    """Minimal cursor context manager; raises execute_error if set."""

    execute_error: Exception | None = None
    # Rows returned by fetchone() for each successive call.
    fetchone_rows: list[tuple[Any, ...] | None] = field(default_factory=list)
    # Rows returned by fetchall().
    fetchall_rows: list[tuple[Any, ...]] = field(default_factory=list)
    # Number of execute() calls to allow before raising execute_error.
    execute_succeed_count: int = 0
    lastrowid: int = 1
    _fetch_count: int = field(default=0, init=False, repr=False)
    _execute_count: int = field(default=0, init=False, repr=False)
    executed_queries: list[str] = field(default_factory=list, init=False, repr=False)
    executed_params: list[Any] = field(default_factory=list, init=False, repr=False)

    def execute(self, query: str, params: Any = None) -> None:
        self.executed_queries.append(query)
        self.executed_params.append(params)
        if self.execute_error and self._execute_count >= self.execute_succeed_count:
            raise self.execute_error
        self._execute_count += 1

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._fetch_count < len(self.fetchone_rows):
            row = self.fetchone_rows[self._fetch_count]
            self._fetch_count += 1
            return row
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.fetchall_rows

    def __enter__(self) -> "CursorStub":
        return self

    def __exit__(self, *args: object) -> None:
        pass


@dataclass
class ConnStub:
    """Minimal connection stub; cursor_stub is returned by cursor()."""

    cursor_stub: CursorStub = field(default_factory=CursorStub)
    autocommit_calls: list[bool] = field(default_factory=list, init=False, repr=False)

    def cursor(self) -> CursorStub:
        return self.cursor_stub

    def autocommit(self, value: bool) -> None:
        self.autocommit_calls.append(value)

    def close(self) -> None:
        pass
