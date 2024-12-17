# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

from abc import ABC, abstractmethod

from .structures import CharmDeployment, CharmIntegration


class JujuClient(ABC):
    @abstractmethod
    def deploy(self, deployment: CharmDeployment):
        raise NotImplementedError
    
    @abstractmethod
    def integrate(self, integration: CharmIntegration):
        raise NotImplementedError
