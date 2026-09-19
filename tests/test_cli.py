from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event, Lock
from typing import Any

import pytest

from codex_ai_code_reviewer.cli import (
    ANALYSIS_PROJECT,
    ANALYSIS_PROMPTS,
    COMPARISON_PROMPT,
    CONFIG_ROOT,
    RESULT_SCHEMA,
    SPECIALIST_PROMPTS,
    SPECIALIST_VARIABLE_BY_PROMPT,
    VARIABLES_ROOT,
    ReviewExecutionOverrides,
    _analysis_prompt_definitions,
    _enter_review_directory,
    _parser,
    _publish_reports,
    _require_expected_execution,
    _require_pipeline_variable_contract,
    _require_workspace_report_output,
    _review_directory,
    _review_run_id,
    _run_review_pipeline,
    _workspace_readmes,
)
from codex_ai_code_reviewer.initialization import (
    InitializationError,
    ProjectDefinition,
    PromptDefinition,
    compose_variable_values,
    load_project_definitions,
    load_variable_definitions,
    prompt_output_variable_name,
    variable_reference_names,
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


def _successful_review(
    output: str,
    *,
    model: str = "gpt-6-astra",
    reasoning_effort: str = "xhigh",
) -> dict[str, Any]:
    return {
        "delivery_mode": "LIVE",
        "model": model,
        "output": output,
        "reasoning_effort": reasoning_effort,
        "risk_profile": "BALANCED",
    }


class ParallelReviewRunner:
    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root
        self._specialist_start = Barrier(len(SPECIALIST_PROMPTS))
        self._lock = Lock()
        self._completion_order = tuple(reversed(SPECIALIST_PROMPTS))
        self._completion_events = {
            prompt_name: Event() for prompt_name in SPECIALIST_PROMPTS
        }
        self.calls: list[str] = []
        self.execution_options: list[tuple[str, str | None, str | None]] = []
        self.comparison_variables: dict[str, str] | None = None
        self.specialist_completion_order: list[str] = []

    def run_prompt(
        self,
        project_name: str,
        prompt_name: str,
        *,
        variables: dict[str, str],
        working_directory: Path,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        assert project_name == ANALYSIS_PROJECT
        assert working_directory.is_dir()
        with self._lock:
            self.calls.append(prompt_name)
            self.execution_options.append((prompt_name, model, reasoning_effort))
        workspace = self._workspace_root / prompt_name
        output_directory = workspace / "outputs"
        output_directory.mkdir(parents=True)
        if prompt_name in SPECIALIST_PROMPTS:
            self._specialist_start.wait(timeout=2)
            completion_index = self._completion_order.index(prompt_name)
            if completion_index:
                previous_prompt = self._completion_order[completion_index - 1]
                assert self._completion_events[previous_prompt].wait(timeout=2)
            report = (
                f"Detailed report from {prompt_name}; "
                "literal {{VAR:ARG_DIRECTORY}}"
            )
            (output_directory / "specialist-review.md").write_text(
                report, encoding="utf-8"
            )
            review = _successful_review(
                f"Handoff from {prompt_name}",
                model=model or "gpt-6-astra",
                reasoning_effort=reasoning_effort or "xhigh",
            )
            review["isolated_workspace"] = str(workspace)
            with self._lock:
                self.specialist_completion_order.append(prompt_name)
            self._completion_events[prompt_name].set()
            return review
        assert prompt_name == COMPARISON_PROMPT
        self.comparison_variables = variables
        (output_directory / "integrated-review.md").write_text(
            "Integrated detailed review", encoding="utf-8"
        )
        review = _successful_review(
            "Audited final review",
            model=model or "gpt-6-astra",
            reasoning_effort=reasoning_effort or "xhigh",
        )
        review["isolated_workspace"] = str(workspace)
        return review


@pytest.mark.parametrize(
    "argv",
    [
        ["--model", "gpt-5.6-luna", "--reasoning", "max", "/project"],
        ["/project", "--model", "gpt-5.6-luna", "--reasoning", "max"],
    ],
)
def test_parser_accepts_execution_overrides_before_or_after_directory(
    argv: list[str],
) -> None:
    arguments = _parser().parse_args(argv)

    assert arguments.directory == Path("/project")
    assert arguments.model == "gpt-5.6-luna"
    assert arguments.reasoning_effort == "max"
    assert arguments.reports_directory.name == "reports"


def test_review_directory_resolves_an_existing_readable_directory(
    tmp_path: Path,
) -> None:
    assert _review_directory(tmp_path) == tmp_path.resolve()


def test_review_run_id_is_utc_and_lexically_sortable() -> None:
    run_ids = [
        _review_run_id(0),
        _review_run_id(10_000_000),
        _review_run_id(1_000_000_000),
    ]

    assert run_ids == [
        "1970-01-01T00-00-00.00Z",
        "1970-01-01T00-00-00.01Z",
        "1970-01-01T00-00-01.00Z",
    ]
    assert sorted(run_ids) == run_ids


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


def test_enter_review_directory_changes_the_python_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    starting_directory = tmp_path / "starting"
    review_directory = tmp_path / "review"
    starting_directory.mkdir()
    review_directory.mkdir()
    monkeypatch.chdir(starting_directory)

    _enter_review_directory(review_directory)

    assert Path.cwd() == review_directory


def test_shell_launcher_selects_the_reviewer_project_from_a_sibling_target(
    tmp_path: Path,
) -> None:
    reviewer_root = Path(__file__).parents[1].resolve()
    review_directory = tmp_path / "sibling project"
    fake_bin = tmp_path / "bin"
    observation = tmp_path / "launcher-observation"
    review_directory.mkdir()
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$PWD" >"$LAUNCHER_OBSERVATION"\n'
        'printf \'%s\\n\' "$@" >>"$LAUNCHER_OBSERVATION"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    environment = os.environ | {
        "LAUNCHER_OBSERVATION": str(observation),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }

    completed = subprocess.run(
        [reviewer_root / "run_the_code_review", review_directory],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    assert observation.read_text(encoding="utf-8").splitlines() == [
        str(review_directory.resolve()),
        "run",
        "--project",
        str(reviewer_root),
        "python",
        str(reviewer_root / "perform_a_code_review.py"),
        str(review_directory.resolve()),
    ]


@pytest.mark.parametrize("options_first", [True, False])
def test_shell_launcher_forwards_overrides_in_either_position(
    tmp_path: Path,
    options_first: bool,
) -> None:
    reviewer_root = Path(__file__).parents[1].resolve()
    review_directory = tmp_path / "target"
    fake_bin = tmp_path / "bin"
    observation = tmp_path / "launcher-observation"
    review_directory.mkdir()
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$PWD" >"$LAUNCHER_OBSERVATION"\n'
        'printf \'%s\\n\' "$@" >>"$LAUNCHER_OBSERVATION"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    environment = os.environ | {
        "LAUNCHER_OBSERVATION": str(observation),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    options = ["--model", "gpt-5.6-luna", "--reasoning", "max"]
    arguments = (
        [*options, str(review_directory)]
        if options_first
        else [str(review_directory), *options]
    )

    completed = subprocess.run(
        [reviewer_root / "run_the_code_review", *arguments],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    forwarded = observation.read_text(encoding="utf-8").splitlines()
    assert forwarded[:6] == [
        str(review_directory.resolve()),
        "run",
        "--project",
        str(reviewer_root),
        "python",
        str(reviewer_root / "perform_a_code_review.py"),
    ]
    expected_arguments = (
        [*options, str(review_directory.resolve())]
        if options_first
        else [str(review_directory.resolve()), *options]
    )
    assert forwarded[6:] == expected_arguments


@pytest.mark.parametrize("inline", [True, False])
def test_shell_launcher_resolves_reports_directory_before_entering_target(
    tmp_path: Path,
    inline: bool,
) -> None:
    reviewer_root = Path(__file__).parents[1].resolve()
    review_directory = tmp_path / "target"
    fake_bin = tmp_path / "bin"
    observation = tmp_path / "launcher-observation"
    review_directory.mkdir()
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$@" >"$LAUNCHER_OBSERVATION"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    environment = os.environ | {
        "LAUNCHER_OBSERVATION": str(observation),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    reports_option = (
        ["--reports-directory=relative-reports"]
        if inline
        else ["--reports-directory", "relative-reports"]
    )

    completed = subprocess.run(
        [reviewer_root / "run_the_code_review", *reports_option, review_directory],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    forwarded = observation.read_text(encoding="utf-8").splitlines()[5:]
    expected_reports_directory = tmp_path / "relative-reports"
    expected_option = (
        [f"--reports-directory={expected_reports_directory}"]
        if inline
        else ["--reports-directory", str(expected_reports_directory)]
    )
    assert forwarded == [*expected_option, str(review_directory)]


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


def test_execution_override_becomes_the_expected_runtime_policy(tmp_path: Path) -> None:
    prompt = _prompt_definitions(tmp_path)["ANALYZE_PIPELINE"]
    overrides = ReviewExecutionOverrides(model="gpt-5.6-luna", reasoning_effort="max")
    result = _successful_review("review", model="gpt-5.6-luna", reasoning_effort="max")

    _require_expected_execution(result, prompt, overrides)

    result["model"] = prompt.model
    with pytest.raises(InitializationError, match="configured policy"):
        _require_expected_execution(result, prompt, overrides)


def test_workspace_report_is_the_authoritative_prompt_output(tmp_path: Path) -> None:
    prompt = _prompt_definitions(tmp_path)["ANALYZE_PIPELINE"]
    workspace = tmp_path / "isolated-workspace"
    outputs = workspace / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "probe-results.jsonl").write_text("{}\n", encoding="utf-8")
    (outputs / "pipeline-review.md").write_text(
        "Complete pipeline report\n", encoding="utf-8"
    )
    review = _successful_review("Small completion handoff")
    review["isolated_workspace"] = str(workspace)

    assert (
        _require_workspace_report_output(review, prompt)
        == "Complete pipeline report\n"
    )


@pytest.mark.parametrize(
    ("reports", "message"),
    [
        ({}, "found 0"),
        ({"one.md": "one", "two.md": "two"}, "found 2"),
        ({"empty.md": "  \n"}, "is empty"),
    ],
)
def test_workspace_report_requires_exactly_one_nonempty_markdown_file(
    tmp_path: Path, reports: dict[str, str], message: str
) -> None:
    prompt = _prompt_definitions(tmp_path)["ANALYZE_PIPELINE"]
    workspace = tmp_path / "isolated-workspace"
    outputs = workspace / "outputs"
    outputs.mkdir(parents=True)
    for name, content in reports.items():
        (outputs / name).write_text(content, encoding="utf-8")
    review = _successful_review("Small completion handoff")
    review["isolated_workspace"] = str(workspace)

    with pytest.raises(InitializationError, match=message):
        _require_workspace_report_output(review, prompt)


def test_workspace_report_rejects_a_markdown_symlink(tmp_path: Path) -> None:
    prompt = _prompt_definitions(tmp_path)["ANALYZE_PIPELINE"]
    workspace = tmp_path / "isolated-workspace"
    outputs = workspace / "outputs"
    outputs.mkdir(parents=True)
    outside_report = tmp_path / "outside.md"
    outside_report.write_text("outside", encoding="utf-8")
    (outputs / "report.md").symlink_to(outside_report)
    review = _successful_review("Small completion handoff")
    review["isolated_workspace"] = str(workspace)

    with pytest.raises(InitializationError, match="regular file"):
        _require_workspace_report_output(review, prompt)


@pytest.mark.parametrize("missing_prompt", ANALYSIS_PROMPTS)
def test_analysis_configuration_requires_every_pipeline_prompt(
    tmp_path: Path, missing_prompt: str
) -> None:
    prompts = _prompt_definitions(tmp_path)
    project = ProjectDefinition(
        ANALYSIS_PROJECT,
        tuple(prompts[name] for name in ANALYSIS_PROMPTS if name != missing_prompt),
    )

    with pytest.raises(InitializationError, match=missing_prompt):
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
    base_directory_context = values["BASE_CRITICAL_DIRECTORY"]
    context = values["REVIEW_DIRECTORY_CONTEXT"]
    assert base_directory_context in context
    assert base_directory_context.count(str(tmp_path)) == 1
    assert context.count(str(tmp_path)) == 2
    assert "{{VAR:" not in base_directory_context
    assert str(tmp_path) in context
    assert "{{VAR:" not in context


def test_repository_prompts_apply_the_intended_execution_profiles() -> None:
    prompts = _analysis_prompt_definitions(load_project_definitions(CONFIG_ROOT))

    assert {
        prompt_name: prompt.risk_profile for prompt_name, prompt in prompts.items()
    } == {
        "ANALYZE_PIPELINE": "BALANCED",
        "ANALYZE_BOUNDARIES": "BALANCED",
        "ANALYZE_NETWORKING": "NETWORKED_WORKSPACE",
        "ANALYZE_INTEGRITY": "BALANCED",
        "ANALYZE_SECURITY": "NETWORKED_WORKSPACE",
        "ANALYZE_PERFORMANCE": "BALANCED",
        "COMPARE_AGENT_REPORTS": "NETWORKED_WORKSPACE",
    }
    assert {(prompt.model, prompt.reasoning_effort) for prompt in prompts.values()} == {
        ("gpt-6-astra", "xhigh")
    }


def test_repository_specialists_use_their_named_lens_variables() -> None:
    prompts = _analysis_prompt_definitions(load_project_definitions(CONFIG_ROOT))

    for prompt_name, lens_variable in SPECIALIST_VARIABLE_BY_PROMPT.items():
        expected_variables = {
            "REVIEW_DIRECTORY_CONTEXT",
            "ANALYZE_HEADER",
            lens_variable,
        }
        assert variable_reference_names(prompts[prompt_name].template) == (
            expected_variables
        )


def test_workspace_readmes_use_specialist_context_and_comparison_prompt(
    tmp_path: Path,
) -> None:
    prompts = _analysis_prompt_definitions(load_project_definitions(CONFIG_ROOT))
    variables = compose_variable_values(
        load_variable_definitions(VARIABLES_ROOT), tmp_path
    )

    readmes = _workspace_readmes(prompts, variables)

    assert set(readmes) == set(ANALYSIS_PROMPTS)
    for prompt_name, variable_name in SPECIALIST_VARIABLE_BY_PROMPT.items():
        assert readmes[prompt_name] == variables[variable_name]
    assert readmes[COMPARISON_PROMPT] == prompts[COMPARISON_PROMPT].template


def test_comparison_prompt_uses_named_specialist_output_variables() -> None:
    prompts = _analysis_prompt_definitions(load_project_definitions(CONFIG_ROOT))

    assert variable_reference_names(prompts[COMPARISON_PROMPT].template) == {
        "REVIEW_DIRECTORY_CONTEXT",
        *(prompt_output_variable_name(name) for name in SPECIALIST_PROMPTS),
    }


def test_review_pipeline_runs_specialists_in_parallel_then_compares_reports(
    tmp_path: Path,
) -> None:
    runner = ParallelReviewRunner(tmp_path)
    prompts = _prompt_definitions(tmp_path)

    result = _run_review_pipeline(
        runner,
        prompts,
        variables={"ARG_DIRECTORY": str(tmp_path)},
        working_directory=tmp_path,
    )

    assert tuple(result.specialist_reviews) == SPECIALIST_PROMPTS
    assert len(result.specialist_reviews) == 6
    assert "ANALYZE_RECONNAISSANCE" not in runner.calls
    assert result.comparison_review["output"] == "Audited final review"
    assert runner.calls[-1] == COMPARISON_PROMPT
    assert runner.specialist_completion_order == list(reversed(SPECIALIST_PROMPTS))
    assert runner.comparison_variables is not None
    for prompt_name in SPECIALIST_PROMPTS:
        variable_name = prompt_output_variable_name(prompt_name)
        assert (
            runner.comparison_variables[variable_name]
            == f"Detailed report from {prompt_name}; literal "
            "{{VAR:ARG_DIRECTORY}}"
        )
    assert not any(
        variable_name.startswith("AGENT_")
        for variable_name in runner.comparison_variables
    )
    assert result.prompt_output_variables == {
        **{
            prompt_output_variable_name(prompt_name): (
                f"Detailed report from {prompt_name}; "
                "literal {{VAR:ARG_DIRECTORY}}"
            )
            for prompt_name in SPECIALIST_PROMPTS
        },
        "COMPARE_AGENT_REPORTS_OUTPUT": "Integrated detailed review",
    }


def test_pipeline_rejects_a_misspelled_or_unavailable_output_reference(
    tmp_path: Path,
) -> None:
    prompts = _prompt_definitions(tmp_path)
    prompts[COMPARISON_PROMPT] = replace(
        prompts[COMPARISON_PROMPT],
        template="{{VAR:ANALYZE_PIPELINE_OUTPUTT}}",
    )
    runner = ParallelReviewRunner(tmp_path)

    with pytest.raises(
        InitializationError,
        match="ANALYZE_PIPELINE_OUTPUTT",
    ):
        _run_review_pipeline(
            runner,
            prompts,
            variables={"ARG_DIRECTORY": str(tmp_path)},
            working_directory=tmp_path,
        )

    assert runner.calls == []


def test_pipeline_rejects_caller_supplied_prompt_output(tmp_path: Path) -> None:
    prompts = _prompt_definitions(tmp_path)

    with pytest.raises(InitializationError, match="generated at runtime"):
        _require_pipeline_variable_contract(
            prompts,
            {
                "ARG_DIRECTORY": str(tmp_path),
                "ANALYZE_PIPELINE_OUTPUT": "spoofed or stale report",
            },
        )


def test_review_pipeline_applies_overrides_to_every_execution(tmp_path: Path) -> None:
    runner = ParallelReviewRunner(tmp_path)

    _run_review_pipeline(
        runner,
        _prompt_definitions(tmp_path),
        variables={"ARG_DIRECTORY": str(tmp_path)},
        working_directory=tmp_path,
        overrides=ReviewExecutionOverrides(
            model="gpt-5.6-luna", reasoning_effort="max"
        ),
    )

    assert sorted(runner.execution_options) == sorted(
        (prompt_name, "gpt-5.6-luna", "max") for prompt_name in ANALYSIS_PROMPTS
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
            model: str | None = None,
            reasoning_effort: str | None = None,
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


def test_reports_are_published_under_project_and_run_directories(
    tmp_path: Path,
) -> None:
    review_directory = tmp_path / "PROJECT NAME"
    reports_root = tmp_path / "reports"
    review_directory.mkdir()
    outputs = {
        prompt_output_variable_name(prompt_name): f"Report from {prompt_name}"
        for prompt_name in ANALYSIS_PROMPTS
    }

    publication = _publish_reports(
        outputs,
        review_directory=review_directory,
        reports_root=reports_root,
        run_id="2026-09-19T12-00-00.12Z",
    )

    assert publication.project_name == "PROJECT_NAME"
    assert publication.run_id == "2026-09-19T12-00-00.12Z"
    assert publication.report_directory == (
        reports_root / "PROJECT_NAME" / publication.run_id
    )
    assert set(publication.report_paths) == set(outputs)
    assert {path.name for path in publication.report_paths.values()} == {
        f"{output_name}.md" for output_name in outputs
    }
    for output_name, report_path in publication.report_paths.items():
        assert report_path.read_text(encoding="utf-8") == f"{outputs[output_name]}\n"
    assert [path.name for path in publication.report_directory.parent.iterdir()] == [
        publication.run_id
    ]

    with pytest.raises(InitializationError, match="already exists"):
        _publish_reports(
            {name: f"replacement {content}" for name, content in outputs.items()},
            review_directory=review_directory,
            reports_root=reports_root,
            run_id=publication.run_id,
        )

    for output_name, report_path in publication.report_paths.items():
        assert report_path.read_text(encoding="utf-8") == f"{outputs[output_name]}\n"


def test_failed_report_publication_removes_staging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review_directory = tmp_path / "PROJECT"
    reports_root = tmp_path / "reports"
    review_directory.mkdir()
    outputs = {
        prompt_output_variable_name(prompt_name): prompt_name
        for prompt_name in ANALYSIS_PROMPTS
    }

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("simulated publication failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(InitializationError, match="simulated publication failure"):
        _publish_reports(
            outputs,
            review_directory=review_directory,
            reports_root=reports_root,
            run_id="2026-09-19T12-00-01.12Z",
        )

    assert list((reports_root / "PROJECT").iterdir()) == []


def test_machine_readable_result_contract_tracks_the_pipeline() -> None:
    schema_path = (
        Path(__file__).parents[1] / "docs/contracts/code-review-result.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["properties"]["schema"]["const"] == RESULT_SCHEMA
    assert "final_review_path" not in schema["properties"]
    assert {
        "report_project_name",
        "report_run_id",
        "report_directory",
        "report_paths",
    } <= set(schema["required"])
    specialist_contract = schema["properties"]["specialist_reviews"]
    assert tuple(specialist_contract["required"]) == SPECIALIST_PROMPTS
    assert set(specialist_contract["properties"]) == set(SPECIALIST_PROMPTS)
    report_paths_contract = schema["properties"]["report_paths"]
    expected_output_names = {
        prompt_output_variable_name(prompt_name) for prompt_name in ANALYSIS_PROMPTS
    }
    assert set(report_paths_contract["required"]) == expected_output_names
    assert set(report_paths_contract["properties"]) == expected_output_names
