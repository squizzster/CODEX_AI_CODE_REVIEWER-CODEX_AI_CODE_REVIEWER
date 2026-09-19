from __future__ import annotations

from pathlib import Path

from codex_ai_code_reviewer.live_events import (
    SOURCE_CODE_LINK_NAME,
    WORKSPACE_DIRECTORY_NAMES,
    create_runner_work_space,
    create_runner_work_space_from_event,
)


def _workspace_event(workspace: Path) -> dict[str, object]:
    return {
        "schema": "codex-prompt-runner.live-event/v1",
        "event": "heartbeat",
        "phase": "executing",
        "isolated_workspace": str(workspace),
    }


def test_create_runner_work_space_creates_requested_contents(tmp_path: Path) -> None:
    workspace = tmp_path / "attempt-001" / "isolated-workspace"
    source_code = tmp_path / "source-code"
    source_code.mkdir()

    result = create_runner_work_space(
        workspace, source_code, "Performance specialist context"
    )

    assert result == 1
    assert sorted(path.name for path in workspace.iterdir()) == sorted(
        (*WORKSPACE_DIRECTORY_NAMES, SOURCE_CODE_LINK_NAME, "README.md")
    )
    for directory_name in WORKSPACE_DIRECTORY_NAMES:
        assert (workspace / directory_name).is_dir()
        assert list((workspace / directory_name).iterdir()) == []
    assert (
        workspace / "README.md"
    ).read_text(encoding="utf-8") == "Performance specialist context\n"
    source_code_link = workspace / SOURCE_CODE_LINK_NAME
    assert source_code_link.is_symlink()
    assert source_code_link.resolve() == source_code.resolve()


def test_create_runner_work_space_returns_zero_when_creation_fails(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "isolated-workspace"
    workspace.write_text("not a directory", encoding="utf-8")
    source_code = tmp_path / "source-code"
    source_code.mkdir()

    assert create_runner_work_space(workspace, source_code, "Pipeline") == 0


def test_matching_event_creates_workspace_immediately(tmp_path: Path) -> None:
    workspace = tmp_path / "attempt-001" / "isolated-workspace"
    source_code = tmp_path / "source-code"
    source_code.mkdir()

    create_runner_work_space_from_event(
        {"ANALYZE_PIPELINE": "Pipeline"},
        source_code,
        "ANALYZE_PIPELINE",
        _workspace_event(workspace),
    )

    assert workspace.is_dir()
    assert (workspace / "README.md").read_text(encoding="utf-8") == "Pipeline\n"
    assert (workspace / SOURCE_CODE_LINK_NAME).resolve() == source_code.resolve()


def test_nonmatching_event_does_nothing(tmp_path: Path) -> None:
    create_runner_work_space_from_event(
        {"ANALYZE_PIPELINE": "Pipeline"},
        tmp_path,
        "ANALYZE_PIPELINE",
        {
            "schema": "codex-prompt-runner.live-event/v1",
            "event": "heartbeat",
            "phase": "finished",
            "delivery_mode": "CACHED",
        },
    )

    assert list(tmp_path.iterdir()) == []
