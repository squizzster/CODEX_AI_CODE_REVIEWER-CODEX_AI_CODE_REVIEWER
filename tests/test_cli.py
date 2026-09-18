from __future__ import annotations

import os
from pathlib import Path
from threading import Barrier, Lock
from typing import Any

import pytest

from codex_ai_code_reviewer.cli import (
    ANALYSIS_PROJECT,
    ANALYSIS_PROMPTS,
    COMPARISON_PROMPT,
    CONFIG_ROOT,
    REPORT_VARIABLE_BY_PROMPT,
    SPECIALIST_PROMPTS,
    VARIABLES_ROOT,
    _analysis_prompt_definitions,
    _require_expected_execution,
    _review_directory,
    _run_review_pipeline,
    _write_final_review,
)
from codex_ai_code_reviewer.initialization import (
    InitializationError,
    ProjectDefinition,
    PromptDefinition,
    compose_variable_values,
    load_project_definitions,
    load_variable_definitions,
)


def _prompt_definitions(tmp_path: Path) -> dict[str, PromptDefinition]:
    return {
        prompt_name: PromptDefinition(
            project_name=ANALYSIS_PROJECT,
            prompt_name=prompt_name,
            source_path=tmp_path / f"{prompt_name}.yaml",
            template=f"Run {prompt_name}",
            model="gpt-6-astra",
            reasoning_effort="xhigh",
            risk_profile="BALANCED",
        )
        for prompt_name in ANALYSIS_PROMPTS
    }


def _successful_review(output: str) -> dict[str, Any]:
    return {
        "delivery_mode": "LIVE",
        "model": "gpt-6-astra",
        "output": output,
        "reasoning_effort": "xhigh",
        "risk_profile": "BALANCED",
    }


class ParallelReviewRunner:
    def __init__(self) -> None:
        self._specialist_start = Barrier(len(SPECIALIST_PROMPTS))
        self._lock = Lock()
        self.calls: list[str] = []
        self.comparison_variables: dict[str, str] | None = None

    def run_prompt(
        self,
        project_name: str,
        prompt_name: str,
        *,
        variables: dict[str, str],
        working_directory: Path,
    ) -> dict[str, Any]:
        assert project_name == ANALYSIS_PROJECT
        assert working_directory.is_dir()
        with self._lock:
            self.calls.append(prompt_name)
        if prompt_name in SPECIALIST_PROMPTS:
            self._specialist_start.wait(timeout=2)
            return _successful_review(
                f"Report from {prompt_name}; literal {{{{VAR:ARG_DIRECTORY}}}}"
            )
        assert prompt_name == COMPARISON_PROMPT
        self.comparison_variables = variables
        return _successful_review("Audited final review")


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


def test_execution_must_match_configured_policy(tmp_path: Path) -> None:
    prompt = PromptDefinition(
        project_name="CODEX_AI_CODE_REVIEW",
        prompt_name="ANALYZE_PIPELINE",
        source_path=tmp_path / "ANALYZE_PIPELINE.yaml",
        template="Review",
        model="gpt-6-astra",
        reasoning_effort="xhigh",
        risk_profile="BALANCED",
    )
    result = {
        "delivery_mode": "LIVE",
        "model": "gpt-6-astra",
        "reasoning_effort": "xhigh",
        "risk_profile": "BALANCED",
    }

    _require_expected_execution(result, prompt)

    result["risk_profile"] = "LOCKED_DOWN"
    with pytest.raises(InitializationError, match="configured policy"):
        _require_expected_execution(result, prompt)


def test_analysis_configuration_requires_every_pipeline_prompt(tmp_path: Path) -> None:
    prompts = _prompt_definitions(tmp_path)
    project = ProjectDefinition(
        ANALYSIS_PROJECT,
        tuple(prompts[name] for name in ANALYSIS_PROMPTS if name != COMPARISON_PROMPT),
    )

    with pytest.raises(InitializationError, match=COMPARISON_PROMPT):
        _analysis_prompt_definitions((project,))


def test_repository_prompts_share_one_resolved_directory_context(
    tmp_path: Path,
) -> None:
    prompts = _analysis_prompt_definitions(load_project_definitions(CONFIG_ROOT))
    configured_variables = load_variable_definitions(VARIABLES_ROOT)

    for prompt in prompts.values():
        assert prompt.template.count("{{VAR:REVIEW_DIRECTORY_CONTEXT}}") == 1
        assert "Your base critical project review directory is:" not in prompt.template

    values = compose_variable_values(configured_variables, tmp_path)
    context = values["REVIEW_DIRECTORY_CONTEXT"]
    assert str(tmp_path) in context
    assert "{{VAR:" not in context


def test_review_pipeline_runs_specialists_in_parallel_then_compares_reports(
    tmp_path: Path,
) -> None:
    runner = ParallelReviewRunner()
    prompts = _prompt_definitions(tmp_path)

    result = _run_review_pipeline(
        runner,
        prompts,
        variables={"ARG_DIRECTORY": str(tmp_path)},
        working_directory=tmp_path,
    )

    assert tuple(result.specialist_reviews) == SPECIALIST_PROMPTS
    assert result.comparison_review["output"] == "Audited final review"
    assert runner.calls[-1] == COMPARISON_PROMPT
    assert runner.comparison_variables is not None
    for prompt_name, variable_name in REPORT_VARIABLE_BY_PROMPT.items():
        assert (
            runner.comparison_variables[variable_name]
            == f"Report from {prompt_name}; literal {{{{VAR:ARG_DIRECTORY}}}}"
        )


def test_review_pipeline_does_not_compare_incomplete_specialist_reports(
    tmp_path: Path,
) -> None:
    prompts = _prompt_definitions(tmp_path)

    class EmptyReportRunner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def run_prompt(
            self,
            project_name: str,
            prompt_name: str,
            *,
            variables: dict[str, str],
            working_directory: Path,
        ) -> dict[str, Any]:
            self.calls.append(prompt_name)
            output = "" if prompt_name == "ANALYZE_BOUNDARIES" else "Report"
            return _successful_review(output)

    runner = EmptyReportRunner()

    with pytest.raises(InitializationError, match="ANALYZE_BOUNDARIES"):
        _run_review_pipeline(
            runner,
            prompts,
            variables={"ARG_DIRECTORY": str(tmp_path)},
            working_directory=tmp_path,
        )

    assert COMPARISON_PROMPT not in runner.calls


def test_final_review_is_atomically_replaced_with_complete_markdown(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "final_review.md"
    output_path.write_text("stale\n", encoding="utf-8")

    observed_path = _write_final_review("audited review", output_path)

    assert observed_path == output_path
    assert output_path.read_text(encoding="utf-8") == "audited review\n"
    assert list(tmp_path.iterdir()) == [output_path]
