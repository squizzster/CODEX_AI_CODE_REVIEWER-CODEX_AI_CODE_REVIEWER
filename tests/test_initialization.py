from __future__ import annotations

from pathlib import Path

import pytest

from codex_ai_code_reviewer.initialization import (
    InitializationError,
    ProjectDefinition,
    PromptDefinition,
    initialize_prompt_catalog,
    load_project_definitions,
)


class FakePromptRunner:
    def __init__(
        self,
        *,
        projects: set[str] | None = None,
        prompts: set[tuple[str, str]] | None = None,
    ) -> None:
        self.projects = projects or set()
        self.prompts = prompts or set()
        self.registered_projects: list[str] = []
        self.registered_prompts: list[PromptDefinition] = []

    def list_projects(self) -> tuple[str, ...]:
        return tuple(sorted(self.projects))

    def register_project(self, project_name: str) -> None:
        self.projects.add(project_name)
        self.registered_projects.append(project_name)

    def prompt_exists(self, project_name: str, prompt_name: str) -> bool:
        return (project_name, prompt_name) in self.prompts

    def register_prompt(self, prompt: PromptDefinition) -> None:
        self.prompts.add((prompt.project_name, prompt.prompt_name))
        self.registered_prompts.append(prompt)


def _write_prompt(path: Path, template: str = 'Say "hello"') -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                f"template: {template!r}",
                "model: gpt-6-astra",
                "reasoning_effort: xhigh",
                "risk_profile: LOCKED_DOWN",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_load_project_definitions_maps_directories_and_filenames(
    tmp_path: Path,
) -> None:
    source = tmp_path / "CODEX_AI_CODE_REVIEW" / "ANALYZE_PIPELINE.yaml"
    _write_prompt(source)

    projects = load_project_definitions(tmp_path)

    assert len(projects) == 1
    assert projects[0].project_name == "CODEX_AI_CODE_REVIEW"
    assert len(projects[0].prompts) == 1
    prompt = projects[0].prompts[0]
    assert prompt.prompt_name == "ANALYZE_PIPELINE"
    assert prompt.template == 'Say "hello"'
    assert prompt.model == "gpt-6-astra"
    assert prompt.reasoning_effort == "xhigh"
    assert prompt.risk_profile == "LOCKED_DOWN"


def test_initialize_registers_only_missing_catalog_entries(tmp_path: Path) -> None:
    existing = PromptDefinition(
        "CODEX_AI_CODE_REVIEW",
        "ANALYZE_PIPELINE",
        tmp_path / "ANALYZE_PIPELINE.yaml",
        'Say "hello"',
        "gpt-6-astra",
        "xhigh",
        "LOCKED_DOWN",
    )
    missing = PromptDefinition(
        "CODEX_AI_CODE_REVIEW",
        "GOODBYE",
        tmp_path / "GOODBYE.yaml",
        'Say "goodbye"',
        "gpt-6-astra",
        "xhigh",
        "LOCKED_DOWN",
    )
    projects = (ProjectDefinition("CODEX_AI_CODE_REVIEW", (existing, missing)),)
    gateway = FakePromptRunner(
        projects={"CODEX_AI_CODE_REVIEW"},
        prompts={("CODEX_AI_CODE_REVIEW", "ANALYZE_PIPELINE")},
    )

    report = initialize_prompt_catalog(projects, gateway)

    assert report.created_projects == ()
    assert report.existing_projects == ("CODEX_AI_CODE_REVIEW",)
    assert report.created_prompts == ("CODEX_AI_CODE_REVIEW/GOODBYE",)
    assert report.existing_prompts == ("CODEX_AI_CODE_REVIEW/ANALYZE_PIPELINE",)
    assert gateway.registered_projects == []
    assert gateway.registered_prompts == [missing]


def test_second_initialization_is_idempotent(tmp_path: Path) -> None:
    prompt = PromptDefinition(
        "CODEX_AI_CODE_REVIEW",
        "ANALYZE_PIPELINE",
        tmp_path / "ANALYZE_PIPELINE.yaml",
        'Say "hello"',
        "gpt-6-astra",
        "xhigh",
        "LOCKED_DOWN",
    )
    projects = (ProjectDefinition("CODEX_AI_CODE_REVIEW", (prompt,)),)
    gateway = FakePromptRunner()

    first = initialize_prompt_catalog(projects, gateway)
    second = initialize_prompt_catalog(projects, gateway)

    assert first.created_projects == ("CODEX_AI_CODE_REVIEW",)
    assert first.created_prompts == ("CODEX_AI_CODE_REVIEW/ANALYZE_PIPELINE",)
    assert second.created_projects == ()
    assert second.created_prompts == ()
    assert second.existing_prompts == ("CODEX_AI_CODE_REVIEW/ANALYZE_PIPELINE",)


def test_duplicate_global_prompt_name_is_rejected_before_initialization(
    tmp_path: Path,
) -> None:
    _write_prompt(tmp_path / "FIRST_PROJECT" / "SHARED_NAME.yaml")
    _write_prompt(tmp_path / "SECOND_PROJECT" / "SHARED_NAME.yaml")

    with pytest.raises(InitializationError, match="already owned"):
        load_project_definitions(tmp_path)


def test_invalid_prompt_field_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "CODEX_AI_CODE_REVIEW" / "ANALYZE_PIPELINE.yaml"
    _write_prompt(source)
    source.write_text(source.read_text(encoding="utf-8") + "surprise: true\n")

    with pytest.raises(InitializationError, match="unexpected surprise"):
        load_project_definitions(tmp_path)
