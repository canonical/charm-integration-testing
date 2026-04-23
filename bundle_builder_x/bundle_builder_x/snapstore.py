# Copyright (C) 2026 Canonical Ltd

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

from .snapstore_http import SnapstoreHttpClient, SnapVersionNotFoundException


class SnapstoreClient:
    http_client: SnapstoreHttpClient
    logger: logging.Logger

    def __init__(
        self,
        http_client: SnapstoreHttpClient | None = None,
        logger: logging.Logger = logging.getLogger(__name__),
    ) -> None:
        self.http_client = http_client if http_client is not None else SnapstoreHttpClient(logger=logger)
        self.logger = logger

    def resolve_snap_version(self, snap: str, channel: str) -> str:
        """Resolve a snap channel to a version string.

        Args:
            snap: The name of the snap.
            channel: A snap channel name such as "3/stable" or "4.0/edge".

        Returns:
            The version string for the snap revision released in that channel
            (e.g. "3.6.21").

        Raises:
            SnapVersionNotFoundException: If the channel has no released revision, or
                the channel name is not present in the snap's channel map.
        """
        self.logger.debug(f"Resolving version for snap {snap!r} and channel {channel!r}")
        snap_info = self.http_client.snap_info(snap)

        for entry in snap_info.channel_map:
            if entry.channel.name == channel:
                self.logger.debug(f"Found version {entry.version!r} for snap {snap!r} and channel {channel!r}")
                return entry.version

        raise SnapVersionNotFoundException(
            f"No released revision found for snap {snap!r} and channel {channel!r}. "
            "Check that the channel name is correct (e.g. '3/stable', '4.0/edge')."
        )
