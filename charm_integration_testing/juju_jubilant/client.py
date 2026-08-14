# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import jubilant
from juju.resource_registry.handles import JujuModelHandle


class JubilantClient:
    def model(self, model: JujuModelHandle | str | None) -> jubilant.Juju:
        # Most callers pass a JujuModelHandle. A handful of call sites (e.g. Kubernetes-pod
        # hooks) only ever see a bare k8s namespace/model name with no controller available,
        # so a plain str is accepted as-is for backward compatibility.
        model_str = model.uri if isinstance(model, JujuModelHandle) else model
        return jubilant.Juju(
            model=model_str,
            wait_timeout=timedelta(days=1).total_seconds(),
        )
