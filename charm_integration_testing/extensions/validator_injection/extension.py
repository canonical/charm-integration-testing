# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path

from juju import JujuBackend, JujuExtension

from validators.runner import ValidatorRunnerResults

proxy_env = " ".join(
    [
        f"{var}={value}"
        for var, value in {
            "HTTP_PROXY": "$JUJU_CHARM_HTTP_PROXY",
            "HTTPS_PROXY": "$JUJU_CHARM_HTTPS_PROXY",
            "NO_PROXY": "$JUJU_CHARM_NO_PROXY",
        }.items()
    ]
)
remote_validators_path = "/var/lib/validators"
remote_packages_path = f"{remote_validators_path}/packages"
venv_runner = f"{remote_validators_path}/.venv/bin/run_validators"
uv_bin = f"{remote_validators_path}/uv"
uv_url = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-unknown-linux-musl.tar.gz"


class ValidatorInjectorExtension(JujuExtension):
    validators_path: Path | None
    juju: JujuBackend
    logger: logging.Logger

    def __init__(self, validators_path: Path | None, juju: JujuBackend, logger: logging.Logger) -> None:
        self.validators_path = validators_path
        self.juju = juju
        self.logger = logger.getChild("ValidatorInjectorExtension")

    def post_validate(self, model: str, application: str, level: str) -> None:
        units = self.juju.application_units(model, application)
        for unit in units:
            self._run_validators_on_unit(model, unit, level)

    def _run_validators_on_unit(self, model: str, unit: str, level: str) -> None:
        # Inject validators
        if self.juju.exec_unit(model, unit, f"test -f {venv_runner}").return_code != 0:
            if not self.validators_path:
                self.logger.warning(f"Validators path not provided, skipping injection on {unit}")
                return
            self._inject_validators(model, unit)

        # Run validators
        self.logger.debug(f"Running validation on unit {unit}")
        run_result = self.juju.exec_unit(model, unit, f"{venv_runner} --level {level}")
        if run_result.return_code != 0:
            raise RuntimeError(f"Validators failed on {unit} (rc={run_result.return_code}): {run_result.stderr}")

        # Collect results
        validator_results = ValidatorRunnerResults.model_validate_json(run_result.stdout)
        failures = []
        for r in validator_results.results:
            if r.status == "PASS":
                self.logger.debug(f"[{unit}] endpoint {r.endpoint}: PASS")
            else:
                if r.error:
                    msg = r.error
                elif r.checks:
                    failed_checks = [c for c in r.checks if not c.passed]
                    msg = f"{len(failed_checks)}/{len(r.checks)} checks failed: {[c.name for c in failed_checks]}"
                else:
                    msg = r.status
                self.logger.error(f"[{unit}] endpoint {r.endpoint}: {msg}")
                failures.append(r.endpoint)

        # Raise if there are any failures
        if failures:
            raise RuntimeError(f"Validation failures on {unit}: {', '.join(failures)}")

    def _inject_validators(self, model: str, unit: str) -> None:
        # Ensure validators path is provided
        if self.validators_path is None:
            raise ValueError("validators_path must be provided to inject validators")
        self.logger.debug(f"Injecting validators on unit {unit}")

        # Copy validators
        self.logger.debug(f"[{unit}] copying validators to {remote_packages_path}")
        self.juju.ssh(model, unit, f"mkdir -p {remote_validators_path}")
        self.juju.scp(model, str(self.validators_path.resolve()), f"{unit}:{remote_packages_path}")

        # Install validators
        for cmd, desc in [
            (
                f"{proxy_env} curl -LsSf {uv_url}" f" | tar -xz -C {remote_validators_path} --strip-components=1",
                "install uv",
            ),
            (
                f"{proxy_env} {uv_bin} venv --python 3.10 {remote_validators_path}/.venv",
                "create venv with python 3.10",
            ),
            (
                f"{proxy_env} {uv_bin} pip install --python {remote_validators_path}/.venv"
                f" {remote_packages_path}/*",
                "install validator packages",
            ),
        ]:
            self.logger.debug(f"[{unit}] {desc} with command: {cmd}")
            result = self.juju.exec_unit(model, unit, cmd)
            if result.return_code != 0:
                raise RuntimeError(f"Failed to {desc} on {unit} (rc={result.return_code}): {result.stderr}")
