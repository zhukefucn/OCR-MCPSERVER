from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest


def _publication_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "staging" / "document"
    target = tmp_path / "published" / "document"
    source.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    source_marker = source / "source.txt"
    source_marker.write_text("source", encoding="utf-8")
    return source, target, source_marker


def test_publication_boundary_never_replaces_new_empty_destination(
    tmp_path: Path,
) -> None:
    from ocr_mcp_server.infra.mineru_archive import publish_directory_no_replace

    source, target, source_marker = _publication_fixture(tmp_path)
    target.mkdir()  # Simulates a destination created exactly at publication time.
    target_identity = os.stat(target, follow_symlinks=False)

    with pytest.raises(FileExistsError):
        publish_directory_no_replace(source, target)

    assert source_marker.read_text(encoding="utf-8") == "source"
    assert os.path.samestat(
        target_identity, os.stat(target, follow_symlinks=False)
    )
    assert list(target.iterdir()) == []


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux renameat2 native contract"
)
def test_linux_native_directory_publication_uses_rename_noreplace(
    tmp_path: Path,
) -> None:
    from ocr_mcp_server.infra.mineru_archive import publish_directory_no_replace

    source, target, source_marker = _publication_fixture(tmp_path)
    target.mkdir()
    target_identity = os.stat(target, follow_symlinks=False)

    with pytest.raises(FileExistsError):
        publish_directory_no_replace(source, target)

    assert source_marker.is_file()
    assert os.path.samestat(
        target_identity, os.stat(target, follow_symlinks=False)
    )
