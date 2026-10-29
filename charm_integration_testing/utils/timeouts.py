# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_deadline: ContextVar[float | None] = ContextVar("operation_deadline", default=None)


@contextmanager
def timeout_after(seconds: float) -> Iterator[None]:
    """Constrain cooperative operations to a shared monotonic deadline.

    Blocking calls must use remaining_timeout(); nested budgets cannot extend
    their parent's deadline. Copy the context when submitting work to threads.
    """
    if seconds <= 0:
        raise ValueError("Timeout must be positive.")
    deadline = time.monotonic() + seconds
    parent = _deadline.get()
    token = _deadline.set(min(parent, deadline) if parent is not None else deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining_timeout(seconds: float) -> float:
    """Return a blocking call's budget, capped by the current operation deadline."""
    deadline = _deadline.get()
    remaining = seconds if deadline is None else min(seconds, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("Operation deadline exceeded.")
    return remaining
