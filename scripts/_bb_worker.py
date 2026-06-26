#!/usr/bin/env python3
"""Subprocess worker: run one bundle-builder-x spec and emit a JSON result to stdout.

Invoked by benchmark_suite.py with a JSON payload on stdin:
    {
        "spec_yaml": "...",          # YAML text of the SpecFile
        "overrides_dir": "...",      # absolute path to charm-overrides/
        "charmhub_url": "..."        # optional, may be null
    }

Emits a JSON object to stdout:
    {
        "status": "SAT" | "UNSAT" | "ERROR",
        "elapsed_s": 1.23,
        "n_apps": 5,
        "n_integrations": 8,
        "error": null | "message"
    }

The parent process is responsible for the hard wall-clock timeout (subprocess.run
with timeout=). When that fires, this process is killed and the parent records
status="TIMEOUT".
"""

import json
import sys
import time
from pathlib import Path


def main() -> None:
    payload = json.loads(sys.stdin.read())
    spec_yaml: str = payload["spec_yaml"]
    overrides_dir: str | None = payload.get("overrides_dir")
    charmhub_url: str | None = payload.get("charmhub_url")

    try:
        # Late imports so import errors surface as ERROR, not at module load time.
        import logging

        import yaml

        from bundle_builder_x.bundle_builder import BundleBuilder, UncompletableBundleError
        from bundle_builder_x.charmhub import CharmhubClient
        from bundle_builder_x.charmhub_http import CharmhubHttpClient
        from bundle_builder_x.overrides import OverridesClient
        from bundle_builder_x.snapstore import SnapstoreClient
        from bundle_builder_x.snapstore_http import SnapstoreHttpClient
        from bundle_builder_x.spec import SpecFile

        # Suppress all logging to keep stdout clean for JSON output.
        logging.disable(logging.CRITICAL)
        logger = logging.getLogger("bb_worker")

        raw = yaml.safe_load(spec_yaml)
        spec = SpecFile.model_validate(raw)

        overrides_client = OverridesClient(
            overrides=Path(overrides_dir) if overrides_dir else None,
        )
        charmhub_http = CharmhubHttpClient(
            logger=logger,
            base_url=charmhub_url,
        )
        snapstore_http = SnapstoreHttpClient(logger=logger)
        charmhub_client = CharmhubClient(
            http_client=charmhub_http,
            logger=logger,
            overrides_client=overrides_client,
        )
        builder = BundleBuilder(
            charmhub_client=charmhub_client,
            snapstore_client=SnapstoreClient(http_client=snapstore_http, logger=logger),
            logger=logger,
        )

        t0 = time.perf_counter()
        try:
            solution = builder.build(spec)
            elapsed = time.perf_counter() - t0

            n_apps = sum(len(b.applications) for b in solution.bundles)
            n_integrations = sum(len(b.integrations) for b in solution.bundles)

            result = {
                "status": "SAT",
                "elapsed_s": round(elapsed, 3),
                "n_apps": n_apps,
                "n_integrations": n_integrations,
                "error": None,
            }
        except UncompletableBundleError as exc:
            elapsed = time.perf_counter() - t0
            msg = str(exc)
            # Distinguish "timed out" (Z3 timeout) from truly UNSAT
            status = "UNSAT"
            if "timed out" in msg.lower() or "timeout" in msg.lower():
                status = "SOLVER_TIMEOUT"
            result = {
                "status": status,
                "elapsed_s": round(elapsed, 3),
                "n_apps": 0,
                "n_integrations": 0,
                "error": msg[:500],
            }
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            from bundle_builder_x.charmhub_http import CharmReleaseNotFoundException, UnparsableCharmException
            # Treat missing or unparsable charm releases as UNSAT: the spec is
            # unresolvable on this platform/channel combination, usually because the
            # override file refers to endpoints that don't exist in the requested channel.
            if isinstance(exc, (CharmReleaseNotFoundException, UnparsableCharmException)):
                result = {
                    "status": "UNSAT",
                    "elapsed_s": round(elapsed, 3),
                    "n_apps": 0,
                    "n_integrations": 0,
                    "error": str(exc)[:300],
                }
            else:
                result = {
                    "status": "ERROR",
                    "elapsed_s": round(elapsed, 3),
                    "n_apps": 0,
                    "n_integrations": 0,
                    "error": repr(exc)[:500],
                }

    except Exception as exc:
        result = {
            "status": "ERROR",
            "elapsed_s": 0.0,
            "n_apps": 0,
            "n_integrations": 0,
            "error": repr(exc)[:500],
        }

    print(json.dumps(result))


if __name__ == "__main__":
    main()
