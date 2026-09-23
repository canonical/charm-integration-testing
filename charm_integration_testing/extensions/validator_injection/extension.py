# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
import shlex
import tarfile
import urllib.request
from pathlib import Path

from juju import JujuBackend, JujuExtension, JujuModelHandle, PersistenceKey, rekey_persistence_state_controller

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

    def post_persistence(
        self,
        model: JujuModelHandle,
        application: str,
        persistence: str,
    ) -> dict[str, list[ValidationResult]]:
        if persistence not in _PERSISTENCE_OPS:
            # A caller/programming error, not a remote failure: raise immediately rather than
            # letting the per-unit loop below swallow it into an ERROR result.
            raise ValueError(f"Unsupported persistence op '{persistence}'; expected one of {sorted(_PERSISTENCE_OPS)}")
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
                outcome = self._run_persistence_on_unit(model, unit, persistence, unit_refs, model_is_k8s)
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
                        error=f"Persistence op '{persistence}' failed on {unit}: {exc}",
                    )
                ]
                continue
            if outcome is None:
                # Skipped entirely (no validators_path configured, see _run_persistence_on_unit) -
                # distinct from "ran and found nothing to report", which returns ([], {}) below.
                results[unit] = []
                continue
            unit_results, updated_refs, cleaned_relation_ids_list = outcome
            results[unit] = unit_results

            if persistence == "cleanup":
                # Canary tables have been dropped; drop tracked state only for relation_ids that
                # cleanup_all actually visited and that didn't produce a FAIL/ERROR result. An
                # entry cleanup never visited (e.g. the relation is gone) means cleanup never ran
                # against the backend for it - keep it so orphaned canary data isn't forgotten.
                failed_relation_ids = {
                    result.relation_id for result in unit_results if result.status in ("FAIL", "ERROR")
                }
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
            else:
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
                            error=f"Persistence op '{persistence}' returned a malformed relation_id on {unit}: {exc}",
                        )
                    ]
                    continue
                self.persistence_state.update(new_entries)
        return results

    def post_migrate_model(self, model: str, source: str, target: str) -> None:
        # A migrated model's units and relation_ids are unchanged, but its controller is not, so
        # every tracked key for it is now stale. Re-key in place so callers never have to.
        rekey_persistence_state_controller(self.persistence_state, model, source, target)

    @property
    def models_with_persistence_state(self) -> set[JujuModelHandle]:
        """The models this extension currently holds canary state for."""
        return {JujuModelHandle(controller=key.controller, model=key.model) for key in self.persistence_state}

    def invalidate_persistence_state_for_models(
        self, models: set[JujuModelHandle], units_by_model: dict[JujuModelHandle, set[str]]
    ) -> None:
        """Drop tracked state for the given units in the given models.

        Used when a relation is removed and re-added: the new relation gets a fresh relation_id in
        every participating model, so any state keyed on the old one is stale and must be dropped
        before the next "prepare" seeds fresh canary data.
        """
        for key in [
            key
            for key in self.persistence_state
            if JujuModelHandle(controller=key.controller, model=key.model) in models
            and key.unit in units_by_model.get(JujuModelHandle(controller=key.controller, model=key.model), set())
        ]:
            del self.persistence_state[key]

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
