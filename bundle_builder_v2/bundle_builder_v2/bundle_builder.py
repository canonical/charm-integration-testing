# Copyright (C) 2025 Canonical Ltd

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.


import logging

from .bundle import Bundle
from .charmhub import CharmhubClient


class UnresolvableBundleError(Exception):
    def __init__(self, message: str, best_bundle: Bundle):
        super().__init__(message)
        self.best_bundle = best_bundle


class BundleBuilder:
    charmhub_client: CharmhubClient
    logger: logging.Logger

    def __init__(
        self,
        charmhub_client: CharmhubClient,
        logger: logging.Logger = logging.getLogger(__name__),
    ):
        self.charmhub_client = charmhub_client
        self.logger = logger

    # Build out the bundle, pulling in charms that fulfill non-optional hanging required integrations
    def build(self, base: Bundle) -> Bundle:
        return base  # Placeholder for actual implementation
