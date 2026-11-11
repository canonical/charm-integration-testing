# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import stat
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile


def unpack_charm_artifact(charm_file: Path, destination: Path) -> Path:
    """Unpack a Charmcraft .charm archive to a Juju-deployable charm directory."""
    charm_file = charm_file.resolve()
    destination = destination.resolve()
    if not charm_file.is_file():
        raise ValueError(f"Charm artifact does not exist or is not a file: {charm_file}")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"Charm artifact destination is not empty: {destination}")

    destination.mkdir(parents=True, exist_ok=True)
    try:
        with ZipFile(charm_file) as archive:
            for entry in archive.infolist():
                member_path = PurePosixPath(entry.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ValueError(f"Charm artifact contains an unsafe path: {entry.filename!r}")
                if stat.S_ISLNK(entry.external_attr >> 16):
                    raise ValueError(f"Charm artifact contains a symbolic link: {entry.filename!r}")

                output_path = (destination / Path(*member_path.parts)).resolve()
                if not output_path.is_relative_to(destination):
                    raise ValueError(f"Charm artifact contains an unsafe path: {entry.filename!r}")
                if entry.is_dir():
                    output_path.mkdir(parents=True, exist_ok=True)
                    continue

                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(archive.read(entry))
                permissions = stat.S_IMODE(entry.external_attr >> 16)
                if permissions:
                    output_path.chmod(permissions)
    except BadZipFile as exc:
        raise ValueError(f"Invalid charm artifact archive: {charm_file}") from exc

    if not (destination / "metadata.yaml").is_file():
        raise ValueError(f"Charm artifact does not contain metadata.yaml: {charm_file}")
    return destination
