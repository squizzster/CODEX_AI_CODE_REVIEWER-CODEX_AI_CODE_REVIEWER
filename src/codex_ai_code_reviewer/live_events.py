"""Review-specific handling for Prompt Runner live events."""

from __future__ import annotations

import os
import re
import stat
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from codex_ai_code_reviewer.initialization import InitializationError

LIVE_EVENT_SCHEMA = "codex-prompt-runner.live-event/v1"
WORKSPACE_DIRECTORY_NAMES = (
    "final_reports",
    "tmp",
    "temp_scripts",
    "scratch_pad",
)
WORKSPACE_WAIT_SECONDS = 5.0
WORKSPACE_POLL_SECONDS = 0.005
_EXECUTION_RUN_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


class ReviewWorkspaceInitializer:
    """Populate each stock Prompt Runner workspace from its first live event."""

    def __init__(self, readme_by_prompt: Mapping[str, str]) -> None:
        self._readme_by_prompt = dict(readme_by_prompt)
        self._initialized: set[Path] = set()
        self._initializing: set[Path] = set()
        self._lock = threading.Lock()

    def __call__(self, prompt_name: str, event: dict[str, Any]) -> None:
        if not _is_workspace_event(event):
            return
        workspace = _validated_workspace_path(event)
        readme = self._readme_by_prompt.get(prompt_name)
        if readme is None:
            raise InitializationError(
                f"No isolated-workspace README is configured for {prompt_name}"
            )
        with self._lock:
            if workspace in self._initialized or workspace in self._initializing:
                return
            self._initializing.add(workspace)
        try:
            _wait_for_runner_workspace(workspace)
            _populate_workspace(workspace, readme)
        except BaseException:
            with self._lock:
                self._initializing.discard(workspace)
            raise
        else:
            with self._lock:
                self._initializing.remove(workspace)
                self._initialized.add(workspace)


def _is_workspace_event(event: dict[str, Any]) -> bool:
    return (
        event.get("schema") == LIVE_EVENT_SCHEMA
        and event.get("event") == "heartbeat"
        and event.get("phase") == "executing"
        and "isolated_workspace" in event
    )


def _absolute_event_path(event: dict[str, Any], field: str) -> Path:
    value = event.get(field)
    if not isinstance(value, str) or not value:
        raise InitializationError(
            f"Prompt Runner workspace event has invalid {field}"
        )
    path = Path(os.path.normpath(value))
    if not path.is_absolute():
        raise InitializationError(
            f"Prompt Runner workspace event has non-absolute {field}"
        )
    return path


def _validated_workspace_path(event: dict[str, Any]) -> Path:
    attempt = event.get("attempt")
    execution_run_id = event.get("execution_run_id")
    artifact_generation = event.get("artifact_generation")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise InitializationError("Prompt Runner workspace event has invalid attempt")
    if (
        not isinstance(execution_run_id, str)
        or _EXECUTION_RUN_ID_PATTERN.fullmatch(execution_run_id) is None
    ):
        raise InitializationError(
            "Prompt Runner workspace event has invalid execution_run_id"
        )
    if (
        not isinstance(artifact_generation, str)
        or not artifact_generation
        or Path(artifact_generation).name != artifact_generation
    ):
        raise InitializationError(
            "Prompt Runner workspace event has invalid artifact_generation"
        )

    state_root = _absolute_event_path(event, "state_root")
    artifacts_root = _absolute_event_path(event, "artifacts_root")
    run_directory = _absolute_event_path(event, "run_directory")
    attempt_directory = _absolute_event_path(event, "attempt_directory")
    workspace = _absolute_event_path(event, "isolated_workspace")
    expected_paths = {
        "artifacts_root": state_root / artifact_generation,
        "run_directory": artifacts_root / f"run-{execution_run_id}",
        "attempt_directory": run_directory / f"attempt-{attempt:03d}",
        "isolated_workspace": attempt_directory / "isolated-workspace",
    }
    observed_paths = {
        "artifacts_root": artifacts_root,
        "run_directory": run_directory,
        "attempt_directory": attempt_directory,
        "isolated_workspace": workspace,
    }
    mismatches = [
        field
        for field, expected in expected_paths.items()
        if observed_paths[field] != expected
    ]
    if mismatches:
        raise InitializationError(
            "Prompt Runner workspace event has inconsistent paths: "
            + ", ".join(mismatches)
        )
    return workspace


def _wait_for_runner_workspace(workspace: Path) -> None:
    # TODO: Replace this bounded startup-window wait with an acknowledged
    # Prompt Runner workspace-ready handshake before Codex is launched.
    deadline = time.monotonic() + WORKSPACE_WAIT_SECONDS
    while True:
        try:
            metadata = workspace.lstat()
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                raise InitializationError(
                    f"Prompt Runner did not create isolated workspace {workspace}"
                ) from None
            time.sleep(WORKSPACE_POLL_SECONDS)
            continue
        except OSError as error:
            raise InitializationError(
                f"Cannot inspect Prompt Runner isolated workspace {workspace}: {error}"
            ) from error
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise InitializationError(
                f"Prompt Runner isolated workspace is not a real directory: {workspace}"
            )
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise InitializationError(
                f"Prompt Runner isolated workspace is not private and process-owned: "
                f"{workspace}"
            )
        return


def _populate_workspace(workspace: Path, readme: str) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        workspace_descriptor = os.open(workspace, flags)
    except OSError as error:
        raise InitializationError(
            f"Cannot open Prompt Runner isolated workspace {workspace}: {error}"
        ) from error
    try:
        for directory_name in WORKSPACE_DIRECTORY_NAMES:
            os.mkdir(directory_name, mode=0o700, dir_fd=workspace_descriptor)
        readme_descriptor = os.open(
            "README.md",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=workspace_descriptor,
        )
        with os.fdopen(readme_descriptor, "w", encoding="utf-8") as readme_file:
            readme_file.write(readme)
            if not readme.endswith("\n"):
                readme_file.write("\n")
            readme_file.flush()
            os.fsync(readme_file.fileno())
    except OSError as error:
        raise InitializationError(
            f"Cannot initialize Prompt Runner isolated workspace {workspace}: {error}"
        ) from error
    finally:
        os.close(workspace_descriptor)
