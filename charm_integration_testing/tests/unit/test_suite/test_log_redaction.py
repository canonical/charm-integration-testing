# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import tarfile
from pathlib import Path

import pytest
from test_suite.log_redaction import prepare_redacted_scan_dir, redact_known_false_positives

_CONTROLLER_KEY_PEM = "controllerkey: |\n  totally-fake-controller-key-body-line\n"
_CA_PRIVATE_KEY_PEM = "caprivatekey: |\n  totally-fake-ca-private-key-body-line\n"
_CA_CERT_PEM = "ca-cert: |\n  totally-fake-public-certificate-body-line\n"


class TestRedactKnownFalsePositives:
    _CONFIGMAP_FILE = "describe-controller-configmap.txt"

    def test_redacts_controllerkey_and_caprivatekey_pem_blocks(self) -> None:
        # GIVEN describe-configmap-style text with both known Juju bootstrap key fields
        text = f"bootstrap-params:\n{_CONTROLLER_KEY_PEM}{_CA_PRIVATE_KEY_PEM}"

        # WHEN redacting
        redacted = redact_known_false_positives(self._CONFIGMAP_FILE, text)

        # THEN both PEM bodies are gone, replaced with a placeholder on the field line
        assert "totally-fake-controller-key-body-line" not in redacted
        assert "totally-fake-ca-private-key-body-line" not in redacted
        assert "controllerkey: [REDACTED" in redacted
        assert "caprivatekey: [REDACTED" in redacted

    def test_leaves_unrelated_fields_and_public_cert_untouched(self) -> None:
        # GIVEN text with the two sensitive fields alongside a public CA cert and other content
        text = f"bootstrap-params:\ncontroller-config:\n  {_CA_CERT_PEM}{_CONTROLLER_KEY_PEM}other-field: value\n"

        # WHEN redacting
        redacted = redact_known_false_positives(self._CONFIGMAP_FILE, text)

        # THEN the public certificate and unrelated fields are unchanged
        assert "totally-fake-public-certificate-body-line" in redacted
        assert "other-field: value" in redacted
        # AND only the private key field was touched
        assert "controllerkey: [REDACTED" in redacted

    def test_no_matching_fields_is_a_no_op(self) -> None:
        # GIVEN text with no known bootstrap key fields
        text = "some-field: value\nanother-field: |\n  plain multi-line\n  block scalar\n"

        # WHEN redacting
        redacted = redact_known_false_positives(self._CONFIGMAP_FILE, text)

        # THEN nothing changes
        assert redacted == text

    def test_same_field_name_in_unrelated_file_is_preserved(self) -> None:
        # GIVEN a real secret using the same field name, but in a file/context that
        # isn't the known Juju configmap dump (e.g. an application log or config)
        text = f"bootstrap-params:\n{_CONTROLLER_KEY_PEM}"

        # WHEN redacting content from an unrelated file
        redacted = redact_known_false_positives("app.log", text)

        # THEN the rule doesn't fire: the field name alone isn't enough to redact it
        assert redacted == text

    def test_field_name_without_bootstrap_params_context_is_preserved(self) -> None:
        # GIVEN the right file name, but content that doesn't have the bootstrap-params
        # structure this field is known to appear in (e.g. a real secret happens to
        # reuse the field name elsewhere in the same file)
        text = _CONTROLLER_KEY_PEM

        # WHEN redacting
        redacted = redact_known_false_positives(self._CONFIGMAP_FILE, text)

        # THEN the rule doesn't fire: field name alone, without the known structure,
        # isn't enough to redact it
        assert redacted == text

    def test_plain_scalar_value_is_preserved(self) -> None:
        # GIVEN the right file/context, but the field is a plain scalar, not a
        # block scalar (e.g. a real secret assigned directly, with no "|")
        text = "bootstrap-params:\ncontrollerkey: totally-real-secret-value\n"

        # WHEN redacting
        redacted = redact_known_false_positives(self._CONFIGMAP_FILE, text)

        # THEN the rule doesn't fire: only the "|" block-scalar form is redacted
        assert redacted == text


class TestPrepareRedactedScanDir:
    def test_copies_plain_files_and_redacts_matching_content(self, tmp_path: Path) -> None:
        # GIVEN a log_dir with a plain text file containing a bootstrap key field
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "describe-controller-configmap.txt").write_text(f"bootstrap-params:\n{_CONTROLLER_KEY_PEM}")
        scan_dir = tmp_path / "scan"

        # WHEN preparing the redacted scan dir
        prepare_redacted_scan_dir(log_dir, scan_dir)

        # THEN the copy exists with the key redacted
        copied = (scan_dir / "describe-controller-configmap.txt").read_text()
        assert "totally-fake-controller-key-body-line" not in copied
        assert "controllerkey: [REDACTED" in copied

    def test_extracts_and_redacts_tar_gz_archives(self, tmp_path: Path) -> None:
        # GIVEN a log_dir with a crashdump-style tar.gz containing the sensitive fields
        # alongside an unrelated real-looking secret
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        source = tmp_path / "source"
        source.mkdir()
        configmap_file = source / "describe-controller-configmap.txt"
        configmap_file.write_text(f"bootstrap-params:\n{_CONTROLLER_KEY_PEM}{_CA_PRIVATE_KEY_PEM}")
        other_file = source / "unit-app-0.log"
        other_file.write_text("some-other-real-looking-secret=abc123")

        archive_path = log_dir / "juju-controller-charmqa.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(configmap_file, arcname="controller-charmqa/configmap/describe-controller-configmap.txt")
            archive.add(other_file, arcname="controller-charmqa/unit-app-0.log")

        scan_dir = tmp_path / "scan"

        # WHEN preparing the redacted scan dir
        prepare_redacted_scan_dir(log_dir, scan_dir)

        # THEN the archive is extracted (not left as an opaque tarball) with the two known
        # fields redacted...
        extracted_configmap = scan_dir / "juju-controller-charmqa.tar.gz.extracted"
        configmap_text = (
            extracted_configmap / "controller-charmqa" / "configmap" / "describe-controller-configmap.txt"
        ).read_text()
        assert "totally-fake-controller-key-body-line" not in configmap_text
        assert "totally-fake-ca-private-key-body-line" not in configmap_text
        assert "controllerkey: [REDACTED" in configmap_text
        assert "caprivatekey: [REDACTED" in configmap_text

        # ...while unrelated content in the same archive is left intact for scanning
        other_text = (extracted_configmap / "controller-charmqa" / "unit-app-0.log").read_text()
        assert other_text == "some-other-real-looking-secret=abc123"

    def test_preserves_same_field_name_in_an_unrelated_file(self, tmp_path: Path) -> None:
        # GIVEN a log_dir with a real secret that happens to reuse the "controllerkey"
        # field name, but in a file that isn't the known Juju configmap dump
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "app.log").write_text(f"bootstrap-params:\n{_CONTROLLER_KEY_PEM}")
        scan_dir = tmp_path / "scan"

        # WHEN preparing the redacted scan dir
        prepare_redacted_scan_dir(log_dir, scan_dir)

        # THEN the field is left untouched: the rule is scoped to the known file/structure
        copied = (scan_dir / "app.log").read_text()
        assert "totally-fake-controller-key-body-line" in copied
        assert "[REDACTED" not in copied

    def test_rejects_archive_members_that_escape_destination(self, tmp_path: Path) -> None:
        # GIVEN a maliciously-crafted tar.gz whose member path escapes the extraction dir
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        evil_file = tmp_path / "evil.txt"
        evil_file.write_text("payload")
        archive_path = log_dir / "evil.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(evil_file, arcname="../../escaped.txt")

        scan_dir = tmp_path / "scan"

        # WHEN/THEN preparing the scan dir refuses to extract the unsafe member
        with pytest.raises(ValueError, match="Refusing to extract"):
            prepare_redacted_scan_dir(log_dir, scan_dir)

    def test_rejects_symlink_archive_members(self, tmp_path: Path) -> None:
        # GIVEN a tar.gz with a symlink member whose in-bounds name hides an
        # out-of-bounds target (e.g. pointing outside the extraction dir)
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        archive_path = log_dir / "evil.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            link_member = tarfile.TarInfo(name="link")
            link_member.type = tarfile.SYMTYPE
            link_member.linkname = "../../etc/passwd"
            archive.addfile(link_member)

        scan_dir = tmp_path / "scan"

        # WHEN/THEN preparing the scan dir refuses to extract the symlink
        with pytest.raises(ValueError, match="Refusing to extract link"):
            prepare_redacted_scan_dir(log_dir, scan_dir)

    def test_rejects_hardlink_archive_members(self, tmp_path: Path) -> None:
        # GIVEN a tar.gz with a hardlink member pointing at a file outside the archive
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        archive_path = log_dir / "evil.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            link_member = tarfile.TarInfo(name="link")
            link_member.type = tarfile.LNKTYPE
            link_member.linkname = "/etc/passwd"
            archive.addfile(link_member)

        scan_dir = tmp_path / "scan"

        # WHEN/THEN preparing the scan dir refuses to extract the hardlink
        with pytest.raises(ValueError, match="Refusing to extract link"):
            prepare_redacted_scan_dir(log_dir, scan_dir)

    def test_rejects_special_archive_members(self, tmp_path: Path) -> None:
        # GIVEN a tar.gz with a device-file member (not a plain file or directory)
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        archive_path = log_dir / "evil.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            device_member = tarfile.TarInfo(name="device")
            device_member.type = tarfile.CHRTYPE
            archive.addfile(device_member)

        scan_dir = tmp_path / "scan"

        # WHEN/THEN preparing the scan dir refuses to extract the special member
        with pytest.raises(ValueError, match="Refusing to extract special"):
            prepare_redacted_scan_dir(log_dir, scan_dir)
