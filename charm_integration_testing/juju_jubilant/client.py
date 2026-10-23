# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import jubilant
from juju import JujuModelHandle


class JubilantClient:
    def model(self, model: JujuModelHandle | None) -> jubilant.Juju:
        return jubilant.Juju(
            model=model.uri if model is not None else None,
            wait_timeout=timedelta(days=1).total_seconds(),
        )
