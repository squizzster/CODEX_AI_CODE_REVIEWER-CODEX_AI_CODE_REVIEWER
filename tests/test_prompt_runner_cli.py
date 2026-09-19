from __future__ import annotations

import json
import subprocess
from io import StringIO
from pathlib import Path

from codex_ai_code_reviewer.initialization import (
    PromptDefinition,
    PromptRunnerCli,
)


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
    events = [
        {
            "schema": "codex-prompt-runner.live-event/v1",
            "event": "heartbeat",
            "event_sequence": 1,
            "invocation_id": "invocation",
            "phase": "building",
        },
        {
            "schema": "codex-prompt-runner.live-event/v1",
            "event": "heartbeat",
            "event_sequence": 2,
            "invocation_id": "invocation",
            "phase": "executing",
            "isolated_workspace": "/state/attempt/isolated-workspace",
        },
    ]

    class FakeProcess:
        def __init__(self, command, **options):
            observed["command"] = command
            observed["options"] = options
            observed["process"] = self
            self.stderr = StringIO(
                "".join(json.dumps(event) + "\n" for event in events)
            )
            self.returncode = 0
            self.waited = False
            options["stdout"].write(
                json.dumps(
                    {
                        "ok": True,
                        "data": {"delivery_mode": "LIVE", "output": "review"},
                    }
                )
            )
            options["stdout"].flush()

        def poll(self):
            return self.returncode if self.waited else None

        def wait(self, timeout=None):
            del timeout
            self.waited = True
            return self.returncode

        def terminate(self):
            self.returncode = -15
            self.waited = True

        def kill(self):
            self.returncode = -9
            self.waited = True

    consumed: list[tuple[str, dict[str, object]]] = []
    forwarded = StringIO()

    def consume(prompt_name: str, payload: dict[str, object]) -> None:
        assert not observed["process"].waited
        consumed.append((prompt_name, payload))

    monkeypatch.setattr(subprocess, "Popen", FakeProcess)
    monkeypatch.setenv("VIRTUAL_ENV", "/unrelated/environment")
    runner = PromptRunnerCli(
        tmp_path, live_event_handler=consume, live_event_stream=forwarded
    )

    result = runner.run_prompt(
        "CODEX_AI_CODE_REVIEW",
        "ANALYZE_PIPELINE",
        variables={"PIPELINE_SPECIALIST": "lens", "ARG_DIRECTORY": "/project"},
        working_directory=Path("/project"),
    )

    assert result == {"delivery_mode": "LIVE", "output": "review"}
    assert consumed == [("ANALYZE_PIPELINE", event) for event in events]
    assert forwarded.getvalue() == "".join(
        json.dumps(event) + "\n" for event in events
    )
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
    assert observed["options"]["stderr"] is subprocess.PIPE
    assert observed["options"]["stdout"] is not subprocess.PIPE
    assert "VIRTUAL_ENV" not in observed["options"]["env"]


def test_live_run_forwards_model_and_reasoning_overrides(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='runner'\nversion='0'\n")
    observed: dict[str, object] = {}

    class FakeProcess:
        def __init__(self, command, **options):
            observed["command"] = command
            self.stderr = StringIO()
            self.returncode = 0
            self.waited = False
            options["stdout"].write(
                json.dumps({"ok": True, "data": {"output": "review"}})
            )
            options["stdout"].flush()

        def poll(self):
            return self.returncode if self.waited else None

        def wait(self, timeout=None):
            del timeout
            self.waited = True
            return self.returncode

        def terminate(self):
            self.returncode = -15
            self.waited = True

        def kill(self):
            self.returncode = -9
            self.waited = True

    monkeypatch.setattr(subprocess, "Popen", FakeProcess)
    runner = PromptRunnerCli(tmp_path, live_event_stream=StringIO())

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


def test_live_run_accepts_custom_executor_without_isolated_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='runner'\nversion='0'\n")

    class FakeProcess:
        def __init__(self, command, **options):
            del command
            self.stderr = StringIO(
                json.dumps(
                    {
                        "schema": "codex-prompt-runner.live-event/v1",
                        "event": "heartbeat",
                        "event_sequence": 1,
                        "invocation_id": "invocation",
                        "phase": "building",
                    }
                )
                + "\n"
            )
            self.returncode = 0
            self.waited = False
            options["stdout"].write(
                json.dumps(
                    {
                        "ok": True,
                        "data": {"delivery_mode": "LIVE", "output": "review"},
                    }
                )
            )
            options["stdout"].flush()

        def poll(self):
            return self.returncode if self.waited else None

        def wait(self, timeout=None):
            del timeout
            self.waited = True
            return self.returncode

        def terminate(self):
            self.returncode = -15
            self.waited = True

        def kill(self):
            self.returncode = -9
            self.waited = True

    monkeypatch.setattr(subprocess, "Popen", FakeProcess)
    handled: list[dict[str, object]] = []
    runner = PromptRunnerCli(
        tmp_path,
        live_event_handler=lambda _prompt, event: handled.append(event),
        live_event_stream=StringIO(),
    )

    result = runner.run_prompt(
        "CODEX_AI_CODE_REVIEW",
        "ANALYZE_PIPELINE",
        variables={"ARG_DIRECTORY": "/project"},
        working_directory=Path("/project"),
    )

    assert result == {"delivery_mode": "LIVE", "output": "review"}
    assert len(handled) == 1
