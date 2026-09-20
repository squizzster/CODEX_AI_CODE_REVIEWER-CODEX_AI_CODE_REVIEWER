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
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from codex_ai_code_reviewer.initialization import (
    ARG_DIRECTORY_VARIABLE,
    PROMPT_RUNNER_ATTEMPT_TIMEOUT_SECONDS,
    PROMPT_RUNNER_RETRY_DELAYS_SECONDS,
    REASONING_EFFORTS,
    InitializationError,
    ProjectDefinition,
    PromptDefinition,
    PromptRunnerLibrary,
    VariableDefinition,
    compose_variable_values,
    initialize_prompt_catalog,
    load_project_definitions,
    load_variable_definitions,
    prompt_output_variable_name,
    variable_reference_names,
)
from codex_ai_code_reviewer.live_events import (
    OUTPUT_DIRECTORY_NAME,
    create_runner_work_space_from_event,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = PROJECT_ROOT / "conf" / "projects"
VARIABLES_ROOT = PROJECT_ROOT / "conf" / "vars"
DEFAULT_REPORTS_ROOT = PROJECT_ROOT / "reports"
RESULT_SCHEMA = "codex-ai-code-reviewer.result/v4"
ANALYSIS_PROJECT = "CODEX_AI_CODE_REVIEW"
V2_ANALYSIS_PROJECT = "CODEX_AI_CODE_REVIEW_V2"
DEFAULT_PROMPT_VERSION = "original"
PROMPT_PROJECT_BY_VERSION = {
    DEFAULT_PROMPT_VERSION: ANALYSIS_PROJECT,
    "v2": V2_ANALYSIS_PROJECT,
}
RECONNAISSANCE_PROMPT = "ANALYZE_RECONNAISSANCE"
QUESTIONS_VARIABLE = "QUESTIONS"
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
REPORT_PROMPTS = (*SPECIALIST_PROMPTS, COMPARISON_PROMPT)
ANALYSIS_PROMPTS = (RECONNAISSANCE_PROMPT, *REPORT_PROMPTS)
WORKSPACE_README_APPEND_VARIABLE = "ADD_TO_README_MD"


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
        "--prompt-version",
        choices=tuple(PROMPT_PROJECT_BY_VERSION),
        default=DEFAULT_PROMPT_VERSION,
        help="select the original or v2 on-disk prompt project",
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
    project_name: str = ANALYSIS_PROJECT,
) -> dict[str, PromptDefinition]:
    prompts = {
        prompt.prompt_name: prompt
        for project in projects
        if project.project_name == project_name
        for prompt in project.prompts
        if prompt.prompt_name in ANALYSIS_PROMPTS
    }
    missing = [name for name in ANALYSIS_PROMPTS if name not in prompts]
    if missing:
        raise InitializationError(
            f"Configuration must define {project_name} prompts: "
            f"{', '.join(missing)}"
        )
    return {name: prompts[name] for name in ANALYSIS_PROMPTS}


def _analysis_variable_definitions(
    project_name: str,
) -> tuple[VariableDefinition, ...]:
    """Combine disjoint shared variables with one project's specialist lenses."""

    shared_variables = load_variable_definitions(VARIABLES_ROOT)
    project_variables = load_variable_definitions(
        CONFIG_ROOT / project_name / "vars"
    )
    expected_project_names = set(SPECIALIST_VARIABLE_BY_PROMPT.values())
    observed_project_names = {
        variable.variable_name for variable in project_variables
    }
    missing = sorted(expected_project_names - observed_project_names)
    unexpected = sorted(observed_project_names - expected_project_names)
    if missing or unexpected:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected {', '.join(unexpected)}")
        raise InitializationError(
            f"{CONFIG_ROOT / project_name / 'vars'}: project specialist variables "
            f"are invalid ({'; '.join(details)})"
        )
    shared_names = {variable.variable_name for variable in shared_variables}
    collisions = sorted(shared_names & observed_project_names)
    if collisions:
        raise InitializationError(
            f"{CONFIG_ROOT / project_name / 'vars'}: project specialist variables "
            f"conflict with shared variables: {', '.join(collisions)}"
        )
    variables_by_name = {
        variable.variable_name: variable for variable in shared_variables
    }
    variables_by_name.update(
        {variable.variable_name: variable for variable in project_variables}
    )
    return tuple(variables_by_name[name] for name in sorted(variables_by_name))


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
    """Require the agent's small final-message handoff."""

    output = review.get("output")
    if not isinstance(output, str) or not output.strip():
        raise InitializationError(
            f"Prompt Runner returned no report for "
            f"{prompt.project_name}/{prompt.prompt_name}"
        )
    return output


def _final_output_produced(
    review: dict[str, Any], prompt: PromptDefinition
) -> str:
    """Return one Markdown report or fall back to the agent's final turn."""

    workspace_value = review.get("isolated_workspace")
    if not isinstance(workspace_value, str) or not workspace_value:
        return _require_report_output(review, prompt)
    workspace = Path(workspace_value)
    output_directory = workspace / OUTPUT_DIRECTORY_NAME
    try:
        if output_directory.is_symlink() or not output_directory.is_dir():
            raise InitializationError(
                f"Prompt produced no real {OUTPUT_DIRECTORY_NAME}/ directory for "
                f"{prompt.project_name}/{prompt.prompt_name}"
            )
        markdown_paths = sorted(
            path
            for path in output_directory.iterdir()
            if path.name.lower().endswith(".md")
        )
    except OSError as error:
        raise InitializationError(
            f"Cannot scan {OUTPUT_DIRECTORY_NAME}/ for "
            f"{prompt.project_name}/{prompt.prompt_name}: {error}"
        ) from error
    if len(markdown_paths) != 1:
        if not markdown_paths:
            return _require_report_output(review, prompt)
        raise InitializationError(
            f"Prompt must produce exactly one Markdown file in "
            f"{output_directory}; found {len(markdown_paths)}"
        )
    report_path = markdown_paths[0]
    if report_path.is_symlink() or not report_path.is_file():
        raise InitializationError(
            f"Prompt report must be a regular file inside {output_directory}: "
            f"{report_path.name}"
        )
    try:
        report = report_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise InitializationError(
            f"Cannot read prompt report {report_path}: {error}"
        ) from error
    if not report.strip():
        raise InitializationError(f"Prompt report is empty: {report_path}")
    return report


def _extract_specialist_question_blocks(
    reconnaissance_output: str,
) -> dict[str, str]:
    """Extract exactly one complete reconnaissance block per specialist."""

    question_blocks: dict[str, str] = {}
    errors: list[str] = []
    for prompt_name in SPECIALIST_PROMPTS:
        opening_tag = f"<{prompt_name}>"
        closing_tag = f"</{prompt_name}>"
        opening_count = reconnaissance_output.count(opening_tag)
        closing_count = reconnaissance_output.count(closing_tag)
        if opening_count != 1 or closing_count != 1:
            errors.append(
                f"{prompt_name} has {opening_count} opening and "
                f"{closing_count} closing tags"
            )
            continue
        block_start = reconnaissance_output.index(opening_tag)
        content_start = block_start + len(opening_tag)
        block_end = reconnaissance_output.find(closing_tag, content_start)
        if block_end == -1:
            errors.append(f"{prompt_name} closes before its questions block opens")
            continue
        if not reconnaissance_output[content_start:block_end].strip():
            errors.append(f"{prompt_name} has an empty questions block")
            continue
        question_blocks[prompt_name] = reconnaissance_output[
            block_start : block_end + len(closing_tag)
        ].strip()
    if errors:
        raise InitializationError(
            "Reconnaissance did not produce exactly one non-empty questions block "
            f"for every specialist: {'; '.join(errors)}"
        )
    return question_blocks


def _require_pipeline_variable_contract(
    prompts: dict[str, PromptDefinition], variables: dict[str, str]
) -> None:
    """Validate stage-visible variables before any prompt execution."""

    output_variable_by_prompt = {
        prompt_name: prompt_output_variable_name(prompt_name)
        for prompt_name in REPORT_PROMPTS
    }
    reserved_runtime_variables = {
        *output_variable_by_prompt.values(),
        QUESTIONS_VARIABLE,
    }
    collisions = sorted(variables.keys() & reserved_runtime_variables)
    if collisions:
        raise InitializationError(
            "Pipeline variables are generated at runtime and cannot be supplied: "
            f"{', '.join(collisions)}"
        )

    base_variables = set(variables)
    available_by_prompt = {
        RECONNAISSANCE_PROMPT: base_variables,
        **{
            prompt_name: base_variables | {QUESTIONS_VARIABLE}
            for prompt_name in SPECIALIST_PROMPTS
        },
    }
    available_by_prompt[COMPARISON_PROMPT] = base_variables | {
        output_variable_by_prompt[prompt_name] for prompt_name in SPECIALIST_PROMPTS
    }

    for prompt_name in ANALYSIS_PROMPTS:
        referenced_variables = variable_reference_names(prompts[prompt_name].template)
        missing = sorted(referenced_variables - available_by_prompt[prompt_name])
        if missing:
            raise InitializationError(
                f"{prompts[prompt_name].source_path}: prompt {prompt_name} references "
                f"variables unavailable at its pipeline stage: {', '.join(missing)}"
            )
        if (
            prompt_name in SPECIALIST_PROMPTS
            and QUESTIONS_VARIABLE not in referenced_variables
        ):
            raise InitializationError(
                f"{prompts[prompt_name].source_path}: prompt {prompt_name} must "
                f"reference {QUESTIONS_VARIABLE}"
            )


def _workspace_readmes(
    prompts: dict[str, PromptDefinition], variables: dict[str, str]
) -> dict[str, str]:
    """Build the prompt-specific context copied into isolated workspaces."""

    readme_appendix = variables.get(WORKSPACE_README_APPEND_VARIABLE)
    if readme_appendix is None:
        raise InitializationError(
            f"Missing workspace README variable: {WORKSPACE_README_APPEND_VARIABLE}"
        )
    prompt_context = {
        RECONNAISSANCE_PROMPT: variables["RECONNAISSANCE_SPECIALIST"],
        **{
            prompt_name: variables[variable_name]
            for prompt_name, variable_name in SPECIALIST_VARIABLE_BY_PROMPT.items()
        },
        COMPARISON_PROMPT: prompts[COMPARISON_PROMPT].template,
    }
    return {
        prompt_name: f"{context.rstrip()}\n\n{readme_appendix.strip()}"
        for prompt_name, context in prompt_context.items()
    }


def _doctor_analysis_configuration(
    projects: tuple[ProjectDefinition, ...],
    review_directory: Path,
    *,
    reserved_runtime_variables: set[str],
) -> dict[str, tuple[dict[str, PromptDefinition], dict[str, str]]]:
    """Validate every selectable prompt project before external state changes."""

    configurations = {}
    for prompt_version, project_name in PROMPT_PROJECT_BY_VERSION.items():
        prompts = _analysis_prompt_definitions(projects, project_name)
        variables = compose_variable_values(
            _analysis_variable_definitions(project_name),
            review_directory,
            reserved_runtime_variables=reserved_runtime_variables,
        )
        _require_pipeline_variable_contract(prompts, variables)
        _workspace_readmes(prompts, variables)
        configurations[prompt_version] = (prompts, variables)
    return configurations


def _run_review_pipeline(
    runner: ReviewExecutionGateway,
    prompts: dict[str, PromptDefinition],
    *,
    variables: dict[str, str],
    working_directory: Path,
    overrides: ReviewExecutionOverrides = DEFAULT_EXECUTION_OVERRIDES,
    project_name: str = ANALYSIS_PROJECT,
) -> ReviewPipelineResult:
    """Generate questions, run specialists concurrently, then audit reports."""

    _require_pipeline_variable_contract(prompts, variables)
    reconnaissance_prompt = prompts[RECONNAISSANCE_PROMPT]
    reconnaissance = runner.run_prompt(
        project_name,
        RECONNAISSANCE_PROMPT,
        variables=variables,
        working_directory=working_directory,
        model=overrides.model,
        reasoning_effort=overrides.reasoning_effort,
    )
    _require_expected_execution(reconnaissance, reconnaissance_prompt, overrides)
    reconnaissance_output = _final_output_produced(
        reconnaissance, reconnaissance_prompt
    )
    question_blocks = _extract_specialist_question_blocks(reconnaissance_output)

    specialist_reviews: dict[str, dict[str, Any]] = {}
    prompt_output_variables: dict[str, str] = {}
    failures: dict[str, str] = {}
    with ThreadPoolExecutor(
        max_workers=len(SPECIALIST_PROMPTS), thread_name_prefix="code-review"
    ) as executor:
        futures = {
            executor.submit(
                runner.run_prompt,
                project_name,
                prompt_name,
                variables={
                    **variables,
                    QUESTIONS_VARIABLE: question_blocks[prompt_name],
                },
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
                final_output_produced = _final_output_produced(review, prompt)
            except InitializationError as error:
                failures[prompt_name] = f"{type(error).__name__}: {error}"
            else:
                specialist_reviews[prompt_name] = review
                prompt_output_variables[prompt_output_variable_name(prompt_name)] = (
                    final_output_produced
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
        project_name,
        COMPARISON_PROMPT,
        variables=comparison_variables,
        working_directory=working_directory,
        model=overrides.model,
        reasoning_effort=overrides.reasoning_effort,
    )
    comparison_prompt = prompts[COMPARISON_PROMPT]
    _require_expected_execution(comparison, comparison_prompt, overrides)
    prompt_output_variables[prompt_output_variable_name(COMPARISON_PROMPT)] = (
        _final_output_produced(comparison, comparison_prompt)
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
        prompt_output_variable_name(prompt_name) for prompt_name in REPORT_PROMPTS
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
    prompt_project = PROMPT_PROJECT_BY_VERSION[arguments.prompt_version]
    overrides = ReviewExecutionOverrides(
        model=arguments.model,
        reasoning_effort=arguments.reasoning_effort,
    )
    try:
        review_directory = _review_directory(arguments.directory)
        reports_root = _reports_root(arguments.reports_directory)
        _enter_review_directory(review_directory)
        projects = load_project_definitions(CONFIG_ROOT)
        reserved_prompt_output_variables = {
            prompt_output_variable_name(prompt.prompt_name)
            for project in projects
            for prompt in project.prompts
        }
        configurations = _doctor_analysis_configuration(
            projects,
            review_directory,
            reserved_runtime_variables={
                *reserved_prompt_output_variables,
                QUESTIONS_VARIABLE,
            },
        )
        analysis_prompts, variable_values = configurations[arguments.prompt_version]
        configured_state_root = os.environ.get("CODEX_PROMPT_RUNNER_STATE_ROOT")
        runner_state_root = (
            Path(configured_state_root).expanduser()
            if configured_state_root
            else None
        )
        runner = PromptRunnerLibrary(
            runner_state_root,
            live_event_handler=partial(
                create_runner_work_space_from_event,
                _workspace_readmes(analysis_prompts, variable_values),
                review_directory,
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
            project_name=prompt_project,
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
        "prompt_version": arguments.prompt_version,
        "prompt_project": prompt_project,
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
            variable_values.keys()
            | pipeline.prompt_output_variables.keys()
            | {QUESTIONS_VARIABLE}
        ),
        "runtime_variables": {ARG_DIRECTORY_VARIABLE: str(review_directory)},
        "prompt_runner_policy": {
            "attempt_timeout_seconds": PROMPT_RUNNER_ATTEMPT_TIMEOUT_SECONDS,
            "retry_delays_seconds": list(PROMPT_RUNNER_RETRY_DELAYS_SECONDS),
        },
        "specialist_reviews": {
            prompt_name: _review_summary(prompt_name, review)
            for prompt_name, review in pipeline.specialist_reviews.items()
        },
        "review": _review_summary(COMPARISON_PROMPT, pipeline.comparison_review),
    }
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0
