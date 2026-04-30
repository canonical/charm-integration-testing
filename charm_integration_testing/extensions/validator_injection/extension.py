# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import tarfile
import urllib.request
from pathlib import Path

from juju import JujuBackend, JujuExtension

from validators.base.validator import ValidationResult
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
venv_runner = f"{remote_validators_path}/venv/bin/run_validators"
uv_bin = f"{remote_validators_path}/uv"
uv_url = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-unknown-linux-musl.tar.gz"


class ValidatorInjectorExtension(JujuExtension):
    validators_path: Path | None
    uv_file: Path | None
    juju: JujuBackend
    logger: logging.Logger

    def __init__(
        self,
        validators_path: Path | None,
        juju: JujuBackend,
        logger: logging.Logger,
        uv_file: Path | None = None,
    ) -> None:
        self.validators_path = validators_path
        self.uv_file = uv_file
        self.juju = juju
        self.logger = logger.getChild("ValidatorInjectorExtension")

    def post_validate(self, model: str, application: str, level: str) -> dict[str, list[ValidationResult]]:
        results: dict[str, list[ValidationResult]] = {}
        model_is_k8s = self.juju.is_k8s_model(model)
        for unit in self.juju.application_units(model, application):
            results[unit] = self._run_validators_on_unit(model, unit, level, model_is_k8s)
        return results

    def _run_validators_on_unit(self, model: str, unit: str, level: str, is_k8s: bool = True) -> list[ValidationResult]:
        # Inject validators
        if self.juju.exec_unit(model, unit, f"test -f {venv_runner}", operator=is_k8s).return_code != 0:
            if not self.validators_path:
                self.logger.warning(f"Validators path not provided, skipping injection on {unit}")
                return []
            self._inject_validators(model, unit, is_k8s=is_k8s)

        # Run validators
        self.logger.debug(f"Running validation on unit {unit}")
        run_result = self.juju.exec_unit(model, unit, f"{venv_runner} --level {level}", operator=is_k8s)
        if run_result.return_code != 0:
            raise RuntimeError(f"Validators failed on {unit} (rc={run_result.return_code}): {run_result.stderr}")

        # Collect results
        return ValidatorRunnerResults.model_validate_json(run_result.stdout).results

    def _inject_validators(self, model: str, unit: str, is_k8s: bool = True) -> None:
        # Ensure validators path is provided
        if self.validators_path is None:
            raise ValueError("validators_path must be provided to inject validators")
        self.logger.debug(f"Injecting validators on unit {unit}")

        # Copy validators
        self.logger.debug(f"[{unit}] copying validators to {remote_validators_path}")
        mkdir = f"mkdir -p {remote_validators_path}"
        if not is_k8s:
            mkdir = f"sudo {mkdir} && sudo chown -R $(id -u) {remote_validators_path}"
        self.juju.ssh(model, unit, mkdir)
        self.juju.scp(model, str(self.validators_path.resolve()), f"{unit}:{remote_validators_path}/packages")

        # Copy uv binary
        uv_file = self._get_uv_file()
        self.logger.debug(f"[{unit}] copying uv to {uv_bin}")
        self.juju.scp(model, str(uv_file.resolve()), f"{unit}:{uv_bin}")

        # Install validators
        for cmd, desc in [
            (f"chmod +x {uv_bin}", "make uv executable"),
            (
                f"{proxy_env} {uv_bin} venv --python '>=3.10' {remote_validators_path}/venv",
                "create venv with python 3.10+",
            ),
            (
                f"{proxy_env} {uv_bin} pip install --python {remote_validators_path}/venv"
                f" {remote_validators_path}/packages/*",
                "install validator packages",
            ),
        ]:
            self.logger.debug(f"[{unit}] {desc} with command: {cmd}")
            result = self.juju.exec_unit(model, unit, cmd, operator=is_k8s)
            if result.return_code != 0:
                raise RuntimeError(f"Failed to {desc} on {unit} (rc={result.return_code}): {result.stderr}")

    def _get_uv_file(self) -> Path:
        if self.uv_file is None:
            self.logger.debug(f"Downloading uv from {uv_url}")
            # As a snap Juju cannot access /tmp, so download into the current folder
            archive_path, _ = urllib.request.urlretrieve(uv_url)  # nosec B310
            with tarfile.open(archive_path) as tar:
                # The tarball contains uv-<arch>/uv — extract just the binary
                member = next(m for m in tar.getmembers() if m.name.endswith("/uv") and not m.isdir())
                f = tar.extractfile(member)
                if f is None:
                    raise RuntimeError("Could not extract uv binary from archive")
                Path("uv").write_bytes(f.read())
            self.uv_file = Path("uv")
        return self.uv_file
