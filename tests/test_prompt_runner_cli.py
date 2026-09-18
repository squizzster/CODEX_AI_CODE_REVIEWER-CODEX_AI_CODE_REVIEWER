from __future__ import annotations

import json
import subprocess
from pathlib import Path

from codex_ai_code_reviewer.initialization import PromptDefinition, PromptRunnerCli


def test_prompt_synchronization_publishes_then_explicitly_updates_defaults(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='runner'\nversion='0'\n")
    commands: list[list[str]] = []

    def fake_run(command, **options):
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"ok": True, "data": {"accepted": True}}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = PromptRunnerCli(tmp_path)
    prompt = PromptDefinition(
        project_name="CODEX_AI_CODE_REVIEW",
        prompt_name="ANALYZE_PIPELINE",
        source_path=tmp_path / "ANALYZE_PIPELINE.yaml",
        template="Review this project.\n",
        model="gpt-6-astra",
        reasoning_effort="xhigh",
        risk_profile="BALANCED",
    )

    runner.synchronize_prompt(prompt)

    assert commands == [
        [
            "uv",
            "run",
            "codex-prompt-runner",
            "prompt",
            "register",
            "CODEX_AI_CODE_REVIEW",
            "ANALYZE_PIPELINE",
            "--template",
            "Review this project.\n",
            "--model",
            "gpt-6-astra",
            "--reasoning",
            "xhigh",
            "--risk-profile",
            "BALANCED",
            "--make-current",
        ],
        [
            "uv",
            "run",
            "codex-prompt-runner",
            "prompt",
            "set-defaults",
            "CODEX_AI_CODE_REVIEW",
            "ANALYZE_PIPELINE",
            "--model",
            "gpt-6-astra",
            "--reasoning",
            "xhigh",
            "--risk-profile",
            "BALANCED",
        ],
    ]


def test_live_run_forwards_events_and_supplies_variables(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='runner'\nversion='0'\n")
    observed: dict[str, object] = {}

    def fake_run(command, **options):
        observed["command"] = command
        observed["options"] = options
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"ok": True, "data": {"output": "review"}}),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("VIRTUAL_ENV", "/unrelated/environment")
    runner = PromptRunnerCli(tmp_path)

    result = runner.run_prompt(
        "CODEX_AI_CODE_REVIEW",
        "ANALYZE_PIPELINE",
        variables={"PIPELINE_SPECIALIST": "lens", "ARG_DIRECTORY": "/project"},
        working_directory=Path("/project"),
    )

    assert result == {"output": "review"}
    assert observed["command"] == [
        "uv",
        "run",
        "codex-prompt-runner",
        "run",
        "CODEX_AI_CODE_REVIEW",
        "ANALYZE_PIPELINE",
        "--var",
        "ARG_DIRECTORY=/project",
        "--var",
        "PIPELINE_SPECIALIST=lens",
        "--cwd",
        "/project",
        "--live",
        "--detail",
    ]
    assert observed["options"]["stderr"] is None
    assert observed["options"]["stdout"] is subprocess.PIPE
    assert "VIRTUAL_ENV" not in observed["options"]["env"]


def test_live_run_forwards_model_and_reasoning_overrides(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='runner'\nversion='0'\n")
    observed: dict[str, object] = {}

    def fake_run(command, **options):
        observed["command"] = command
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"ok": True, "data": {"output": "review"}}),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = PromptRunnerCli(tmp_path)

    runner.run_prompt(
        "CODEX_AI_CODE_REVIEW",
        "ANALYZE_PIPELINE",
        variables={"ARG_DIRECTORY": "/project"},
        working_directory=Path("/project"),
        model="gpt-5.6-luna",
        reasoning_effort="max",
    )

    assert observed["command"] == [
        "uv",
        "run",
        "codex-prompt-runner",
        "run",
        "CODEX_AI_CODE_REVIEW",
        "ANALYZE_PIPELINE",
        "--model",
        "gpt-5.6-luna",
        "--reasoning",
        "max",
        "--var",
        "ARG_DIRECTORY=/project",
        "--cwd",
        "/project",
        "--live",
        "--detail",
    ]
