from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path

import pytest
from codex_prompt_runner_system import AttemptArtifacts, AttemptOutput
from codex_prompt_runner_system import PromptRunner as RealPromptRunner

from codex_ai_code_reviewer import initialization
from codex_ai_code_reviewer.initialization import (
    PROMPT_RUNNER_ATTEMPT_TIMEOUT_SECONDS,
    PROMPT_RUNNER_RETRY_DELAYS_SECONDS,
    InitializationError,
    ProjectDefinition,
    PromptDefinition,
    PromptRunnerLibrary,
    initialize_prompt_catalog,
)


class CapturingExecutor:
    def __init__(self, output: bytes = b"review") -> None:
        self.output = output
        self.calls: list[dict[str, object]] = []
        self.lock = threading.Lock()

    def execute(
        self,
        prompt,
        *,
        attempt_directory,
        model,
        reasoning_effort,
        permissions,
        timeout_seconds,
        event_sink,
    ):
        attempt_directory.mkdir(mode=0o700)
        event_sink.emit(
            "codex",
            codex_event={
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "review"},
            },
        )
        with self.lock:
            self.calls.append(
                {
                    "prompt": prompt,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                    "risk_profile_sha256": permissions.sha256,
                    "timeout_seconds": timeout_seconds,
                }
            )
        return AttemptOutput(
            self.output,
            0,
            AttemptArtifacts(
                output_sha256=hashlib.sha256(self.output).hexdigest(),
                events_sha256=hashlib.sha256(b"events").hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
            ),
            {
                "executor": "capturing",
                "execution_policy_sha256": permissions.sha256,
            },
        )


def _prompt(
    tmp_path: Path,
    *,
    project_name: str = "CODEX_AI_CODE_REVIEW",
    prompt_name: str = "ANALYZE_PIPELINE",
    template: str = "Review {{VAR:ARG_DIRECTORY}}.\n",
    model: str = "gpt-6-astra",
    reasoning_effort: str = "xhigh",
    risk_profile: str = "BALANCED",
) -> PromptDefinition:
    return PromptDefinition(
        project_name=project_name,
        prompt_name=prompt_name,
        source_path=tmp_path / project_name / f"{prompt_name}.yaml",
        template=template,
        model=model,
        reasoning_effort=reasoning_effort,
        risk_profile=risk_profile,
    )


def _prepared_gateway(
    tmp_path: Path, executor: CapturingExecutor | None = None
) -> tuple[PromptRunnerLibrary, PromptDefinition]:
    gateway = PromptRunnerLibrary(tmp_path / "state", executor=executor)
    prompt = _prompt(tmp_path)
    initialize_prompt_catalog(
        (ProjectDefinition(prompt.project_name, (prompt,)),), gateway
    )
    return gateway, prompt


def test_library_adapter_constructs_runner_with_review_policy(
    tmp_path: Path, monkeypatch
) -> None:
    observed: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, **options) -> None:
            observed.update(options)

    monkeypatch.setattr(initialization, "PromptRunner", FakeRunner)
    executor = CapturingExecutor()

    PromptRunnerLibrary(tmp_path / "state", executor=executor)

    assert observed == {
        "state_root": tmp_path / "state",
        "executor": executor,
        "retry_delays_seconds": PROMPT_RUNNER_RETRY_DELAYS_SECONDS,
        "attempt_timeout_seconds": PROMPT_RUNNER_ATTEMPT_TIMEOUT_SECONDS,
    }
    assert PROMPT_RUNNER_RETRY_DELAYS_SECONDS == (120, 300)
    assert PROMPT_RUNNER_ATTEMPT_TIMEOUT_SECONDS == 5400.0


def test_catalog_sync_links_identical_global_prompt_to_both_projects(
    tmp_path: Path,
) -> None:
    gateway = PromptRunnerLibrary(tmp_path / "state")
    original = _prompt(tmp_path)
    v2 = _prompt(tmp_path, project_name="CODEX_AI_CODE_REVIEW_V2")

    report = initialize_prompt_catalog(
        (
            ProjectDefinition(original.project_name, (original,)),
            ProjectDefinition(v2.project_name, (v2,)),
        ),
        gateway,
    )

    assert report.created_projects == (
        "CODEX_AI_CODE_REVIEW",
        "CODEX_AI_CODE_REVIEW_V2",
    )
    assert report.created_prompts == (
        "CODEX_AI_CODE_REVIEW/ANALYZE_PIPELINE",
        "CODEX_AI_CODE_REVIEW_V2/ANALYZE_PIPELINE",
    )
    assert gateway.prompt_status(original) == "current"
    assert gateway.prompt_status(v2) == "current"


def test_prompt_synchronization_updates_drifted_defaults(tmp_path: Path) -> None:
    gateway, prompt = _prepared_gateway(tmp_path)
    changed = _prompt(tmp_path, model="gpt-5.6-luna", reasoning_effort="max")

    assert gateway.prompt_status(changed) == "drifted"
    gateway.synchronize_prompt(changed)

    assert gateway.prompt_status(changed) == "current"
    assert gateway.prompt_status(prompt) == "drifted"


def test_live_run_uses_90_minute_timeout_and_forwards_typed_events(
    tmp_path: Path,
) -> None:
    executor = CapturingExecutor()
    handled: list[tuple[str, dict[str, object]]] = []
    forwarded = StringIO()
    gateway = PromptRunnerLibrary(
        tmp_path / "state",
        executor=executor,
        live_event_handler=lambda prompt, event: handled.append((prompt, event)),
        live_event_stream=forwarded,
    )
    prompt = _prompt(tmp_path)
    initialize_prompt_catalog(
        (ProjectDefinition(prompt.project_name, (prompt,)),), gateway
    )

    result = gateway.run_prompt(
        prompt.project_name,
        prompt.prompt_name,
        variables={"ARG_DIRECTORY": "/project"},
        working_directory=tmp_path,
        model="gpt-5.6-luna",
        reasoning_effort="max",
    )

    assert result["delivery_mode"] == "LIVE"
    assert result["output"] == "review"
    assert result["model"] == "gpt-5.6-luna"
    assert result["reasoning_effort"] == "max"
    assert result["risk_profile"] == "BALANCED"
    assert result["execution_run_id"]
    assert executor.calls[0]["timeout_seconds"] == 5400.0
    assert b"Review /project." in executor.calls[0]["prompt"]

    events = [json.loads(line) for line in forwarded.getvalue().splitlines()]
    assert events
    assert [event["event_sequence"] for event in events] == list(
        range(1, len(events) + 1)
    )
    assert len({event["invocation_id"] for event in events}) == 1
    assert all(
        event["schema"] == "codex-prompt-runner.live-event/v1" for event in events
    )
    assert handled == [(prompt.prompt_name, event) for event in events]
    assert any(
        event["event"] == "heartbeat" and event.get("phase") == "executing"
        for event in events
    )
    assert any(event["event"] == "codex" for event in events)


def test_one_library_gateway_runs_six_prompts_concurrently(tmp_path: Path) -> None:
    executor = CapturingExecutor()
    gateway = PromptRunnerLibrary(
        tmp_path / "state", executor=executor, live_event_stream=StringIO()
    )
    prompts = tuple(
        _prompt(tmp_path, prompt_name=f"ANALYZE_ROLE_{number}", template="Review.\n")
        for number in range(6)
    )
    initialize_prompt_catalog(
        (ProjectDefinition("CODEX_AI_CODE_REVIEW", prompts),), gateway
    )

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(
                lambda prompt: gateway.run_prompt(
                    prompt.project_name,
                    prompt.prompt_name,
                    variables={},
                    working_directory=tmp_path,
                ),
                prompts,
            )
        )

    assert len(results) == 6
    assert all(result["delivery_mode"] == "LIVE" for result in results)
    assert len({result["execution_run_id"] for result in results}) == 6
    assert len(executor.calls) == 6
    assert {call["timeout_seconds"] for call in executor.calls} == {5400.0}


def test_live_event_handler_failure_fails_the_review(tmp_path: Path) -> None:
    executor = CapturingExecutor()

    def reject_event(_prompt: str, _event: dict[str, object]) -> None:
        raise InitializationError("workspace setup failed")

    gateway = PromptRunnerLibrary(
        tmp_path / "state",
        executor=executor,
        live_event_handler=reject_event,
        live_event_stream=StringIO(),
    )
    prompt = _prompt(tmp_path)
    initialize_prompt_catalog(
        (ProjectDefinition(prompt.project_name, (prompt,)),), gateway
    )

    with pytest.raises(InitializationError, match="workspace setup failed"):
        gateway.run_prompt(
            prompt.project_name,
            prompt.prompt_name,
            variables={"ARG_DIRECTORY": "/project"},
            working_directory=tmp_path,
        )


def test_stock_execution_requires_an_advertised_isolated_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    def runner_without_stock_workspace(**options):
        options["executor"] = CapturingExecutor()
        return RealPromptRunner(**options)

    monkeypatch.setattr(initialization, "PromptRunner", runner_without_stock_workspace)
    gateway = PromptRunnerLibrary(tmp_path / "state", live_event_stream=StringIO())
    prompt = _prompt(tmp_path)
    initialize_prompt_catalog(
        (ProjectDefinition(prompt.project_name, (prompt,)),), gateway
    )

    with pytest.raises(InitializationError, match="no isolated workspace"):
        gateway.run_prompt(
            prompt.project_name,
            prompt.prompt_name,
            variables={"ARG_DIRECTORY": "/project"},
            working_directory=tmp_path,
        )
