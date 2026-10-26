# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path
from xml.etree import ElementTree  # nosec B405

from pydantic import BaseModel


class ObserverResult(BaseModel):
    name: str
    status: str
    template_id: str
    category: str
    comment: str
    io_log: str


def junit_results(path: Path, scenario: str) -> list[ObserverResult]:
    """Convert pytest JUnit results to Test Observer's existing test-results schema."""
    root = ElementTree.parse(path).getroot()  # nosec B314
    results = []
    for suite in [root] if root.tag == "testsuite" else root.findall("testsuite"):
        for case in suite.findall("testcase"):
            name = case.get("name")
            if not name:
                raise ValueError(f"Missing or empty testcase name in {path}")
            classname = case.get("classname", "")
            errors = case.findall("error") + case.findall("failure")
            skipped = case.findall("skipped")
            results.append(
                ObserverResult(
                    name=f"{scenario}/{classname}::{name}",
                    status="FAILED" if errors else "SKIPPED" if skipped else "PASSED",
                    template_id=classname,
                    category=f"{scenario}/{suite.get('name', '')}",
                    comment="\n".join(item.get("message", "") for item in errors + skipped),
                    io_log="\n".join(item.text or "" for item in case.findall("system-out")),
                )
            )
    if not results:
        raise ValueError(f"No test results found in {path}")
    return results
