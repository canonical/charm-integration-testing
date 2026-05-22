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
from datetime import timedelta
from functools import cache

import requests
from pydantic import BaseModel, ConfigDict, Field
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class SnapVersionNotFoundException(Exception):
    """Raised when a version for the requested snap channel cannot be found."""

    pass


class SnapChannel(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    track: str
    risk: str


class SnapChannelMapEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    channel: SnapChannel
    version: str


class SnapInfoResponse(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    channel_map: list[SnapChannelMapEntry] = Field(default_factory=list, alias="channel-map")


_DEFAULT_SNAPCRAFT_API_URL = "https://api.snapcraft.io"


class SnapstoreHttpClient:
    session: requests.Session
    logger: logging.Logger
    timeout: timedelta
    _info_endpoint: str

    def __init__(
        self,
        logger: logging.Logger = logging.getLogger(__name__),
        session: requests.Session | None = None,
        timeout: timedelta = timedelta(seconds=30),
        base_url: str = _DEFAULT_SNAPCRAFT_API_URL,
    ) -> None:
        self.logger = logger
        self.timeout = timeout
        self._info_endpoint = base_url.rstrip("/") + "/v2/snaps/info/{snap_name}"

        retry_strategy = Retry(
            total=10,
            status_forcelist=[408, 429, 500, 502, 503, 504],
            allowed_methods=Retry.DEFAULT_ALLOWED_METHODS | {"GET", "POST"},
            backoff_factor=0.5,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session = session if session is not None else requests.Session()
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    @cache
    def snap_info(self, snap_name: str) -> SnapInfoResponse:
        self.logger.debug(f"Calling snap info for snap {snap_name}")

        request_url = self._info_endpoint.format(snap_name=snap_name)
        request_headers = {"Snap-Device-Series": "16"}
        response = self.session.get(url=request_url, headers=request_headers, timeout=self.timeout.total_seconds())
        response.raise_for_status()
        return SnapInfoResponse.model_validate(response.json())
