# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
import subprocess
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import pytest
from ci_runner.config import Config, Scenario
from ci_runner.runner import run_scenarios
from test_observer_client import TestObserverClient as ObserverClient
from test_observer_client import TestObserverClientError as ObserverError

from bundle_builder_x import Charm, CharmChannel


@pytest.fixture
def server() -> Iterator[tuple[str, list[dict[str, Any]], list[int]]]:
    requests: list[dict[str, Any]] = []
    statuses = [200]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                }
            )
            self.send_response(statuses[0])
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=httpd.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", requests, statuses
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()


def test_observer_upload_uses_verified_endpoint_and_does_not_retry_writes(
    server: tuple[str, list[dict[str, Any]], list[int]],
) -> None:
    api, requests, statuses = server
    with ObserverClient(logging.getLogger(__name__), api, "test-token") as observer:
        observer.post_results(123, [{"name": "test_deploy", "status": "PASSED"}])
        statuses[0] = 503
        with pytest.raises(ObserverError, match="HTTP 503") as error:
            observer.post_results(123, [{"name": "test_deploy", "status": "FAILED"}])
    assert len(requests) == 2
    assert requests[0]["path"] == "/v1/test-executions/123/test-results"
    assert requests[0]["body"] == [{"name": "test_deploy", "status": "PASSED"}]
    assert requests[0]["authorization"] == "Bearer test-token"
    assert "test-token" not in str(error.value)


@pytest.mark.parametrize(
    ("test_exit", "report_status", "expected"),
    [(0, 200, 0), (1, 200, 1), (0, 503, 1), (1, 503, 1)],
    ids=["success", "test-failure", "report-failure", "both-fail"],
)
def test_reporting_preserves_test_and_upload_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: tuple[str, list[dict[str, Any]], list[int]],
    test_exit: int,
    report_status: int,
    expected: int,
) -> None:
    api, requests, statuses = server
    statuses[0] = report_status
    monkeypatch.setenv("TEST_OBSERVER_API", api)
    monkeypatch.setenv("TEST_OBSERVER_TOKEN", "test-token")

    def execute(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        report = Path(argv[argv.index("--junitxml") + 1])
        report.write_text(
            '<testsuites><testsuite name="pytest"><testcase name="test_deploy">'
            + ('<failure message="failed"/>' if test_exit else "")
            + "</testcase></testsuite></testsuites>"
        )
        return subprocess.CompletedProcess(argv, test_exit)

    monkeypatch.setattr("ci_runner.runner.subprocess.run", execute)
    target = Charm(
        name="target",
        local_path="/tmp/local.charm",
        channel=CharmChannel.model_validate("latest/stable"),
        revision=-1,
        ubuntu_version="24.04",
        ubuntu_arch="amd64",
        endpoints={},
        platforms=["kubernetes"],
    )
    scenario = Scenario(endpoint="db", neighbor="client", neighbor_endpoint="db")
    status = run_scenarios(target, [scenario], Config(observer_execution_id=123), "k8s", "kubernetes", tmp_path)
    assert status == expected
    assert len(requests) == 1
    assert requests[0]["body"][0]["status"] == ("FAILED" if test_exit else "PASSED")
