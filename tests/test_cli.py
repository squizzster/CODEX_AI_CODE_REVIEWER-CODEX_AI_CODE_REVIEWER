from __future__ import annotations

import os
from pathlib import Path

import pytest

from codex_ai_code_reviewer.cli import _review_directory
from codex_ai_code_reviewer.initialization import InitializationError


def test_review_directory_resolves_an_existing_readable_directory(
    tmp_path: Path,
) -> None:
    assert _review_directory(tmp_path) == tmp_path.resolve()


def test_review_directory_must_exist(tmp_path: Path) -> None:
    with pytest.raises(InitializationError, match="does not exist"):
        _review_directory(tmp_path / "missing")


def test_review_target_must_be_a_directory(tmp_path: Path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("content", encoding="utf-8")

    with pytest.raises(InitializationError, match="not a directory"):
        _review_directory(file_path)


def test_review_directory_requires_read_and_traverse_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "access", lambda *args, **kwargs: False)

    with pytest.raises(InitializationError, match="not readable and traversable"):
        _review_directory(tmp_path)
