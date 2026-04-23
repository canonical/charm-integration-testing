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

import logging
import time

from pydantic import BaseModel, ConfigDict


class Span(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    start: float
    duration: float


class _SpanToken(BaseModel):
    full_label: str
    start: float


class Timeline:
    """Collects timing spans and can produce a Gantt chart or text report."""

    def __init__(
        self,
        logger: logging.Logger | None = None,
        _spans: list[Span] | None = None,
        _prefix: str = "",
    ) -> None:
        self.logger = logger.getChild("timeline") if logger else None
        self._spans: list[Span] = _spans if _spans is not None else []
        self._prefix = _prefix

    def child(self, name: str) -> "Timeline":
        """Return a child Timeline that shares the same span list with a label prefix."""
        prefix = f"{self._prefix}{name}/" if self._prefix else f"{name}/"
        return Timeline(logger=self.logger, _spans=self._spans, _prefix=prefix)

    def on(self, label: str) -> _SpanToken:
        """Start a timing span. Returns a token to pass to off()."""
        full_label = f"{self._prefix}{label}"
        return _SpanToken(full_label=full_label, start=time.monotonic())

    def off(self, token: _SpanToken) -> None:
        """End a timing span and record it."""
        duration = time.monotonic() - token.start
        span = Span(label=token.full_label, start=token.start, duration=duration)
        self._spans.append(span)
        if self.logger is not None:
            self.logger.debug(f"[{token.full_label}] {duration:.3f}s")

    def report(self) -> str:
        """Return a text summary of all recorded spans, sorted by duration descending."""
        if not self._spans:
            return "No spans recorded."
        lines = ["Timing report:"]
        for span in sorted(self._spans, key=lambda s: s.duration, reverse=True):
            lines.append(f"  {span.label}: {span.duration:.3f}s")
        return "\n".join(lines)

    def mermaid(self, markdown: bool = False) -> str:
        """Return a Mermaid Gantt diagram of recorded spans.

        If markdown=True, wraps the diagram in a fenced code block.
        """
        if not self._spans:
            diagram = "gantt\n    title Bundle Builder Timeline\n    dateFormat x\n"
        else:
            origin = min(s.start for s in self._spans)
            lines = ["gantt", "    title Bundle Builder Timeline", "    dateFormat x"]

            # Group spans by their top-level prefix (first path component)
            sections: dict[str, list[Span]] = {}
            for span in self._spans:
                parts = span.label.split("/", 1)
                section = parts[0] if len(parts) > 1 else "other"
                sections.setdefault(section, []).append(span)

            for section, spans in sections.items():
                lines.append(f"    section {_sanitize_mermaid(section)}")
                for span in spans:
                    label = _sanitize_mermaid(span.label.split("/", 1)[-1] if "/" in span.label else span.label)
                    start_ms = int((span.start - origin) * 1000)
                    duration_ms = max(1, int(span.duration * 1000))
                    end_ms = start_ms + duration_ms
                    lines.append(f"        {label} : {start_ms}, {end_ms}")

            diagram = "\n".join(lines) + "\n"

        if markdown:
            return f"```mermaid\n{diagram}```\n"
        return diagram


def _sanitize_mermaid(label: str) -> str:
    """Sanitize a label for use in a Mermaid diagram."""
    return label.replace(":", " -").replace(",", " ")


class NullTimeline(Timeline):
    """Drop-in replacement for Timeline that records nothing."""

    def __init__(self) -> None:
        pass

    def child(self, name: str) -> "NullTimeline":
        return self

    def on(self, label: str) -> _SpanToken:
        return _SpanToken(full_label="", start=0.0)

    def off(self, token: _SpanToken) -> None:
        pass

    def report(self) -> str:
        return ""

    def mermaid(self, markdown: bool = False) -> str:
        return ""
