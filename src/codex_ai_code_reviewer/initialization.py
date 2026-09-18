"""Initialize Prompt Runner catalog state from repository-owned YAML."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml

PROJECT_NAME_PATTERN = re.compile(r"[A-Z0-9_-]+")
VARIABLE_NAME_PATTERN = re.compile(r"[A-Z][A-Z0-9_]*")
VARIABLE_REFERENCE_PATTERN = re.compile(r"\{\{VAR:([A-Z][A-Z0-9_]*)\}\}")
REASONING_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh", "max"})
RISK_PROFILES = frozenset(
    {"LOCKED_DOWN", "WEB_RESEARCH", "BALANCED", "NETWORKED_WORKSPACE", "FULL_ACCESS"}
)
PROMPT_KEYS = frozenset({"prompt", "model", "reasoning_effort", "risk_profile"})
ARG_DIRECTORY_VARIABLE = "ARG_DIRECTORY"
PROMPT_OUTPUT_VARIABLE_SUFFIX = "_OUTPUT"


class InitializationError(RuntimeError):
    """A configuration or Prompt Runner initialization failure."""


def prompt_output_variable_name(prompt_name: str) -> str:
    """Return the stable runtime variable owned by one completed prompt."""

    variable_name = f"{prompt_name}{PROMPT_OUTPUT_VARIABLE_SUFFIX}"
    if VARIABLE_NAME_PATTERN.fullmatch(variable_name) is None:
        raise InitializationError(
            f"prompt name {prompt_name!r} cannot form a runtime variable; "
            "prompt filenames must match [A-Z][A-Z0-9_]*.yaml"
        )
    return variable_name


def variable_reference_names(value: str) -> frozenset[str]:
    """Return direct variable references without expanding opaque runtime values."""

    return frozenset(VARIABLE_REFERENCE_PATTERN.findall(value))


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
class VariableDefinition:
    variable_name: str
    source_path: Path
    value: str


@dataclass(frozen=True, slots=True)
class InitializationReport:
    created_projects: tuple[str, ...]
    existing_projects: tuple[str, ...]
    created_prompts: tuple[str, ...]
    updated_prompts: tuple[str, ...]
    existing_prompts: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "created_projects": list(self.created_projects),
            "existing_projects": list(self.existing_projects),
            "created_prompts": list(self.created_prompts),
            "updated_prompts": list(self.updated_prompts),
            "existing_prompts": list(self.existing_prompts),
        }


type PromptStatus = Literal["missing", "current", "drifted"]


class PromptRunnerGateway(Protocol):
    def list_projects(self) -> tuple[str, ...]: ...

    def register_project(self, project_name: str) -> None: ...

    def prompt_status(self, prompt: PromptDefinition) -> PromptStatus: ...

    def synchronize_prompt(self, prompt: PromptDefinition) -> None: ...


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
    try:
        prompt_output_variable_name(prompt_name)
    except InitializationError as error:
        raise InitializationError(f"{source_path}: {error}") from None


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
    template = _required_string(loaded, "prompt", source_path)
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


def load_variable_definitions(config_root: Path) -> tuple[VariableDefinition, ...]:
    """Load reusable Prompt Runner variable values from YAML scalar files."""

    if not config_root.is_dir():
        raise InitializationError(
            f"Variable configuration directory does not exist: {config_root}"
        )

    variables: list[VariableDefinition] = []
    for source_path in sorted(config_root.glob("*.yaml")):
        if not source_path.is_file():
            continue
        variable_name = source_path.name.removesuffix(".yaml")
        if VARIABLE_NAME_PATTERN.fullmatch(variable_name) is None:
            raise InitializationError(
                f"{source_path}: variable filename must match [A-Z][A-Z0-9_]*.yaml"
            )
        try:
            loaded = yaml.safe_load(source_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            raise InitializationError(
                f"{source_path}: cannot read variable YAML: {error}"
            ) from error
        if not isinstance(loaded, str) or not loaded:
            raise InitializationError(
                f"{source_path}: variable YAML must contain one non-empty string scalar"
            )
        variables.append(VariableDefinition(variable_name, source_path, loaded))

    return tuple(variables)


def compose_variable_values(
    configured_variables: tuple[VariableDefinition, ...],
    review_directory: Path,
    *,
    reserved_runtime_variables: Collection[str] = (),
) -> dict[str, str]:
    """Combine runtime values and resolve trusted configured-variable references."""

    values = {
        variable.variable_name: variable.value for variable in configured_variables
    }
    reserved_names = {ARG_DIRECTORY_VARIABLE, *reserved_runtime_variables}
    conflicting_names = sorted(reserved_names & values.keys())
    if conflicting_names:
        conflicting_name = conflicting_names[0]
        source_path = next(
            variable.source_path
            for variable in configured_variables
            if variable.variable_name == conflicting_name
        )
        raise InitializationError(
            f"{source_path}: {conflicting_name} is reserved for runtime pipeline data"
        )
    values[ARG_DIRECTORY_VARIABLE] = str(review_directory)
    source_by_name = {
        variable.variable_name: variable.source_path
        for variable in configured_variables
    }
    resolved: dict[str, str] = {}
    resolving: list[str] = []

    def resolve(variable_name: str) -> str:
        if variable_name in resolved:
            return resolved[variable_name]
        if variable_name in resolving:
            cycle_start = resolving.index(variable_name)
            cycle = (*resolving[cycle_start:], variable_name)
            source_path = source_by_name.get(variable_name, variable_name)
            raise InitializationError(
                f"{source_path}: variable reference cycle: {' -> '.join(cycle)}"
            )

        resolving.append(variable_name)

        def replace_reference(match: re.Match[str]) -> str:
            referenced_name = match.group(1)
            if referenced_name not in values:
                source_path = source_by_name.get(variable_name, variable_name)
                raise InitializationError(
                    f"{source_path}: variable {variable_name} references missing "
                    f"variable {referenced_name}"
                )
            return resolve(referenced_name)

        try:
            value = VARIABLE_REFERENCE_PATTERN.sub(
                replace_reference, values[variable_name]
            )
        finally:
            resolving.pop()
        resolved[variable_name] = value
        return value

    for variable_name in values:
        resolve(variable_name)
    return resolved


def initialize_prompt_catalog(
    projects: tuple[ProjectDefinition, ...], gateway: PromptRunnerGateway
) -> InitializationReport:
    """Register missing projects and prompts through one Prompt Runner boundary."""

    known_projects = set(gateway.list_projects())
    created_projects: list[str] = []
    existing_projects: list[str] = []
    created_prompts: list[str] = []
    updated_prompts: list[str] = []
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
            status = gateway.prompt_status(prompt)
            if status == "current":
                existing_prompts.append(identity)
            elif status == "missing":
                gateway.synchronize_prompt(prompt)
                _require_synchronized_prompt(gateway, prompt)
                created_prompts.append(identity)
            elif status == "drifted":
                gateway.synchronize_prompt(prompt)
                _require_synchronized_prompt(gateway, prompt)
                updated_prompts.append(identity)
            else:
                raise InitializationError(
                    f"Prompt Runner adapter returned unknown prompt status: {status!r}"
                )

    return InitializationReport(
        tuple(created_projects),
        tuple(existing_projects),
        tuple(created_prompts),
        tuple(updated_prompts),
        tuple(existing_prompts),
    )


def _require_synchronized_prompt(
    gateway: PromptRunnerGateway, prompt: PromptDefinition
) -> None:
    observed = gateway.prompt_status(prompt)
    if observed != "current":
        raise InitializationError(
            f"Prompt synchronization did not converge for "
            f"{prompt.project_name}/{prompt.prompt_name}: {observed}"
        )


@dataclass(frozen=True, slots=True)
class _CommandResult:
    exit_code: int
    payload: dict[str, Any]
    stderr: str


def _runner_subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("VIRTUAL_ENV", None)
    return environment


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
                env=_runner_subprocess_environment(),
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

    def prompt_status(self, prompt: PromptDefinition) -> PromptStatus:
        result = self._invoke("prompt", "list", prompt.project_name, prompt.prompt_name)
        if result.exit_code == 0 and result.payload.get("ok") is True:
            data = self._require_success(result)
            defaults = data.get("defaults")
            versions = data.get("versions")
            if not isinstance(defaults, dict) or not isinstance(versions, list):
                raise InitializationError(
                    "Prompt Runner prompt state has an invalid shape"
                )
            current_versions = [
                version
                for version in versions
                if isinstance(version, dict) and version.get("current") is True
            ]
            if len(current_versions) != 1:
                raise InitializationError(
                    "Prompt Runner prompt state must contain one current version"
                )
            expected_sha256 = hashlib.sha256(
                prompt.template.encode("utf-8")
            ).hexdigest()
            current = current_versions[0]
            matches = (
                current.get("sha256") == expected_sha256
                and defaults.get("model") == prompt.model
                and defaults.get("reasoning_effort") == prompt.reasoning_effort
                and defaults.get("risk_profile") == prompt.risk_profile
            )
            return "current" if matches else "drifted"
        errors = result.payload.get("errors")
        if isinstance(errors, list) and any(
            isinstance(error, dict) and error.get("code") == "prompt_not_found"
            for error in errors
        ):
            return "missing"
        self._require_success(result)
        raise AssertionError("unreachable")

    def synchronize_prompt(self, prompt: PromptDefinition) -> None:
        """Publish desired bytes/current version, then explicitly update defaults."""

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
        self._require_success(
            self._invoke(
                "prompt",
                "set-defaults",
                prompt.project_name,
                prompt.prompt_name,
                "--model",
                prompt.model,
                "--reasoning",
                prompt.reasoning_effort,
                "--risk-profile",
                prompt.risk_profile,
            )
        )

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
        """Force one live run while forwarding structured progress to stderr."""

        command = [
            "uv",
            "run",
            "codex-prompt-runner",
            "run",
            project_name,
            prompt_name,
        ]
        if model is not None:
            command.extend(("--model", model))
        if reasoning_effort is not None:
            command.extend(("--reasoning", reasoning_effort))
        for name, value in sorted(variables.items()):
            command.extend(("--var", f"{name}={value}"))
        command.extend(("--cwd", str(working_directory), "--live", "--detail"))
        try:
            completed = subprocess.run(
                command,
                cwd=self._runner_root,
                check=False,
                stdout=subprocess.PIPE,
                stderr=None,
                text=True,
                encoding="utf-8",
                env=_runner_subprocess_environment(),
            )
        except OSError as error:
            raise InitializationError(f"Cannot start Prompt Runner: {error}") from error
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise InitializationError(
                f"Prompt Runner returned invalid run JSON (exit {completed.returncode})"
            ) from error
        if not isinstance(payload, dict):
            raise InitializationError(
                "Prompt Runner returned a non-object run JSON document"
            )
        return self._require_success(
            _CommandResult(completed.returncode, payload, "forwarded to stderr")
        )
