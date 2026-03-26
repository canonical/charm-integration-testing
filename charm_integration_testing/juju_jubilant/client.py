# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import jubilant


class JubilantClient:
    def model(self, model: str | None) -> jubilant.Juju:
        return jubilant.Juju(
            model=model,
            wait_timeout=timedelta(days=1).total_seconds(),
        )
