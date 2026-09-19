"""Create review workspaces from Prompt Runner live events."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from codex_ai_code_reviewer.initialization import InitializationError

LIVE_EVENT_SCHEMA = "codex-prompt-runner.live-event/v1"
SOURCE_CODE_LINK_NAME = "source_code_read_only_link"
FINAL_REPORTS_LINK_NAME = "final_reports_md"
OUTPUT_DIRECTORY_NAME = "outputs"
WORKSPACE_DIRECTORY_NAMES = (
    OUTPUT_DIRECTORY_NAME,
    "tmp",
    "temp_scripts",
    "scratch_pad",
)


def create_runner_work_space(
    workspace: Path, source_code_directory: Path, readme: str
) -> int:
    """Create one agent workspace, returning 1 on success and 0 on failure."""

    try:
        source_code_directory = source_code_directory.resolve(strict=True)
        if not source_code_directory.is_dir():
            return 0
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        for directory_name in WORKSPACE_DIRECTORY_NAMES:
            (workspace / directory_name).mkdir(mode=0o700, exist_ok=True)
        final_reports_link = workspace / FINAL_REPORTS_LINK_NAME
        final_reports_link.symlink_to(OUTPUT_DIRECTORY_NAME, target_is_directory=True)
        source_code_link = workspace / SOURCE_CODE_LINK_NAME
        source_code_link.symlink_to(source_code_directory, target_is_directory=True)
        (workspace / "README.md").write_text(
            readme if readme.endswith("\n") else f"{readme}\n",
            encoding="utf-8",
        )
        return int(
            workspace.is_dir()
            and all((workspace / name).is_dir() for name in WORKSPACE_DIRECTORY_NAMES)
            and final_reports_link.is_symlink()
            and final_reports_link.resolve()
            == (workspace / OUTPUT_DIRECTORY_NAME).resolve()
            and source_code_link.is_symlink()
            and source_code_link.resolve() == source_code_directory
            and (workspace / "README.md").is_file()
        )
    except OSError:
        return 0


def create_runner_work_space_from_event(
    readme_by_prompt: Mapping[str, str],
    source_code_directory: Path,
    prompt_name: str,
    event: dict[str, Any],
) -> None:
    """Create a workspace when the matching Prompt Runner event arrives."""

    workspace_value = event.get("isolated_workspace")
    if not (
        event.get("schema") == LIVE_EVENT_SCHEMA
        and event.get("event") == "heartbeat"
        and event.get("phase") == "executing"
        and isinstance(workspace_value, str)
        and workspace_value
    ):
        return
    readme = readme_by_prompt.get(prompt_name)
    if readme is None:
        raise InitializationError(
            f"No isolated-workspace README is configured for {prompt_name}"
        )
    if (
        create_runner_work_space(
            Path(workspace_value), source_code_directory, readme
        )
        == 0
    ):
        raise InitializationError(
            f"Could not create Prompt Runner workspace {workspace_value}"
        )
