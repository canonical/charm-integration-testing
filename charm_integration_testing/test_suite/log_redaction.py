# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import re
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class RedactionRule:
    """A pattern matching one known false-positive secret, scoped to where it occurs.

    A rule only fires on paths whose relative path (including any parent
    directories, e.g. an archive member path) matches ``file_name_pattern``, and
    only within the body captured by ``section_pattern`` (group 1), so a field
    name never triggers a redaction outside the exact location it's known to
    come from.
    """

    file_name_pattern: re.Pattern[str]
    section_pattern: re.Pattern[str]
    field_pattern: re.Pattern[str]
    replace: Callable[[re.Match[str]], str]

    def apply(self, relative_path: str, text: str) -> str:
        if not self.file_name_pattern.search(relative_path):
            return text

        def _redact_section(section_match: re.Match[str]) -> str:
            header = section_match.group(0)[: section_match.start(1) - section_match.start(0)]
            return header + self.field_pattern.sub(self.replace, section_match.group(1))

        return self.section_pattern.sub(_redact_section, text)


def _yaml_block_scalar_pattern(field_names: tuple[str, ...]) -> re.Pattern[str]:
    """Match a top-level YAML block-scalar field's header and indented body.

    Matches ``<field>: |`` (optionally with chomping/indentation indicators, e.g.
    ``|-``, ``|2``) followed by its indented lines, e.g. a PEM block. A plain scalar
    value like ``<field>: some-value`` is left untouched.
    """
    return re.compile(
        rf"^([ \t]*)({'|'.join(field_names)}):[ \t]*\|(?:[+-]\d?|\d[+-]?)?[ \t]*\n(?:\1[ \t]+\S.*\n?)*",
        re.MULTILINE,
    )


def _describe_configmap_section(section_name: str) -> re.Pattern[str]:
    """Match a kubectl ``describe configmap`` Data section's raw body.

    kubectl renders each Data key as ``<key>:\\n----\\n<raw value>``, with the next
    key's own ``----`` header marking where the section ends. Capturing only the
    body (group 1) scopes redaction to fields inside this specific section.
    """
    return re.compile(
        rf"^{re.escape(section_name)}:\n----\n(.*?)(?=^[\w./-]+:\n----\n|\Z)",
        re.MULTILINE | re.DOTALL,
    )


def _configmap_section_key_rule(section_name: str, field_names: tuple[str, ...], reason: str) -> RedactionRule:
    """Build a rule redacting ``field_names`` only inside a named Data section of a
    ``kubectl describe configmap`` dump.
    """

    def _replace(match: re.Match[str]) -> str:
        indent, field = match.group(1), match.group(2)
        return f"{indent}{field}: [REDACTED - {reason}]\n"

    return RedactionRule(
        file_name_pattern=re.compile(r"(?:^|/)configmap/describe-controller-configmap\.txt$"),
        section_pattern=_describe_configmap_section(section_name),
        field_pattern=_yaml_block_scalar_pattern(field_names),
        replace=_replace,
    )


# Known false positives to redact before secret scanning. Each rule is scoped to a
# specific file and section, so a same-named field elsewhere is untouched.
_REDACTION_RULES: list[RedactionRule] = [
    # Juju's controller agent.conf (embedded verbatim in the configmap describe
    # dump) carries the controller's own ephemeral TLS key material; it's
    # regenerated per bootstrap and not a real secret. See issue #1033.
    _configmap_section_key_rule(
        "controller-agent.conf",
        ("controllerkey", "caprivatekey"),
        reason="ephemeral Juju bootstrap key, see issue #1033",
    ),
]


def redact_known_false_positives(relative_path: str, text: str) -> str:
    """Redact known false-positive secrets from ``text``, scoped by ``relative_path``.

    ``relative_path`` should include any parent directories (e.g. an archive
    member path like ``controller-charmqa/configmap/describe-controller-configmap.txt``),
    since rules match on the full path, not just the file's basename. Only content
    matching a rule's path and section is touched; everything else, including a
    real secret under the same field name elsewhere, is left intact.
    """
    for rule in _REDACTION_RULES:
        text = rule.apply(relative_path, text)
    return text


def _redact_file_in_place(path: Path, root: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, ValueError):
        return  # binary content; nothing to redact
    relative_path = path.relative_to(root).as_posix()
    redacted = redact_known_false_positives(relative_path, text)
    if redacted != text:
        path.write_text(redacted, encoding="utf-8")


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    """Extract ``archive`` into ``destination``, rejecting members that could escape it.

    Validates paths and rejects links/special members ourselves, since Python's
    built-in ``filter="data"`` protection only exists from 3.12+ and this repo
    supports 3.10+.
    """
    destination = destination.resolve()
    for member in archive.getmembers():
        if member.issym() or member.islnk():
            raise ValueError(f"Refusing to extract link archive member: {member.name!r}")
        if not (member.isfile() or member.isdir()):
            raise ValueError(f"Refusing to extract special archive member: {member.name!r}")
        member_path = (destination / member.name).resolve()
        if member_path != destination and destination not in member_path.parents:
            raise ValueError(f"Refusing to extract unsafe archive member path: {member.name!r}")
    if hasattr(tarfile, "data_filter"):
        # `filter` needs Python 3.12+; our own checks above cover 3.10/3.11.
        archive.extractall(destination, filter="data")  # nosec B202 - members validated above
    else:
        archive.extractall(destination)  # nosec B202 - members validated above


def _copy_tree(source: Path, destination: Path) -> None:
    """Copy ``source`` into ``destination``, skipping any symlink entries.

    Mirrors ``_safe_extract``'s policy: a symlink could point outside ``source``, so
    following it would pull unrelated content into the scan tree.
    """
    destination.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        if entry.is_symlink():
            continue
        target = destination / entry.name
        if entry.is_dir():
            _copy_tree(entry, target)
        elif entry.is_file():
            shutil.copy2(entry, target)


def prepare_redacted_scan_dir(log_dir: Path, scan_dir: Path) -> None:
    """Populate ``scan_dir`` with a redacted copy of ``log_dir`` for secret scanning.

    Plain files are copied as-is; ``.tar``/``.tar.gz`` archives are extracted (not
    re-compressed) so their members can be redacted individually. See
    :func:`redact_known_false_positives` for what gets redacted.
    """
    scan_dir.mkdir(parents=True, exist_ok=True)
    for entry in log_dir.iterdir():
        if entry.is_symlink():
            # A symlink could point outside log_dir; skip it rather than follow it.
            continue
        if entry.is_file() and tarfile.is_tarfile(entry):
            extract_dir = scan_dir / f"{entry.name}.extracted"
            with tarfile.open(entry) as archive:
                _safe_extract(archive, extract_dir)
            for member_path in extract_dir.rglob("*"):
                if member_path.is_file():
                    _redact_file_in_place(member_path, root=extract_dir)
        elif entry.is_file():
            destination = scan_dir / entry.name
            shutil.copy2(entry, destination)
            _redact_file_in_place(destination, root=scan_dir)
        elif entry.is_dir():
            destination = scan_dir / entry.name
            _copy_tree(entry, destination)
            for member_path in destination.rglob("*"):
                if member_path.is_file():
                    _redact_file_in_place(member_path, root=scan_dir)
