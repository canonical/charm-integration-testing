# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import datetime, timedelta, timezone

from .backend import JujuBackend, JujuWaitIdleTimeoutError


class JujuClient:
    backend: JujuBackend

    def __init__(self, backend):
        self.backend = backend

    def scale_application(self, application: str, num: int):
        print(f"Scaling application {application} to {num} units.")
        self.backend.scale_application(application, num)

    def num_units(self, application: str) -> int:
        print(f"Getting the number of units for {application}.")
        return self.backend.num_units(application)

    def wait_idle(self, model: str = "default", timeout: timedelta = timedelta(days=1)):
        print("Waiting for idle.")
        print("::group::Juju Status:")

        # Loop until timeout
        end_time = datetime.now(timezone.utc) + timeout
        while datetime.now(timezone.utc) < end_time:
            try:
                self.backend.wait_idle(model=model, timeout=timedelta(seconds=5))
            except JujuWaitIdleTimeoutError:
                self.print_status()
            except Exception as e:
                print("::endgroup::")
                raise e
            else:
                self.print_status()
                print("::endgroup::")
                return

        print("::endgroup::")
        raise JujuWaitIdleTimeoutError

    def print_status(self):
        print(f"{datetime.now(timezone.utc)}")
        print()
        print(self.backend.juju_status_text())
        print("-----------------------------")
