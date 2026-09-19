"""Command-line entry point for config initialization and future review execution."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from codex_ai_code_reviewer.initialization import (
    ARG_DIRECTORY_VARIABLE,
    REASONING_EFFORTS,
    InitializationError,
    ProjectDefinition,
    PromptDefinition,
    PromptRunnerCli,
    compose_variable_values,
    initialize_prompt_catalog,
    load_project_definitions,
    load_variable_definitions,
    prompt_output_variable_name,
    variable_reference_names,
)
from codex_ai_code_reviewer.live_events import ReviewWorkspaceInitializer

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = PROJECT_ROOT / "conf" / "projects"
VARIABLES_ROOT = PROJECT_ROOT / "conf" / "vars"
DEFAULT_RUNNER_ROOT = PROJECT_ROOT.parent / "CODEX_PROMPT_RUNNER_SYSTEM"
DEFAULT_REPORTS_ROOT = PROJECT_ROOT / "reports"
RESULT_SCHEMA = "codex-ai-code-reviewer.result/v2"
ANALYSIS_PROJECT = "CODEX_AI_CODE_REVIEW"
# Reconnaissance is temporarily disabled; its prompt configuration is retained.
SPECIALIST_PROMPTS = (
    "ANALYZE_PIPELINE",
    "ANALYZE_BOUNDARIES",
    "ANALYZE_NETWORKING",
    "ANALYZE_INTEGRITY",
    "ANALYZE_SECURITY",
    "ANALYZE_PERFORMANCE",
)
SPECIALIST_VARIABLE_BY_PROMPT = {
    "ANALYZE_PIPELINE": "PIPELINE_SPECIALIST",
    "ANALYZE_BOUNDARIES": "BOUNDARIES_SPECIALIST",
    "ANALYZE_NETWORKING": "NETWORKING_SPECIALIST",
    "ANALYZE_INTEGRITY": "INTEGRITY_SPECIALIST",
    "ANALYZE_SECURITY": "SECURITY_SPECIALIST",
    "ANALYZE_PERFORMANCE": "PERFORMANCE_SPECIALIST",
}
COMPARISON_PROMPT = "COMPARE_AGENT_REPORTS"
ANALYSIS_PROMPTS = (*SPECIALIST_PROMPTS, COMPARISON_PROMPT)


class ReviewExecutionGateway(Protocol):
    def run_prompt(
        self,
        project_name: str,
        prompt_name: str,
        *,
        variables: dict[str, str],
        working_directory: Path,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ReviewPipelineResult:
    specialist_reviews: dict[str, dict[str, Any]]
    comparison_review: dict[str, Any]
    prompt_output_variables: dict[str, str]


@dataclass(frozen=True, slots=True)
class ReviewExecutionOverrides:
    model: str | None = None
    reasoning_effort: str | None = None


DEFAULT_EXECUTION_OVERRIDES = ReviewExecutionOverrides()


@dataclass(frozen=True, slots=True)
class ReportPublication:
    project_name: str
    run_id: str
    report_directory: Path
    report_paths: dict[str, Path]


def _model_name(value: str) -> str:
    if not value or any(character.isspace() for character in value):
        raise argparse.ArgumentTypeError(
            "model must be non-empty and contain no whitespace"
        )
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="perform_a_code_review.py",
        description="Initialize configured prompts and perform a live code review.",
    )
    parser.add_argument(
        "--model",
        type=_model_name,
        help="override every prompt's configured model for this run",
    )
    parser.add_argument(
        "--reasoning",
        dest="reasoning_effort",
        choices=sorted(REASONING_EFFORTS),
        help="override every prompt's configured reasoning effort for this run",
    )
    parser.add_argument(
        "--reports-directory",
        type=Path,
        default=DEFAULT_REPORTS_ROOT,
        help="report root containing <project_name>/<run_id>/ directories",
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


def _enter_review_directory(review_directory: Path) -> None:
    """Anchor the reviewer process in the validated target before any execution."""

    try:
        os.chdir(review_directory)
    except OSError as error:
        raise InitializationError(
            f"Cannot enter review directory {review_directory}: {error}"
        ) from error


def _reports_root(path: Path) -> Path:
    """Resolve the reports root before entering the reviewed directory."""

    try:
        return path.expanduser().resolve(strict=False)
    except OSError as error:
        raise InitializationError(
            f"Cannot resolve reports directory {path}: {error}"
        ) from error


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
    review: dict[str, object],
    prompt: PromptDefinition,
    overrides: ReviewExecutionOverrides = DEFAULT_EXECUTION_OVERRIDES,
) -> None:
    expected = {
        "delivery_mode": "LIVE",
        "model": overrides.model or prompt.model,
        "reasoning_effort": overrides.reasoning_effort or prompt.reasoning_effort,
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


def _require_pipeline_variable_contract(
    prompts: dict[str, PromptDefinition], variables: dict[str, str]
) -> None:
    """Validate stage-visible variables before any prompt execution."""

    output_variable_by_prompt = {
        prompt_name: prompt_output_variable_name(prompt_name)
        for prompt_name in ANALYSIS_PROMPTS
    }
    collisions = sorted(variables.keys() & output_variable_by_prompt.values())
    if collisions:
        raise InitializationError(
            "Prompt output variables are generated at runtime and cannot be supplied: "
            f"{', '.join(collisions)}"
        )

    base_variables = set(variables)
    available_by_prompt = {
        prompt_name: base_variables for prompt_name in SPECIALIST_PROMPTS
    }
    available_by_prompt[COMPARISON_PROMPT] = base_variables | {
        output_variable_by_prompt[prompt_name] for prompt_name in SPECIALIST_PROMPTS
    }

    for prompt_name in ANALYSIS_PROMPTS:
        missing = sorted(
            variable_reference_names(prompts[prompt_name].template)
            - available_by_prompt[prompt_name]
        )
        if missing:
            raise InitializationError(
                f"{prompts[prompt_name].source_path}: prompt {prompt_name} references "
                f"variables unavailable at its pipeline stage: {', '.join(missing)}"
            )


def _workspace_readmes(
    prompts: dict[str, PromptDefinition], variables: dict[str, str]
) -> dict[str, str]:
    """Build the prompt-specific context copied into isolated workspaces."""

    return {
        **{
            prompt_name: variables[variable_name]
            for prompt_name, variable_name in SPECIALIST_VARIABLE_BY_PROMPT.items()
        },
        COMPARISON_PROMPT: prompts[COMPARISON_PROMPT].template,
    }


def _run_review_pipeline(
    runner: ReviewExecutionGateway,
    prompts: dict[str, PromptDefinition],
    *,
    variables: dict[str, str],
    working_directory: Path,
    overrides: ReviewExecutionOverrides = DEFAULT_EXECUTION_OVERRIDES,
) -> ReviewPipelineResult:
    """Run independent specialists concurrently, then audit their reports."""

    _require_pipeline_variable_contract(prompts, variables)
    specialist_reviews: dict[str, dict[str, Any]] = {}
    prompt_output_variables: dict[str, str] = {}
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
                model=overrides.model,
                reasoning_effort=overrides.reasoning_effort,
            ): prompt_name
            for prompt_name in SPECIALIST_PROMPTS
        }
        for future in as_completed(futures):
            prompt_name = futures[future]
            prompt = prompts[prompt_name]
            try:
                review = future.result()
                _require_expected_execution(review, prompt, overrides)
                _require_report_output(review, prompt)
            except InitializationError as error:
                failures[prompt_name] = f"{type(error).__name__}: {error}"
            else:
                specialist_reviews[prompt_name] = review
                prompt_output_variables[prompt_output_variable_name(prompt_name)] = (
                    _require_report_output(review, prompt)
                )

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
    comparison_variables.update(prompt_output_variables)
    comparison = runner.run_prompt(
        ANALYSIS_PROJECT,
        COMPARISON_PROMPT,
        variables=comparison_variables,
        working_directory=working_directory,
        model=overrides.model,
        reasoning_effort=overrides.reasoning_effort,
    )
    comparison_prompt = prompts[COMPARISON_PROMPT]
    _require_expected_execution(comparison, comparison_prompt, overrides)
    prompt_output_variables[prompt_output_variable_name(COMPARISON_PROMPT)] = (
        _require_report_output(comparison, comparison_prompt)
    )
    return ReviewPipelineResult(
        ordered_specialist_reviews,
        comparison,
        prompt_output_variables,
    )


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


def _report_project_name(review_directory: Path) -> str:
    target_name = re.sub(r"[^A-Za-z0-9._-]+", "_", review_directory.name)
    return target_name.strip("._-")[:80] or "repository"


def _review_run_id(timestamp_ns: int | None = None) -> str:
    observed_ns = time.time_ns() if timestamp_ns is None else timestamp_ns
    seconds, nanoseconds = divmod(observed_ns, 1_000_000_000)
    observed_at = datetime.fromtimestamp(seconds, UTC)
    hundredths = nanoseconds // 10_000_000
    return f"{observed_at:%Y-%m-%dT%H-%M-%S}.{hundredths:02d}Z"


def _publish_reports(
    prompt_outputs: dict[str, str],
    *,
    review_directory: Path,
    reports_root: Path,
    run_id: str,
) -> ReportPublication:
    """Atomically publish one complete directory of prompt reports."""

    if re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}\.[0-9]{2}Z",
        run_id,
    ) is None:
        raise InitializationError(f"Invalid review run ID: {run_id!r}")
    expected_names = tuple(
        prompt_output_variable_name(prompt_name) for prompt_name in ANALYSIS_PROMPTS
    )
    if set(prompt_outputs) != set(expected_names):
        missing = sorted(set(expected_names) - set(prompt_outputs))
        unexpected = sorted(set(prompt_outputs) - set(expected_names))
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected {', '.join(unexpected)}")
        raise InitializationError(
            f"Cannot publish incomplete prompt reports ({'; '.join(details)})"
        )

    staging_directory: Path | None = None
    project_name = _report_project_name(review_directory)
    project_reports_directory = reports_root / project_name
    try:
        project_reports_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not project_reports_directory.is_dir():
            raise OSError(f"not a directory: {project_reports_directory}")
        staging_directory = Path(
            tempfile.mkdtemp(
                prefix=".code_review_pending_", dir=project_reports_directory
            )
        )
        for output_name in expected_names:
            report_path = staging_directory / f"{output_name}.md"
            with report_path.open("x", encoding="utf-8") as report_file:
                content = prompt_outputs[output_name]
                report_file.write(content)
                if not content.endswith("\n"):
                    report_file.write("\n")
                report_file.flush()
                os.fsync(report_file.fileno())

        report_directory = project_reports_directory / run_id
        if report_directory.exists():
            raise OSError(f"report directory already exists: {report_directory}")
        os.replace(staging_directory, report_directory)
        staging_directory = None
    except OSError as error:
        if staging_directory is not None:
            shutil.rmtree(staging_directory, ignore_errors=True)
        raise InitializationError(
            f"Cannot publish reports in {reports_root}: {error}"
        ) from error

    return ReportPublication(
        project_name,
        run_id,
        report_directory,
        {
            output_name: report_directory / f"{output_name}.md"
            for output_name in expected_names
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    overrides = ReviewExecutionOverrides(
        model=arguments.model,
        reasoning_effort=arguments.reasoning_effort,
    )
    try:
        review_directory = _review_directory(arguments.directory)
        reports_root = _reports_root(arguments.reports_directory)
        _enter_review_directory(review_directory)
        projects = load_project_definitions(CONFIG_ROOT)
        analysis_prompts = _analysis_prompt_definitions(projects)
        configured_variables = load_variable_definitions(VARIABLES_ROOT)
        reserved_prompt_output_variables = {
            prompt_output_variable_name(prompt.prompt_name)
            for project in projects
            for prompt in project.prompts
        }
        variable_values = compose_variable_values(
            configured_variables,
            review_directory,
            reserved_runtime_variables=reserved_prompt_output_variables,
        )
        _require_pipeline_variable_contract(analysis_prompts, variable_values)
        configured_runner_root = os.environ.get("CODEX_PROMPT_RUNNER_PROJECT_ROOT")
        runner_root = (
            Path(configured_runner_root).expanduser()
            if configured_runner_root
            else DEFAULT_RUNNER_ROOT
        )
        runner = PromptRunnerCli(
            runner_root,
            live_event_handler=ReviewWorkspaceInitializer(
                _workspace_readmes(analysis_prompts, variable_values)
            ),
        )
        report = initialize_prompt_catalog(projects, runner)
        review_run_id = _review_run_id()
        pipeline = _run_review_pipeline(
            runner,
            analysis_prompts,
            variables=variable_values,
            working_directory=review_directory,
            overrides=overrides,
        )
        publication = _publish_reports(
            pipeline.prompt_output_variables,
            review_directory=review_directory,
            reports_root=reports_root,
            run_id=review_run_id,
        )
    except InitializationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    output = {
        "schema": RESULT_SCHEMA,
        "ok": True,
        "phase": "code_review_complete",
        "report_project_name": publication.project_name,
        "report_run_id": publication.run_id,
        "report_directory": str(publication.report_directory),
        "report_paths": {
            output_name: str(report_path)
            for output_name, report_path in publication.report_paths.items()
        },
        "review_directory": str(review_directory),
        "catalog": report.as_dict(),
        "variables": sorted(
            variable_values.keys() | pipeline.prompt_output_variables.keys()
        ),
        "runtime_variables": {ARG_DIRECTORY_VARIABLE: str(review_directory)},
        "specialist_reviews": {
            prompt_name: _review_summary(prompt_name, review)
            for prompt_name, review in pipeline.specialist_reviews.items()
        },
        "review": _review_summary(COMPARISON_PROMPT, pipeline.comparison_review),
    }
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0
