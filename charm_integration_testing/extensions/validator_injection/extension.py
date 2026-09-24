# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
import shlex
import tarfile
import urllib.request
from pathlib import Path

from juju import (
    JujuBackend,
    JujuExtension,
    JujuIntegrationApplication,
    JujuModelHandle,
    JujuValidationError,
    PersistenceKey,
    rekey_persistence_state_controller,
)

from validators.base.validator import PersistenceState, ValidationResult
from validators.runner import ValidatorRunnerResults

install_env = " ".join(
    [
        f"{var}={value}"
        for var, value in {
            "HTTP_PROXY": "$JUJU_CHARM_HTTP_PROXY",
            "HTTPS_PROXY": "$JUJU_CHARM_HTTPS_PROXY",
            "NO_PROXY": "$JUJU_CHARM_NO_PROXY",
            "UV_NO_CACHE": "1",
        }.items()
    ]
)
remote_validators_path = "/var/lib/juju/validators"
venv_runner = f"{remote_validators_path}/venv/bin/run_validators"
uv_bin = f"{remote_validators_path}/uv"
uv_url = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-unknown-linux-musl.tar.gz"

# The ops run_validators' --persistence flag accepts (see validators/runner/runner.py).
_PERSISTENCE_OPS = frozenset({"prepare", "checkpoint", "cleanup"})


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
        # Canary state seeded by "prepare" and advanced by "checkpoint", keyed by
        # PersistenceKey(controller, model, unit, relation_id). Owned here rather than threaded
        # through validate_model by callers, so a single session-scoped instance is the one source
        # of truth across every client built during a run.
        self.persistence_state: dict[PersistenceKey, PersistenceState] = {}

    def post_validate(self, model: JujuModelHandle, application: str, level: str) -> dict[str, list[ValidationResult]]:
        results: dict[str, list[ValidationResult]] = {}
        model_is_k8s = self.juju.is_k8s_model(model)
        for unit in self.juju.application_units(model, application):
            results[unit] = self._run_validators_on_unit(model, unit, level, model_is_k8s)
        return results

    def persistence_operation(self, model: JujuModelHandle) -> str:
        # Auto-decide the op from tracked state: any state for this model means a previous
        # "prepare" seeded canary data, so this run must verify it ("checkpoint"); otherwise
        # seed it ("prepare"). This is selected once per validate_model() call, not per
        # application, so one application's prepare updates do not make later applications in the
        # same model switch to checkpoint before they have seeded their own state.
        if any(key.controller == model.controller and key.model == model.model for key in self.persistence_state):
            return "checkpoint"
        return "prepare"

    def post_persistence(
        self,
        model: JujuModelHandle,
        application: str,
        persistence: str | None = None,
    ) -> dict[str, list[ValidationResult]]:
        op = persistence or self.persistence_operation(model)
        results: dict[str, list[ValidationResult]] = {}
        model_is_k8s = self.juju.is_k8s_model(model)
        for unit in self.juju.application_units(model, application):
            # Slice persistence_state down to just this unit's relations; the runner only ever
            # needs (and only ever reports back) refs for the unit it's running on.
            unit_refs = {
                key.relation_id: state
                for key, state in self.persistence_state.items()
                if key.controller == model.controller and key.model == model.model and key.unit == unit
            }
            try:
                outcome = self._run_persistence_on_unit(model, unit, op, unit_refs, model_is_k8s)
            except Exception as exc:
                # Report a transport/remote-command failure as an ERROR result for this unit
                # (mirroring how ValidatorRunner turns a validator-level exception into an ERROR
                # result) so validate_model()'s normal FAIL/ERROR aggregation surfaces it, while
                # every other unit still gets its own attempt.
                results[unit] = [
                    ValidationResult(
                        status="ERROR",
                        endpoint="",
                        interface="",
                        role="requires",
                        level="deep",
                        relation_id=-1,
                        error=f"Persistence op '{op}' failed on {unit}: {exc}",
                    )
                ]
                continue
            if outcome is None:
                # Skipped entirely (no validators_path configured, see _run_persistence_on_unit) -
                # distinct from "ran and found nothing to report", which returns ([], {}) below.
                results[unit] = []
                continue
            unit_results, updated_refs, _ = outcome
            results[unit] = unit_results

            try:
                # Build every key/state pair before mutating persistence_state: a malformed
                # remote payload (updated_refs is parsed from JSON, so Pydantic accepts any
                # string key) must not partially apply this unit's updates before failing.
                new_entries = {
                    PersistenceKey(
                        controller=model.controller,
                        model=model.model,
                        unit=unit,
                        relation_id=int(relation_id_str),
                    ): state
                    for relation_id_str, state in updated_refs.items()
                }
            except (TypeError, ValueError) as exc:
                results[unit] = [
                    ValidationResult(
                        status="ERROR",
                        endpoint="",
                        interface="",
                        role="requires",
                        level="deep",
                        relation_id=-1,
                        error=f"Persistence op '{op}' returned a malformed relation_id on {unit}: {exc}",
                    )
                ]
                continue
            self.persistence_state.update(new_entries)
        return results

    def pre_remove(self, model: JujuModelHandle, *applications: str) -> None:
        # Cleanup runs before the backend removes the application, while its relations still
        # exist, so cleanup_all can visit them and drop canary data. No tracked state for this
        # model (e.g. a CMR provider side, which raises PersistenceNotApplicable) is a no-op.
        self._cleanup_model(model)

    def pre_remove_integration(
        self,
        model: JujuModelHandle,
        endpoint_1: JujuIntegrationApplication,
        endpoint_2: JujuIntegrationApplication,
    ) -> None:
        # For a CMR teardown the integration is removed before the applications, so cleanup
        # must run here (relations still exist) rather than in pre_remove.
        endpoint_filters: dict[str, set[str]] = {}
        for endpoint in (endpoint_1, endpoint_2):
            endpoint_filters.setdefault(endpoint.application, set()).add(endpoint.endpoint)
        self._cleanup_model(model, endpoint_filters=endpoint_filters)

    def _cleanup_model(self, model: JujuModelHandle, endpoint_filters: dict[str, set[str]] | None = None) -> None:
        """Drop canary data for every unit with tracked state in the model, raising on failure.

        Cleanup must run while the model's relations still exist, so it is invoked from the
        pre-removal hooks rather than from validate_model. Unlike post_persistence's cleanup
        branch, failures here raise (JujuValidationError) rather than returning ERROR results,
        because the caller is about to destroy the model's relations: a silent failure would
        strand canary data forever.
        """
        failed_validations: dict[str, list[ValidationResult]] = {}
        model_is_k8s = self.juju.is_k8s_model(model)
        for unit in sorted(
            {
                key.unit
                for key in self.persistence_state
                if key.controller == model.controller and key.model == model.model
            }
        ):
            application = unit.split("/", maxsplit=1)[0]
            if unit not in self.juju.application_units(model, application):
                for key in [
                    key
                    for key in self.persistence_state
                    if key.controller == model.controller and key.model == model.model and key.unit == unit
                ]:
                    del self.persistence_state[key]
                continue
            endpoints = endpoint_filters.get(application) if endpoint_filters is not None else None
            if endpoint_filters is not None and not endpoints:
                continue
            outcome = self._run_persistence_on_unit(model, unit, "cleanup", {}, model_is_k8s, endpoints=endpoints)
            if outcome is None:
                # No validators_path configured: nothing was cleaned, so keep the state so
                # orphaned canary data isn't forgotten.
                continue
            unit_results, _, cleaned_relation_ids_list = outcome
            failed_relation_ids = {result.relation_id for result in unit_results if result.status in ("FAIL", "ERROR")}
            cleaned_relation_ids = set(cleaned_relation_ids_list) - failed_relation_ids
            for key in [
                key
                for key in self.persistence_state
                if key.controller == model.controller
                and key.model == model.model
                and key.unit == unit
                and key.relation_id in cleaned_relation_ids
            ]:
                del self.persistence_state[key]
            if failed_relation_ids:
                failed_validations[unit] = [result for result in unit_results if result.status in ("FAIL", "ERROR")]
        if failed_validations:
            raise JujuValidationError(failed_validations)

    def post_migrate_model(self, model: str, source: str, target: str) -> None:
        # A migrated model's units and relation_ids are unchanged, but its controller is not, so
        # every tracked key for it is now stale. Re-key in place so callers never have to.
        rekey_persistence_state_controller(self.persistence_state, model, source, target)

    def _run_validators_on_unit(
        self, model: JujuModelHandle, unit: str, level: str, is_k8s: bool = True
    ) -> list[ValidationResult]:
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

    def _run_persistence_on_unit(
        self,
        model: JujuModelHandle,
        unit: str,
        persistence: str,
        refs: dict[int, PersistenceState],
        is_k8s: bool = True,
        endpoints: set[str] | None = None,
    ) -> tuple[list[ValidationResult], dict[str, PersistenceState], list[int]] | None:
        if persistence not in _PERSISTENCE_OPS:
            raise ValueError(f"Unsupported persistence op '{persistence}'; expected one of {sorted(_PERSISTENCE_OPS)}")

        # Inject validators
        if self.juju.exec_unit(model, unit, f"test -f {venv_runner}", operator=is_k8s).return_code != 0:
            if not self.validators_path:
                # An unconfigured validators_path means no validators are being tested at all, so
                # this must be a silent skip rather than a hard failure. Return None (not the
                # empty-but-ran ([], {})) so post_persistence() can tell a skip apart from cleanup
                # finding nothing to report.
                self.logger.warning(f"Validators path not provided, skipping persistence op '{persistence}' on {unit}")
                return None
            self._inject_validators(model, unit, is_k8s=is_k8s)

        # Run persistence op
        self.logger.debug(f"Running persistence op '{persistence}' on unit {unit}")
        cmd = f"{venv_runner} --persistence {shlex.quote(persistence)}"
        if persistence == "checkpoint":
            refs_json = json.dumps({str(relation_id): state.model_dump() for relation_id, state in refs.items()})
            cmd += f" --refs {shlex.quote(refs_json)}"
        if endpoints is not None:
            cmd += f" --endpoints {shlex.quote(json.dumps(sorted(endpoints)))}"
        run_result = self.juju.exec_unit(model, unit, cmd, operator=is_k8s)
        if run_result.return_code != 0:
            raise RuntimeError(
                f"Persistence op '{persistence}' failed on {unit} (rc={run_result.return_code}): {run_result.stderr}"
            )

        # Collect results
        parsed = ValidatorRunnerResults.model_validate_json(run_result.stdout)
        return parsed.results, parsed.updated_refs, parsed.cleaned_relation_ids

    def _inject_validators(self, model: JujuModelHandle, unit: str, is_k8s: bool = True) -> None:
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
                f"{install_env} {uv_bin} venv --python '>=3.10' {remote_validators_path}/venv",
                "create venv with python 3.10+",
            ),
            (
                f"{install_env} {uv_bin} pip install --python {remote_validators_path}/venv {remote_validators_path}/packages/*",
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
