from __future__ import annotations

import stat
import threading
import time
from pathlib import Path

import pytest

from codex_ai_code_reviewer.initialization import InitializationError
from codex_ai_code_reviewer.live_events import (
    WORKSPACE_DIRECTORY_NAMES,
    ReviewWorkspaceInitializer,
)


def _workspace_event(state_root: Path, *, attempt: int = 1) -> dict[str, object]:
    execution_run_id = "45d33ddfcb7be901f9ad18db8b760a58"
    artifacts_root = state_root / "artifacts-v8"
    run_directory = artifacts_root / f"run-{execution_run_id}"
    attempt_directory = run_directory / f"attempt-{attempt:03d}"
    return {
        "schema": "codex-prompt-runner.live-event/v1",
        "event": "heartbeat",
        "phase": "executing",
        "state_root": str(state_root),
        "artifact_generation": "artifacts-v8",
        "artifacts_root": str(artifacts_root),
        "run_directory": str(run_directory),
        "attempt_directory": str(attempt_directory),
        "isolated_workspace": str(attempt_directory / "isolated-workspace"),
        "execution_run_id": execution_run_id,
        "attempt": attempt,
    }


def _create_runner_workspace(event: dict[str, object]) -> None:
    workspace = Path(str(event["isolated_workspace"]))
    time.sleep(0.02)
    workspace.mkdir(mode=0o700, parents=True)


def test_initial_execution_event_populates_workspace_after_runner_creation(
    tmp_path: Path,
) -> None:
    event = _workspace_event(tmp_path / "state")
    creator = threading.Thread(target=_create_runner_workspace, args=(event,))
    creator.start()
    initializer = ReviewWorkspaceInitializer(
        {"ANALYZE_PERFORMANCE": "Performance specialist context"}
    )

    initializer("ANALYZE_PERFORMANCE", event)
    creator.join()

    workspace = Path(str(event["isolated_workspace"]))
    assert sorted(path.name for path in workspace.iterdir()) == sorted(
        (*WORKSPACE_DIRECTORY_NAMES, "README.md")
    )
    for directory_name in WORKSPACE_DIRECTORY_NAMES:
        directory = workspace / directory_name
        assert directory.is_dir()
        assert list(directory.iterdir()) == []
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    readme = workspace / "README.md"
    assert readme.read_text(encoding="utf-8") == "Performance specialist context\n"
    assert stat.S_IMODE(readme.stat().st_mode) == 0o600


def test_retry_event_populates_its_fresh_attempt_workspace(tmp_path: Path) -> None:
    events = [_workspace_event(tmp_path / "state", attempt=value) for value in (1, 2)]
    initializer = ReviewWorkspaceInitializer({"ANALYZE_PIPELINE": "Pipeline"})

    for event in events:
        workspace = Path(str(event["isolated_workspace"]))
        workspace.mkdir(mode=0o700, parents=True)
        initializer("ANALYZE_PIPELINE", event)

    assert all(
        (Path(str(event["isolated_workspace"])) / "README.md").is_file()
        for event in events
    )


def test_cached_finished_event_creates_no_workspace(tmp_path: Path) -> None:
    initializer = ReviewWorkspaceInitializer({"ANALYZE_PIPELINE": "Pipeline"})

    initializer(
        "ANALYZE_PIPELINE",
        {
            "schema": "codex-prompt-runner.live-event/v1",
            "event": "heartbeat",
            "phase": "finished",
            "delivery_mode": "CACHED",
        },
    )

    assert list(tmp_path.iterdir()) == []


def test_inconsistent_workspace_event_is_rejected(tmp_path: Path) -> None:
    event = _workspace_event(tmp_path / "state")
    event["isolated_workspace"] = str(tmp_path / "wrong")
    initializer = ReviewWorkspaceInitializer({"ANALYZE_SECURITY": "Security"})

    with pytest.raises(InitializationError, match="inconsistent paths"):
        initializer("ANALYZE_SECURITY", event)


def test_workspace_initialization_does_not_overwrite_readme(tmp_path: Path) -> None:
    event = _workspace_event(tmp_path / "state")
    workspace = Path(str(event["isolated_workspace"]))
    workspace.mkdir(mode=0o700, parents=True)
    (workspace / "README.md").write_text("agent-owned", encoding="utf-8")
    initializer = ReviewWorkspaceInitializer({"ANALYZE_INTEGRITY": "Integrity"})

    with pytest.raises(InitializationError, match="Cannot initialize"):
        initializer("ANALYZE_INTEGRITY", event)

    assert (workspace / "README.md").read_text(encoding="utf-8") == "agent-owned"
