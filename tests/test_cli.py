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
    DEFAULT_PROMPT_VERSION,
    PROMPT_PROJECT_BY_VERSION,
    QUESTIONS_VARIABLE,
    RECONNAISSANCE_PROMPT,
    REPORT_PROMPTS,
    RESULT_SCHEMA,
    SPECIALIST_PROMPTS,
    SPECIALIST_VARIABLE_BY_PROMPT,
    V2_ANALYSIS_PROJECT,
    VARIABLES_ROOT,
    WORKSPACE_README_APPEND_VARIABLE,
    ReviewExecutionOverrides,
    _analysis_prompt_definitions,
    _analysis_variable_definitions,
    _doctor_analysis_configuration,
    _enter_review_directory,
    _extract_specialist_question_blocks,
    _final_output_produced,
    _parser,
    _publish_reports,
    _require_expected_execution,
    _require_pipeline_variable_contract,
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


def _prompt_definitions(
    tmp_path: Path, project_name: str = ANALYSIS_PROJECT
) -> dict[str, PromptDefinition]:
    return {
        prompt_name: PromptDefinition(
            project_name=project_name,
            prompt_name=prompt_name,
            source_path=tmp_path / f"{prompt_name}.yaml",
            template=(
                f"{{{{VAR:{QUESTIONS_VARIABLE}}}}}\nRun {prompt_name}"
                if prompt_name in SPECIALIST_PROMPTS
                else f"Run {prompt_name}"
            ),
            model="gpt-6-astra",
            reasoning_effort="xhigh",
            risk_profile="BALANCED",
        )
        for prompt_name in ANALYSIS_PROMPTS
    }


def _reconnaissance_output() -> str:
    return "\n\n".join(
        f"<{prompt_name}>\n{prompt_name} focused question?\n</{prompt_name}>"
        for prompt_name in SPECIALIST_PROMPTS
    )


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
    def __init__(
        self, workspace_root: Path, project_name: str = ANALYSIS_PROJECT
    ) -> None:
        self._workspace_root = workspace_root
        self._project_name = project_name
        self._specialist_start = Barrier(len(SPECIALIST_PROMPTS))
        self._lock = Lock()
        self._completion_order = tuple(reversed(SPECIALIST_PROMPTS))
        self._completion_events = {
            prompt_name: Event() for prompt_name in SPECIALIST_PROMPTS
        }
        self.calls: list[str] = []
        self.execution_options: list[tuple[str, str | None, str | None]] = []
        self.comparison_variables: dict[str, str] | None = None
        self.specialist_questions: dict[str, str] = {}
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
        assert project_name == self._project_name
        assert working_directory.is_dir()
        with self._lock:
            self.calls.append(prompt_name)
            self.execution_options.append((prompt_name, model, reasoning_effort))
        workspace = self._workspace_root / prompt_name
        output_directory = workspace / "outputs"
        output_directory.mkdir(parents=True)
        if prompt_name == RECONNAISSANCE_PROMPT:
            (output_directory / "reconnaissance.md").write_text(
                _reconnaissance_output(), encoding="utf-8"
            )
            review = _successful_review(
                "Reconnaissance handoff",
                model=model or "gpt-6-astra",
                reasoning_effort=reasoning_effort or "xhigh",
            )
            review["isolated_workspace"] = str(workspace)
            return review
        if prompt_name in SPECIALIST_PROMPTS:
            with self._lock:
                self.specialist_questions[prompt_name] = variables[QUESTIONS_VARIABLE]
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
        [
            "/project",
            "--prompt-version",
            "v2",
            "--model",
            "gpt-5.6-luna",
            "--reasoning",
            "max",
        ],
    ],
)
def test_parser_accepts_execution_overrides_before_or_after_directory(
    argv: list[str],
) -> None:
    arguments = _parser().parse_args(argv)

    assert arguments.directory == Path("/project")
    assert arguments.model == "gpt-5.6-luna"
    assert arguments.reasoning_effort == "max"
    assert arguments.prompt_version == (
        "v2" if "v2" in argv else DEFAULT_PROMPT_VERSION
    )
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
    options = [
        "--prompt-version",
        "v2",
        "--model",
        "gpt-5.6-luna",
        "--reasoning",
        "max",
    ]
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
        _final_output_produced(review, prompt)
        == "Complete pipeline report\n"
    )


def test_workspace_report_does_not_require_a_final_turn(tmp_path: Path) -> None:
    prompt = _prompt_definitions(tmp_path)["ANALYZE_PIPELINE"]
    workspace = tmp_path / "isolated-workspace"
    outputs = workspace / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "pipeline-review.md").write_text(
        "Complete file-backed report\n", encoding="utf-8"
    )
    review = _successful_review("")
    review["isolated_workspace"] = str(workspace)

    assert _final_output_produced(review, prompt) == "Complete file-backed report\n"


@pytest.mark.parametrize(
    ("reports", "message"),
    [
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
        _final_output_produced(review, prompt)


def test_workspace_report_falls_back_to_handoff_when_outputs_has_no_markdown(
    tmp_path: Path,
) -> None:
    prompt = _prompt_definitions(tmp_path)["ANALYZE_PIPELINE"]
    workspace = tmp_path / "isolated-workspace"
    outputs = workspace / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "probe-results.jsonl").write_text("{}\n", encoding="utf-8")
    review = _successful_review("Complete report returned as the last message")
    review["isolated_workspace"] = str(workspace)

    assert _final_output_produced(review, prompt) == review["output"]


def test_final_output_requires_markdown_or_a_final_turn(tmp_path: Path) -> None:
    prompt = _prompt_definitions(tmp_path)["ANALYZE_PIPELINE"]
    workspace = tmp_path / "isolated-workspace"
    outputs = workspace / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "probe-results.jsonl").write_text("{}\n", encoding="utf-8")
    review = _successful_review("")
    review["isolated_workspace"] = str(workspace)

    with pytest.raises(InitializationError, match="returned no report"):
        _final_output_produced(review, prompt)


def test_report_falls_back_to_handoff_without_an_isolated_workspace(
    tmp_path: Path,
) -> None:
    prompt = _prompt_definitions(tmp_path)["ANALYZE_PIPELINE"]
    review = _successful_review("Custom executor report")

    assert _final_output_produced(review, prompt) == "Custom executor report"


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
        _final_output_produced(review, prompt)


def test_reconnaissance_extracts_one_matching_block_per_specialist() -> None:
    output = f"Survey introduction\n\n{_reconnaissance_output()}\n\nSurvey limits"

    blocks = _extract_specialist_question_blocks(output)

    assert tuple(blocks) == SPECIALIST_PROMPTS
    for prompt_name, block in blocks.items():
        assert block == (
            f"<{prompt_name}>\n"
            f"{prompt_name} focused question?\n"
            f"</{prompt_name}>"
        )
        assert all(
            f"<{other_prompt}>" not in block
            for other_prompt in SPECIALIST_PROMPTS
            if other_prompt != prompt_name
        )


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("", "ANALYZE_PIPELINE has 0 opening and 0 closing tags"),
        (
            _reconnaissance_output()
            + "\n<ANALYZE_PIPELINE>duplicate?</ANALYZE_PIPELINE>",
            "ANALYZE_PIPELINE has 2 opening and 2 closing tags",
        ),
        (
            _reconnaissance_output().replace(
                "ANALYZE_INTEGRITY focused question?", "   "
            ),
            "ANALYZE_INTEGRITY has an empty questions block",
        ),
        (
            _reconnaissance_output().replace(
                "<ANALYZE_SECURITY>\nANALYZE_SECURITY focused question?\n"
                "</ANALYZE_SECURITY>",
                "</ANALYZE_SECURITY>\nANALYZE_SECURITY focused question?\n"
                "<ANALYZE_SECURITY>",
            ),
            "ANALYZE_SECURITY closes before its questions block opens",
        ),
    ],
)
def test_reconnaissance_rejects_missing_duplicate_or_empty_blocks(
    output: str, message: str
) -> None:
    with pytest.raises(InitializationError, match=message):
        _extract_specialist_question_blocks(output)


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


@pytest.mark.parametrize("project_name", PROMPT_PROJECT_BY_VERSION.values())
def test_repository_prompts_share_one_resolved_directory_context(
    tmp_path: Path, project_name: str
) -> None:
    prompts = _analysis_prompt_definitions(
        load_project_definitions(CONFIG_ROOT), project_name
    )
    configured_variables = _analysis_variable_definitions(project_name)

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


@pytest.mark.parametrize("project_name", PROMPT_PROJECT_BY_VERSION.values())
def test_repository_prompts_apply_the_intended_execution_profiles(
    project_name: str,
) -> None:
    prompts = _analysis_prompt_definitions(
        load_project_definitions(CONFIG_ROOT), project_name
    )

    assert {
        prompt_name: prompt.risk_profile for prompt_name, prompt in prompts.items()
    } == {
        "ANALYZE_RECONNAISSANCE": "BALANCED",
        "ANALYZE_PIPELINE": "BALANCED",
        "ANALYZE_BOUNDARIES": "BALANCED",
        "ANALYZE_NETWORKING": "NETWORKED_WORKSPACE",
        "ANALYZE_INTEGRITY": "BALANCED",
        "ANALYZE_SECURITY": "NETWORKED_WORKSPACE",
        "ANALYZE_PERFORMANCE": "BALANCED",
        "COMPARE_AGENT_REPORTS": "NETWORKED_WORKSPACE",
    }
    assert {
        prompt_name: (prompt.model, prompt.reasoning_effort)
        for prompt_name, prompt in prompts.items()
    } == {
        "ANALYZE_RECONNAISSANCE": ("gpt-6-astra", "xhigh"),
        "ANALYZE_PIPELINE": ("gpt-6-astra", "xhigh"),
        "ANALYZE_BOUNDARIES": ("gpt-6-astra", "xhigh"),
        "ANALYZE_NETWORKING": ("gpt-6-astra", "xhigh"),
        "ANALYZE_INTEGRITY": ("gpt-6-astra", "xhigh"),
        "ANALYZE_SECURITY": ("gpt-6-astra", "xhigh"),
        "ANALYZE_PERFORMANCE": ("gpt-6-astra", "xhigh"),
        "COMPARE_AGENT_REPORTS": ("gpt-6-astra", "max"),
    }


@pytest.mark.parametrize("project_name", PROMPT_PROJECT_BY_VERSION.values())
def test_repository_specialists_use_their_named_lens_variables(
    project_name: str,
) -> None:
    prompts = _analysis_prompt_definitions(
        load_project_definitions(CONFIG_ROOT), project_name
    )

    for prompt_name, lens_variable in SPECIALIST_VARIABLE_BY_PROMPT.items():
        expected_variables = {
            QUESTIONS_VARIABLE,
            "REVIEW_DIRECTORY_CONTEXT",
            "ANALYZE_HEADER",
            lens_variable,
        }
        assert variable_reference_names(prompts[prompt_name].template) == (
            expected_variables
        )


@pytest.mark.parametrize("project_name", PROMPT_PROJECT_BY_VERSION.values())
def test_reconnaissance_instructions_define_every_specialist_block(
    tmp_path: Path, project_name: str
) -> None:
    variables = compose_variable_values(
        _analysis_variable_definitions(project_name), tmp_path
    )
    instructions = variables["RECONNAISSANCE_SPECIALIST"]

    for prompt_name in SPECIALIST_PROMPTS:
        assert f"<{prompt_name}>" in instructions
        assert f"</{prompt_name}>" in instructions


@pytest.mark.parametrize("project_name", PROMPT_PROJECT_BY_VERSION.values())
def test_workspace_readmes_use_specialist_context_and_comparison_prompt(
    tmp_path: Path, project_name: str
) -> None:
    prompts = _analysis_prompt_definitions(
        load_project_definitions(CONFIG_ROOT), project_name
    )
    variables = compose_variable_values(
        _analysis_variable_definitions(project_name), tmp_path
    )

    readmes = _workspace_readmes(prompts, variables)

    assert set(readmes) == set(ANALYSIS_PROMPTS)
    appendix = variables[WORKSPACE_README_APPEND_VARIABLE].strip()
    assert readmes[RECONNAISSANCE_PROMPT] == (
        f"{variables['RECONNAISSANCE_SPECIALIST'].rstrip()}\n\n{appendix}"
    )
    for prompt_name, variable_name in SPECIALIST_VARIABLE_BY_PROMPT.items():
        assert readmes[prompt_name] == (
            f"{variables[variable_name].rstrip()}\n\n{appendix}"
        )
    assert readmes[COMPARISON_PROMPT] == (
        f"{prompts[COMPARISON_PROMPT].template.rstrip()}\n\n{appendix}"
    )


def test_original_and_v2_prompt_projects_are_identical_on_disk() -> None:
    original_root = CONFIG_ROOT / ANALYSIS_PROJECT
    v2_root = CONFIG_ROOT / V2_ANALYSIS_PROJECT
    prompt_names = {path.name for path in original_root.glob("*.yaml")}

    assert prompt_names == {path.name for path in v2_root.glob("*.yaml")}
    for prompt_name in prompt_names:
        assert (original_root / prompt_name).read_bytes() == (
            v2_root / prompt_name
        ).read_bytes()


def test_specialist_variables_are_owned_by_their_prompt_project() -> None:
    shared_names = {
        definition.variable_name
        for definition in load_variable_definitions(VARIABLES_ROOT)
    }
    original_variables = {
        definition.variable_name: definition.value
        for definition in load_variable_definitions(
            CONFIG_ROOT / ANALYSIS_PROJECT / "vars"
        )
    }
    v2_variables = {
        definition.variable_name: definition.value
        for definition in load_variable_definitions(
            CONFIG_ROOT / V2_ANALYSIS_PROJECT / "vars"
        )
    }

    assert set(original_variables) == set(SPECIALIST_VARIABLE_BY_PROMPT.values())
    assert set(v2_variables) == set(original_variables)
    assert shared_names.isdisjoint(original_variables)
    assert not any(name.endswith("_V2") for name in shared_names)
    assert all(
        original_variables[name] != v2_variables[name] for name in original_variables
    )


def test_configuration_doctor_validates_every_prompt_version(tmp_path: Path) -> None:
    projects = load_project_definitions(CONFIG_ROOT)
    reserved_runtime_variables = {
        QUESTIONS_VARIABLE,
        *(
            prompt_output_variable_name(prompt.prompt_name)
            for project in projects
            for prompt in project.prompts
        ),
    }

    configurations = _doctor_analysis_configuration(
        projects,
        tmp_path,
        reserved_runtime_variables=reserved_runtime_variables,
    )

    assert set(configurations) == set(PROMPT_PROJECT_BY_VERSION)
    for prompt_version, project_name in PROMPT_PROJECT_BY_VERSION.items():
        prompts, variables = configurations[prompt_version]
        assert all(prompt.project_name == project_name for prompt in prompts.values())
        assert set(SPECIALIST_VARIABLE_BY_PROMPT.values()) <= set(variables)


def test_workspace_readmes_require_shared_appendix(tmp_path: Path) -> None:
    with pytest.raises(InitializationError, match="ADD_TO_README_MD"):
        _workspace_readmes(_prompt_definitions(tmp_path), {})


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
    assert runner.calls[0] == RECONNAISSANCE_PROMPT
    assert result.comparison_review["output"] == "Audited final review"
    assert runner.calls[-1] == COMPARISON_PROMPT
    assert runner.specialist_completion_order == list(reversed(SPECIALIST_PROMPTS))
    assert runner.specialist_questions == _extract_specialist_question_blocks(
        _reconnaissance_output()
    )
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


def test_review_pipeline_runs_the_selected_v2_prompt_project(tmp_path: Path) -> None:
    runner = ParallelReviewRunner(tmp_path, V2_ANALYSIS_PROJECT)

    result = _run_review_pipeline(
        runner,
        _prompt_definitions(tmp_path, V2_ANALYSIS_PROJECT),
        variables={"ARG_DIRECTORY": str(tmp_path)},
        working_directory=tmp_path,
        project_name=V2_ANALYSIS_PROJECT,
    )

    assert tuple(result.specialist_reviews) == SPECIALIST_PROMPTS
    assert runner.calls[0] == RECONNAISSANCE_PROMPT
    assert len(runner.calls[1:-1]) == len(SPECIALIST_PROMPTS)
    assert set(runner.calls[1:-1]) == set(SPECIALIST_PROMPTS)
    assert runner.calls[-1] == COMPARISON_PROMPT


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


def test_pipeline_requires_every_specialist_to_receive_questions(
    tmp_path: Path,
) -> None:
    prompts = _prompt_definitions(tmp_path)
    prompts["ANALYZE_SECURITY"] = replace(
        prompts["ANALYZE_SECURITY"], template="Run ANALYZE_SECURITY"
    )
    runner = ParallelReviewRunner(tmp_path)

    with pytest.raises(InitializationError, match="must reference QUESTIONS"):
        _run_review_pipeline(
            runner,
            prompts,
            variables={"ARG_DIRECTORY": str(tmp_path)},
            working_directory=tmp_path,
        )

    assert runner.calls == []


def test_invalid_reconnaissance_stops_before_specialists(tmp_path: Path) -> None:
    prompts = _prompt_definitions(tmp_path)

    class InvalidReconnaissanceRunner:
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
            return _successful_review("No structured question blocks")

    runner = InvalidReconnaissanceRunner()

    with pytest.raises(InitializationError, match="Reconnaissance did not produce"):
        _run_review_pipeline(
            runner,
            prompts,
            variables={"ARG_DIRECTORY": str(tmp_path)},
            working_directory=tmp_path,
        )

    assert runner.calls == [RECONNAISSANCE_PROMPT]


@pytest.mark.parametrize(
    "runtime_variable",
    ["ANALYZE_PIPELINE_OUTPUT", QUESTIONS_VARIABLE],
)
def test_pipeline_rejects_caller_supplied_runtime_variable(
    tmp_path: Path, runtime_variable: str
) -> None:
    prompts = _prompt_definitions(tmp_path)

    with pytest.raises(InitializationError, match="generated at runtime"):
        _require_pipeline_variable_contract(
            prompts,
            {
                "ARG_DIRECTORY": str(tmp_path),
                runtime_variable: "spoofed or stale value",
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
            if prompt_name == RECONNAISSANCE_PROMPT:
                return _successful_review(_reconnaissance_output())
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
        for prompt_name in REPORT_PROMPTS
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
        for prompt_name in REPORT_PROMPTS
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
        "prompt_version",
        "prompt_project",
        "report_project_name",
        "report_run_id",
        "report_directory",
        "report_paths",
        "prompt_runner_policy",
    } <= set(schema["required"])
    assert schema["properties"]["prompt_version"]["enum"] == ["original", "v2"]
    assert set(schema["properties"]["prompt_project"]["enum"]) == set(
        PROMPT_PROJECT_BY_VERSION.values()
    )
    specialist_contract = schema["properties"]["specialist_reviews"]
    assert tuple(specialist_contract["required"]) == SPECIALIST_PROMPTS
    assert set(specialist_contract["properties"]) == set(SPECIALIST_PROMPTS)
    report_paths_contract = schema["properties"]["report_paths"]
    expected_output_names = {
        prompt_output_variable_name(prompt_name) for prompt_name in REPORT_PROMPTS
    }
    assert set(report_paths_contract["required"]) == expected_output_names
    assert set(report_paths_contract["properties"]) == expected_output_names
    policy = schema["properties"]["prompt_runner_policy"]
    assert policy["properties"]["attempt_timeout_seconds"]["const"] == 5400.0
    assert [
        item["const"]
        for item in policy["properties"]["retry_delays_seconds"]["prefixItems"]
    ] == [120, 300]
