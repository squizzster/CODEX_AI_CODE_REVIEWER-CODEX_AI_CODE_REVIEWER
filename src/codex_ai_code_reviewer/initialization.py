"""Initialize Prompt Runner catalog state from repository-owned YAML."""

from __future__ import annotations

import json
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

PROJECT_NAME_PATTERN = re.compile(r"[A-Z0-9_-]+")
REASONING_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh", "max"})
RISK_PROFILES = frozenset(
    {"LOCKED_DOWN", "WEB_RESEARCH", "BALANCED", "NETWORKED_WORKSPACE", "FULL_ACCESS"}
)
PROMPT_KEYS = frozenset({"template", "model", "reasoning_effort", "risk_profile"})


class InitializationError(RuntimeError):
    """A configuration or Prompt Runner initialization failure."""


@dataclass(frozen=True, slots=True)
class PromptDefinition:
    project_name: str
    prompt_name: str
    source_path: Path
    template: str
    model: str
    reasoning_effort: str
    risk_profile: str


@dataclass(frozen=True, slots=True)
class ProjectDefinition:
    project_name: str
    prompts: tuple[PromptDefinition, ...]


@dataclass(frozen=True, slots=True)
class InitializationReport:
    created_projects: tuple[str, ...]
    existing_projects: tuple[str, ...]
    created_prompts: tuple[str, ...]
    existing_prompts: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "created_projects": list(self.created_projects),
            "existing_projects": list(self.existing_projects),
            "created_prompts": list(self.created_prompts),
            "existing_prompts": list(self.existing_prompts),
        }


class PromptRunnerGateway(Protocol):
    def list_projects(self) -> tuple[str, ...]: ...

    def register_project(self, project_name: str) -> None: ...

    def prompt_exists(self, project_name: str, prompt_name: str) -> bool: ...

    def register_prompt(self, prompt: PromptDefinition) -> None: ...


def _required_string(data: dict[str, Any], key: str, source_path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise InitializationError(f"{source_path}: {key!r} must be a non-empty string")
    return value


def _validate_prompt_name(prompt_name: str, source_path: Path) -> None:
    if not prompt_name:
        raise InitializationError(
            f"{source_path}: the filename must provide a prompt name"
        )
    if unicodedata.normalize("NFC", prompt_name) != prompt_name:
        raise InitializationError(f"{source_path}: prompt name must be NFC-normalized")
    if not prompt_name.isprintable() or len(prompt_name.encode("utf-8")) > 255:
        raise InitializationError(
            f"{source_path}: prompt name must be printable and at most 255 UTF-8 bytes"
        )


def _load_prompt(project_name: str, source_path: Path) -> PromptDefinition:
    try:
        loaded = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise InitializationError(
            f"{source_path}: cannot read prompt YAML: {error}"
        ) from error
    if not isinstance(loaded, dict) or not all(isinstance(key, str) for key in loaded):
        raise InitializationError(
            f"{source_path}: prompt YAML must be a string-keyed mapping"
        )

    unexpected = sorted(set(loaded) - PROMPT_KEYS)
    missing = sorted(PROMPT_KEYS - set(loaded))
    if missing or unexpected:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected {', '.join(unexpected)}")
        raise InitializationError(
            f"{source_path}: invalid fields ({'; '.join(details)})"
        )

    prompt_name = source_path.name.removesuffix(".yaml")
    _validate_prompt_name(prompt_name, source_path)
    template = _required_string(loaded, "template", source_path)
    model = _required_string(loaded, "model", source_path)
    reasoning_effort = _required_string(loaded, "reasoning_effort", source_path)
    risk_profile = _required_string(loaded, "risk_profile", source_path)

    if any(character.isspace() for character in model):
        raise InitializationError(f"{source_path}: model must not contain whitespace")
    if reasoning_effort not in REASONING_EFFORTS:
        raise InitializationError(
            f"{source_path}: reasoning_effort must be one of {', '.join(sorted(REASONING_EFFORTS))}"
        )
    if risk_profile not in RISK_PROFILES:
        raise InitializationError(
            f"{source_path}: risk_profile must be one of {', '.join(sorted(RISK_PROFILES))}"
        )

    return PromptDefinition(
        project_name=project_name,
        prompt_name=prompt_name,
        source_path=source_path,
        template=template,
        model=model,
        reasoning_effort=reasoning_effort,
        risk_profile=risk_profile,
    )


def load_project_definitions(config_root: Path) -> tuple[ProjectDefinition, ...]:
    """Load and fully validate config before any registry mutation."""

    if not config_root.is_dir():
        raise InitializationError(
            f"Prompt configuration directory does not exist: {config_root}"
        )

    projects: list[ProjectDefinition] = []
    prompt_owners: dict[str, Path] = {}
    project_paths = sorted(path for path in config_root.iterdir() if path.is_dir())
    if not project_paths:
        raise InitializationError(f"No project directories found in {config_root}")

    for project_path in project_paths:
        project_name = project_path.name
        if PROJECT_NAME_PATTERN.fullmatch(project_name) is None:
            raise InitializationError(
                f"{project_path}: project directory must match [A-Z0-9_-]+"
            )
        prompts = tuple(
            _load_prompt(project_name, prompt_path)
            for prompt_path in sorted(project_path.glob("*.yaml"))
            if prompt_path.is_file()
        )
        for prompt in prompts:
            previous_owner = prompt_owners.setdefault(
                prompt.prompt_name, prompt.source_path
            )
            if previous_owner != prompt.source_path:
                raise InitializationError(
                    f"{prompt.source_path}: prompt name {prompt.prompt_name!r} is already owned by "
                    f"{previous_owner}"
                )
        projects.append(ProjectDefinition(project_name, prompts))

    return tuple(projects)


def initialize_prompt_catalog(
    projects: tuple[ProjectDefinition, ...], gateway: PromptRunnerGateway
) -> InitializationReport:
    """Register missing projects and prompts through one Prompt Runner boundary."""

    known_projects = set(gateway.list_projects())
    created_projects: list[str] = []
    existing_projects: list[str] = []
    created_prompts: list[str] = []
    existing_prompts: list[str] = []

    for project in projects:
        if project.project_name in known_projects:
            existing_projects.append(project.project_name)
        else:
            gateway.register_project(project.project_name)
            known_projects.add(project.project_name)
            created_projects.append(project.project_name)

        for prompt in project.prompts:
            identity = f"{project.project_name}/{prompt.prompt_name}"
            if gateway.prompt_exists(project.project_name, prompt.prompt_name):
                existing_prompts.append(identity)
            else:
                gateway.register_prompt(prompt)
                created_prompts.append(identity)

    return InitializationReport(
        tuple(created_projects),
        tuple(existing_projects),
        tuple(created_prompts),
        tuple(existing_prompts),
    )


@dataclass(frozen=True, slots=True)
class _CommandResult:
    exit_code: int
    payload: dict[str, Any]
    stderr: str


class PromptRunnerCli:
    """Adapter for the Prompt Runner's versioned JSON command contract."""

    def __init__(self, runner_root: Path) -> None:
        self._runner_root = runner_root.resolve()
        if not (self._runner_root / "pyproject.toml").is_file():
            raise InitializationError(
                f"Prompt Runner project was not found at {self._runner_root}"
            )

    def _invoke(self, *arguments: str) -> _CommandResult:
        command = ["uv", "run", "codex-prompt-runner", *arguments]
        try:
            completed = subprocess.run(
                command,
                cwd=self._runner_root,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        except OSError as error:
            raise InitializationError(f"Cannot start Prompt Runner: {error}") from error
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise InitializationError(
                "Prompt Runner returned invalid JSON "
                f"(exit {completed.returncode}): {completed.stderr.strip()}"
            ) from error
        if not isinstance(payload, dict):
            raise InitializationError(
                "Prompt Runner returned a non-object JSON document"
            )
        return _CommandResult(completed.returncode, payload, completed.stderr)

    @staticmethod
    def _require_success(result: _CommandResult) -> dict[str, Any]:
        if result.exit_code == 0 and result.payload.get("ok") is True:
            data = result.payload.get("data")
            if isinstance(data, dict):
                return data
            raise InitializationError("Prompt Runner success response has invalid data")
        errors = result.payload.get("errors")
        raise InitializationError(
            f"Prompt Runner command failed (exit {result.exit_code}): {errors!r}; "
            f"stderr={result.stderr.strip()!r}"
        )

    def list_projects(self) -> tuple[str, ...]:
        data = self._require_success(self._invoke("project", "list"))
        projects = data.get("projects")
        if not isinstance(projects, list) or not all(
            isinstance(project, str) for project in projects
        ):
            raise InitializationError("Prompt Runner project list has an invalid shape")
        return tuple(projects)

    def register_project(self, project_name: str) -> None:
        self._require_success(self._invoke("project", "register", project_name))

    def prompt_exists(self, project_name: str, prompt_name: str) -> bool:
        result = self._invoke("prompt", "list", project_name, prompt_name)
        if result.exit_code == 0 and result.payload.get("ok") is True:
            self._require_success(result)
            return True
        errors = result.payload.get("errors")
        if isinstance(errors, list) and any(
            isinstance(error, dict) and error.get("code") == "prompt_not_found"
            for error in errors
        ):
            return False
        self._require_success(result)
        raise AssertionError("unreachable")

    def register_prompt(self, prompt: PromptDefinition) -> None:
        self._require_success(
            self._invoke(
                "prompt",
                "register",
                prompt.project_name,
                prompt.prompt_name,
                "--template",
                prompt.template,
                "--model",
                prompt.model,
                "--reasoning",
                prompt.reasoning_effort,
                "--risk-profile",
                prompt.risk_profile,
                "--make-current",
            )
        )
