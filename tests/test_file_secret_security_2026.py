"""Generic file-secret reader must refuse path substitution and large/unusual files."""
import os
import pytest

from agent_playbook_os.errors import SecretResolutionError
from agent_playbook_os.secret_resolver import FileSecretResolver, SecretValue


@pytest.mark.skipif(os.name != "posix", reason="POSIX secure dir_fd reader")
def test_file_secret_reads_regular_file_and_returns_redaction_marker(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "KEY").write_text("real-value\n", encoding="utf-8")
    resolver = FileSecretResolver([root])
    value = resolver.resolve("file-secret://sub/KEY")
    assert isinstance(value, SecretValue)
    assert value == "real-value"
    assert value.source_ref == "file-secret://sub/KEY"


@pytest.mark.skipif(os.name != "posix", reason="POSIX secure dir_fd reader")
def test_file_secret_rejects_outside_and_nonregular_files(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("do-not-read", encoding="utf-8")
    (root / "escape").symlink_to(outside)
    resolver = FileSecretResolver([root])
    with pytest.raises(SecretResolutionError):
        resolver.resolve("file-secret://escape")
    with pytest.raises(SecretResolutionError):
        resolver.resolve("file-secret://../outside")
    (root / "directory").mkdir()
    with pytest.raises(SecretResolutionError, match="regular file"):
        resolver.resolve("file-secret://directory")


@pytest.mark.skipif(os.name != "posix", reason="POSIX secure dir_fd reader")
def test_file_secret_size_limit_and_invalid_utf8(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "oversized").write_bytes(b"123456")
    resolver = FileSecretResolver([root], max_bytes=4)
    with pytest.raises(SecretResolutionError, match="max_bytes"):
        resolver.resolve("file-secret://oversized")
    (root / "binary").write_bytes(b"\xff\xfe")
    with pytest.raises(SecretResolutionError, match="UTF-8"):
        resolver.resolve("file-secret://binary")


@pytest.mark.parametrize("bad", [0, -1, 1.5, True])
def test_file_secret_rejects_invalid_max_bytes(tmp_path, bad):
    with pytest.raises(ValueError, match="max_bytes"):
        FileSecretResolver([tmp_path], max_bytes=bad)
