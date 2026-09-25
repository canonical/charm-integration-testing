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

    A rule only fires on files whose name matches ``file_name_pattern`` and whose
    content matches ``required_context``, so a field name alone never triggers a
    redaction outside the specific file/structure it's known to come from.
    """

    file_name_pattern: re.Pattern[str]
    required_context: re.Pattern[str]
    pattern: re.Pattern[str]
    replace: Callable[[re.Match[str]], str]

    def apply(self, file_name: str, text: str) -> str:
        if not (self.file_name_pattern.search(file_name) and self.required_context.search(text)):
            return text
        return self.pattern.sub(self.replace, text)


def _yaml_block_scalar_rule(
    file_name_pattern: str,
    required_context: str,
    field_names: tuple[str, ...],
    reason: str,
) -> RedactionRule:
    """Build a rule that redacts a top-level YAML block-scalar field's body.

    Matches ``<field>: |`` (optionally with chomping/indentation indicators, e.g.
    ``|-``, ``|2``) followed by its indented lines, e.g. a PEM block. A plain scalar
    value like ``<field>: some-value`` is left untouched.
    """
    pattern = re.compile(
        rf"^([ \t]*)({'|'.join(field_names)}):[ \t]*\|(?:[+-]\d?|\d[+-]?)?[ \t]*\n(?:\1[ \t]+\S.*\n?)*",
        re.MULTILINE,
    )

    def _replace(match: re.Match[str]) -> str:
        indent, field = match.group(1), match.group(2)
        return f"{indent}{field}: [REDACTED - {reason}]\n"

    return RedactionRule(
        file_name_pattern=re.compile(file_name_pattern),
        required_context=re.compile(required_context, re.MULTILINE),
        pattern=pattern,
        replace=_replace,
    )


# Known false positives to redact before secret scanning. Each rule is scoped to a
# specific file/structure, so unrelated files with a same-named field are untouched.
_REDACTION_RULES: list[RedactionRule] = [
    # Juju's controller configmap describe output embeds these ephemeral bootstrap
    # keys under a bootstrap-params blob; they're regenerated per bootstrap and not
    # real secrets. See issue #1033.
    _yaml_block_scalar_rule(
        file_name_pattern=r"^describe-controller-configmap\.txt$",
        required_context=r"^bootstrap-params:",
        field_names=("controllerkey", "caprivatekey"),
        reason="ephemeral Juju bootstrap key, see issue #1033",
    ),
]


def redact_known_false_positives(file_name: str, text: str) -> str:
    """Redact known false-positive secrets from ``text``, scoped by ``file_name``.

    Only content matching a rule's file name and structural context is touched;
    everything else, including a real secret under the same field name elsewhere,
    is left intact.
    """
    for rule in _REDACTION_RULES:
        text = rule.apply(file_name, text)
    return text


def _redact_file_in_place(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, ValueError):
        return  # binary content; nothing to redact
    redacted = redact_known_false_positives(path.name, text)
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


def prepare_redacted_scan_dir(log_dir: Path, scan_dir: Path) -> None:
    """Populate ``scan_dir`` with a redacted copy of ``log_dir`` for secret scanning.

    Plain files are copied as-is; ``.tar``/``.tar.gz`` archives are extracted (not
    re-compressed) so their members can be redacted individually. See
    :func:`redact_known_false_positives` for what gets redacted.
    """
    scan_dir.mkdir(parents=True, exist_ok=True)
    for entry in log_dir.iterdir():
        if entry.is_file() and tarfile.is_tarfile(entry):
            extract_dir = scan_dir / f"{entry.name}.extracted"
            with tarfile.open(entry) as archive:
                _safe_extract(archive, extract_dir)
            for member_path in extract_dir.rglob("*"):
                if member_path.is_file():
                    _redact_file_in_place(member_path)
        elif entry.is_file():
            destination = scan_dir / entry.name
            shutil.copy2(entry, destination)
            _redact_file_in_place(destination)
        elif entry.is_dir():
            destination = scan_dir / entry.name
            shutil.copytree(entry, destination)
            for member_path in destination.rglob("*"):
                if member_path.is_file():
                    _redact_file_in_place(member_path)
