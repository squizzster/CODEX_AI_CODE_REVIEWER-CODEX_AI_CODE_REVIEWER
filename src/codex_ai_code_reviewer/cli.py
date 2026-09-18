"""Command-line entry point for config initialization and future review execution."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from codex_ai_code_reviewer.initialization import (
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
ANALYSIS_PROJECT = "CODEX_AI_CODE_REVIEW"
ANALYSIS_PROMPT = "ANALYZE_PIPELINE"


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


def _analysis_prompt_definition(
    projects: tuple[ProjectDefinition, ...],
) -> PromptDefinition:
    matches = [
        prompt
        for project in projects
        if project.project_name == ANALYSIS_PROJECT
        for prompt in project.prompts
        if prompt.prompt_name == ANALYSIS_PROMPT
    ]
    if len(matches) != 1:
        raise InitializationError(
            f"Configuration must define exactly one {ANALYSIS_PROJECT}/{ANALYSIS_PROMPT} prompt"
        )
    return matches[0]


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


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        review_directory = _review_directory(arguments.directory)
        projects = load_project_definitions(CONFIG_ROOT)
        analysis_prompt = _analysis_prompt_definition(projects)
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
        review = runner.run_prompt(
            ANALYSIS_PROJECT,
            ANALYSIS_PROMPT,
            variables=variable_values,
            working_directory=review_directory,
        )
        _require_expected_execution(review, analysis_prompt)
    except InitializationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    output = {
        "ok": True,
        "phase": "code_review_complete",
        "review_directory": str(review_directory),
        "catalog": report.as_dict(),
        "variables": sorted(variable_values),
        "runtime_variables": {ARG_DIRECTORY_VARIABLE: str(review_directory)},
        "review": {
            "attempts": review.get("attempts"),
            "delivery_mode": review.get("delivery_mode"),
            "execution_run_id": review.get("execution_run_id"),
            "model": review.get("model"),
            "output": review.get("output"),
            "reasoning_effort": review.get("reasoning_effort"),
            "request_id": review.get("request_id"),
            "result_id": review.get("result_id"),
            "risk_profile": review.get("risk_profile"),
            "usage": review.get("provenance", {}).get("usage"),
        },
    }
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0
