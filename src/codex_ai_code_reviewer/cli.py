"""Command-line entry point for config initialization and future review execution."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from codex_ai_code_reviewer.initialization import (
    AGENT_REPORT_VARIABLES,
    ARG_DIRECTORY_VARIABLE,
    InitializationError,
    ProjectDefinition,
    PromptDefinition,
    PromptRunnerCli,
    compose_variable_values,
    initialize_prompt_catalog,
    load_project_definitions,
    load_variable_definitions,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = PROJECT_ROOT / "conf" / "projects"
VARIABLES_ROOT = PROJECT_ROOT / "conf" / "vars"
DEFAULT_RUNNER_ROOT = PROJECT_ROOT.parent / "CODEX_PROMPT_RUNNER_SYSTEM"
FINAL_REVIEW_PATH = Path("/tmp/final_review.md")
ANALYSIS_PROJECT = "CODEX_AI_CODE_REVIEW"
SPECIALIST_PROMPTS = (
    "ANALYZE_PIPELINE",
    "ANALYZE_BOUNDARIES",
    "ANALYZE_NETWORKING",
)
COMPARISON_PROMPT = "COMPARE_AGENT_REPORTS"
REPORT_VARIABLE_BY_PROMPT = {
    "ANALYZE_PIPELINE": "AGENT_1_REPORT",
    "ANALYZE_BOUNDARIES": "AGENT_2_REPORT",
    "ANALYZE_NETWORKING": "AGENT_3_REPORT",
}
ANALYSIS_PROMPTS = (*SPECIALIST_PROMPTS, COMPARISON_PROMPT)


class ReviewExecutionGateway(Protocol):
    def run_prompt(
        self,
        project_name: str,
        prompt_name: str,
        *,
        variables: dict[str, str],
        working_directory: Path,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ReviewPipelineResult:
    specialist_reviews: dict[str, dict[str, Any]]
    comparison_review: dict[str, Any]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="perform_a_code_review.py",
        description="Initialize configured prompts and perform a live code review.",
    )
    parser.add_argument("directory", type=Path, help="directory to review")
    return parser


def _review_directory(path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise InitializationError(f"Review directory does not exist: {path}") from error
    if not resolved.is_dir():
        raise InitializationError(f"Review target is not a directory: {resolved}")
    if not os.access(resolved, os.R_OK | os.X_OK, effective_ids=True):
        raise InitializationError(
            f"Review directory is not readable and traversable: {resolved}"
        )
    try:
        with os.scandir(resolved) as entries:
            next(entries, None)
    except OSError as error:
        raise InitializationError(
            f"Review directory cannot be read: {resolved}: {error}"
        ) from error
    return resolved


def _analysis_prompt_definitions(
    projects: tuple[ProjectDefinition, ...],
) -> dict[str, PromptDefinition]:
    prompts = {
        prompt.prompt_name: prompt
        for project in projects
        if project.project_name == ANALYSIS_PROJECT
        for prompt in project.prompts
        if prompt.prompt_name in ANALYSIS_PROMPTS
    }
    missing = [name for name in ANALYSIS_PROMPTS if name not in prompts]
    if missing:
        raise InitializationError(
            f"Configuration must define {ANALYSIS_PROJECT} prompts: "
            f"{', '.join(missing)}"
        )
    return {name: prompts[name] for name in ANALYSIS_PROMPTS}


def _require_expected_execution(
    review: dict[str, object], prompt: PromptDefinition
) -> None:
    expected = {
        "delivery_mode": "LIVE",
        "model": prompt.model,
        "reasoning_effort": prompt.reasoning_effort,
        "risk_profile": prompt.risk_profile,
    }
    mismatches = {
        field: {"expected": value, "observed": review.get(field)}
        for field, value in expected.items()
        if review.get(field) != value
    }
    if mismatches:
        raise InitializationError(
            f"Prompt Runner execution did not match configured policy: {mismatches}"
        )


def _require_report_output(review: dict[str, Any], prompt: PromptDefinition) -> str:
    output = review.get("output")
    if not isinstance(output, str) or not output.strip():
        raise InitializationError(
            f"Prompt Runner returned no report for "
            f"{prompt.project_name}/{prompt.prompt_name}"
        )
    return output


def _run_review_pipeline(
    runner: ReviewExecutionGateway,
    prompts: dict[str, PromptDefinition],
    *,
    variables: dict[str, str],
    working_directory: Path,
) -> ReviewPipelineResult:
    """Run independent specialists concurrently, then audit their reports."""

    specialist_reviews: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    with ThreadPoolExecutor(
        max_workers=len(SPECIALIST_PROMPTS), thread_name_prefix="code-review"
    ) as executor:
        futures = {
            executor.submit(
                runner.run_prompt,
                ANALYSIS_PROJECT,
                prompt_name,
                variables=variables,
                working_directory=working_directory,
            ): prompt_name
            for prompt_name in SPECIALIST_PROMPTS
        }
        for future in as_completed(futures):
            prompt_name = futures[future]
            prompt = prompts[prompt_name]
            try:
                review = future.result()
                _require_expected_execution(review, prompt)
                _require_report_output(review, prompt)
            except InitializationError as error:
                failures[prompt_name] = f"{type(error).__name__}: {error}"
            else:
                specialist_reviews[prompt_name] = review

    if failures:
        details = "; ".join(
            f"{prompt_name}: {failures[prompt_name]}"
            for prompt_name in SPECIALIST_PROMPTS
            if prompt_name in failures
        )
        raise InitializationError(f"Specialist review stage failed: {details}")

    ordered_specialist_reviews = {
        prompt_name: specialist_reviews[prompt_name]
        for prompt_name in SPECIALIST_PROMPTS
    }
    comparison_variables = dict(variables)
    comparison_variables.update(
        {
            REPORT_VARIABLE_BY_PROMPT[prompt_name]: _require_report_output(
                ordered_specialist_reviews[prompt_name], prompts[prompt_name]
            )
            for prompt_name in SPECIALIST_PROMPTS
        }
    )
    comparison = runner.run_prompt(
        ANALYSIS_PROJECT,
        COMPARISON_PROMPT,
        variables=comparison_variables,
        working_directory=working_directory,
    )
    comparison_prompt = prompts[COMPARISON_PROMPT]
    _require_expected_execution(comparison, comparison_prompt)
    _require_report_output(comparison, comparison_prompt)
    return ReviewPipelineResult(ordered_specialist_reviews, comparison)


def _review_summary(prompt_name: str, review: dict[str, Any]) -> dict[str, Any]:
    provenance = review.get("provenance")
    usage = provenance.get("usage") if isinstance(provenance, dict) else None
    return {
        "attempts": review.get("attempts"),
        "delivery_mode": review.get("delivery_mode"),
        "execution_run_id": review.get("execution_run_id"),
        "model": review.get("model"),
        "output": review.get("output"),
        "prompt": prompt_name,
        "reasoning_effort": review.get("reasoning_effort"),
        "request_id": review.get("request_id"),
        "result_id": review.get("result_id"),
        "risk_profile": review.get("risk_profile"),
        "usage": usage,
    }


def _write_final_review(content: str, output_path: Path = FINAL_REVIEW_PATH) -> Path:
    """Atomically publish the successful comparison report as Markdown."""

    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            text=True,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(content)
            if not content.endswith("\n"):
                temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, output_path)
    except OSError as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise InitializationError(
            f"Cannot write final review to {output_path}: {error}"
        ) from error
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        review_directory = _review_directory(arguments.directory)
        projects = load_project_definitions(CONFIG_ROOT)
        analysis_prompts = _analysis_prompt_definitions(projects)
        configured_variables = load_variable_definitions(VARIABLES_ROOT)
        variable_values = compose_variable_values(
            configured_variables, review_directory
        )
        configured_runner_root = os.environ.get("CODEX_PROMPT_RUNNER_PROJECT_ROOT")
        runner_root = (
            Path(configured_runner_root).expanduser()
            if configured_runner_root
            else DEFAULT_RUNNER_ROOT
        )
        runner = PromptRunnerCli(runner_root)
        report = initialize_prompt_catalog(projects, runner)
        pipeline = _run_review_pipeline(
            runner,
            analysis_prompts,
            variables=variable_values,
            working_directory=review_directory,
        )
        final_review_path = _write_final_review(
            _require_report_output(
                pipeline.comparison_review,
                analysis_prompts[COMPARISON_PROMPT],
            )
        )
    except InitializationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    output = {
        "ok": True,
        "phase": "code_review_complete",
        "final_review_path": str(final_review_path),
        "review_directory": str(review_directory),
        "catalog": report.as_dict(),
        "variables": sorted(variable_values.keys() | AGENT_REPORT_VARIABLES),
        "runtime_variables": {ARG_DIRECTORY_VARIABLE: str(review_directory)},
        "specialist_reviews": {
            prompt_name: _review_summary(prompt_name, review)
            for prompt_name, review in pipeline.specialist_reviews.items()
        },
        "review": _review_summary(COMPARISON_PROMPT, pipeline.comparison_review),
    }
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0
