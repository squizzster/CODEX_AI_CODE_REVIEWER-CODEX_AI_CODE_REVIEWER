"""Initialize Prompt Runner catalog state from repository-owned YAML."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sys
import threading
import unicodedata
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, TextIO

import yaml
from codex_prompt_runner_system import (
    ModelExecutor,
    PromptRunner,
    PromptRunnerError,
    RunOutcome,
)

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
PROMPT_RUNNER_ATTEMPT_TIMEOUT_SECONDS = 5400.0
PROMPT_RUNNER_RETRY_DELAYS_SECONDS = (120, 300)
LIVE_EVENT_SCHEMA = "codex-prompt-runner.live-event/v1"

type LiveEventHandler = Callable[[str, dict[str, Any]], None]


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
    prompt_owners: dict[str, PromptDefinition] = {}
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
                prompt.prompt_name, prompt
            )
            previous_contract = (
                previous_owner.template,
                previous_owner.model,
                previous_owner.reasoning_effort,
                previous_owner.risk_profile,
            )
            current_contract = (
                prompt.template,
                prompt.model,
                prompt.reasoning_effort,
                prompt.risk_profile,
            )
            if previous_contract != current_contract:
                raise InitializationError(
                    f"{prompt.source_path}: globally named prompt "
                    f"{prompt.prompt_name!r} conflicts with "
                    f"{previous_owner.source_path}; shared prompt definitions must "
                    "have identical text and execution defaults"
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


class _ReviewerProgressSink:
    """Project typed Prompt Runner progress onto the existing live JSONL contract."""

    def __init__(
        self,
        prompt_name: str,
        *,
        live_event_handler: LiveEventHandler | None = None,
        live_event_stream: TextIO | None = None,
        stream_write_lock: threading.Lock,
    ) -> None:
        self._prompt_name = prompt_name
        self._live_event_handler = live_event_handler
        self._live_event_stream = live_event_stream or sys.stderr
        self._stream_write_lock = stream_write_lock
        self._event_lock = threading.Lock()
        self._invocation_id = secrets.token_hex(16)
        self._event_sequence = 0
        self.isolated_workspace: str | None = None
        self.failure: Exception | None = None

    def emit(self, event: str, **fields: Any) -> None:
        """Handle and forward one event atomically with a stable invocation identity."""

        with self._event_lock:
            self._event_sequence += 1
            payload = {
                "schema": LIVE_EVENT_SCHEMA,
                "event": event,
                "invocation_id": self._invocation_id,
                "event_sequence": self._event_sequence,
                "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                **{key: value for key, value in fields.items() if value is not None},
            }
            workspace_value = payload.get("isolated_workspace")
            if isinstance(workspace_value, str) and workspace_value:
                self.isolated_workspace = workspace_value
            handler_failure: Exception | None = None
            if self._live_event_handler is not None:
                try:
                    self._live_event_handler(self._prompt_name, payload)
                except Exception as error:  # noqa: BLE001 - surfaced after execution
                    self.failure = error
                    handler_failure = error
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._stream_write_lock:
            self._live_event_stream.write(f"{line}\n")
            self._live_event_stream.flush()
        if handler_failure is not None:
            raise handler_failure


def _runner_failure(error: PromptRunnerError) -> InitializationError:
    details = f"; context={error.context!r}" if error.context else ""
    return InitializationError(
        f"Prompt Runner failed [{error.code}]: {error.message}{details}"
    )


def _require_live_event_success(
    sink: _ReviewerProgressSink, prompt_name: str
) -> None:
    if sink.failure is not None:
        raise InitializationError(
            f"Prompt Runner live-event handling failed for {prompt_name}: "
            f"{sink.failure}"
        ) from sink.failure


def _run_outcome_record(outcome: RunOutcome) -> dict[str, Any]:
    """Preserve the Reviewer's stable dictionary contract from a typed outcome."""

    build = outcome.build
    template = build.materialized.template
    unique = build.unique_prompt
    record: dict[str, Any] = {
        "delivery_mode": outcome.delivery_mode,
        "request_id": outcome.request_id,
        "project": template.project_name,
        "prompt": template.prompt_name,
        "prompt_name_origin": template.prompt_name_origin,
        "template_version": template.version,
        "template_sha256": template.sha256,
        "template_byte_length": template.byte_length,
        "template_is_current": template.current,
        "unique_built_prompt_id": unique.public_id,
        "built_prompt_sha256": unique.built_prompt_sha256,
        "model": unique.model,
        "reasoning_effort": unique.reasoning_effort,
        "risk_profile": build.risk_profile,
        "execution_policy_sha256": unique.execution_policy_sha256,
        "codex_permissions": unique.codex_permissions.as_dict(),
        "result_id": outcome.result.public_id,
        "result_version": outcome.result.version,
        "result_sha256": outcome.result.sha256,
        "result_byte_length": len(outcome.result.content),
        "live_occurrence_count": outcome.result.live_occurrence_count,
        "output": outcome.result.content.decode("utf-8"),
        "available_result_variants": outcome.available_result_variants,
        "attempts": outcome.attempts,
        "client_reasoning": list(outcome.client_reasoning),
        "model_source": build.model_source,
        "reasoning_effort_source": build.reasoning_effort_source,
        "risk_profile_source": build.risk_profile_source,
        "provenance": outcome.provenance,
    }
    if outcome.run_id is not None:
        record["execution_run_id"] = outcome.run_id
    if outcome.progress_delivery.get("degraded"):
        record["progress_delivery"] = outcome.progress_delivery
    return record


class PromptRunnerLibrary:
    """Typed in-process adapter over Prompt Runner's public Python facade."""

    def __init__(
        self,
        state_root: Path | None = None,
        *,
        executor: ModelExecutor | None = None,
        retry_delays_seconds: tuple[int, ...] = PROMPT_RUNNER_RETRY_DELAYS_SECONDS,
        attempt_timeout_seconds: float = PROMPT_RUNNER_ATTEMPT_TIMEOUT_SECONDS,
        live_event_handler: LiveEventHandler | None = None,
        live_event_stream: TextIO | None = None,
    ) -> None:
        try:
            self._runner = PromptRunner(
                state_root=state_root,
                executor=executor,
                retry_delays_seconds=retry_delays_seconds,
                attempt_timeout_seconds=attempt_timeout_seconds,
            )
        except (PromptRunnerError, TypeError, ValueError) as error:
            if isinstance(error, PromptRunnerError):
                raise _runner_failure(error) from error
            raise InitializationError(f"Invalid Prompt Runner policy: {error}") from error
        self._live_event_handler = live_event_handler
        self._live_event_stream = live_event_stream or sys.stderr
        self._live_event_write_lock = threading.Lock()
        self._requires_isolated_workspace = executor is None

    def list_projects(self) -> tuple[str, ...]:
        try:
            return self._runner.list_projects()
        except PromptRunnerError as error:
            raise _runner_failure(error) from error

    def register_project(self, project_name: str) -> None:
        try:
            self._runner.register_project(project_name)
        except PromptRunnerError as error:
            raise _runner_failure(error) from error

    def prompt_status(self, prompt: PromptDefinition) -> PromptStatus:
        try:
            defaults = self._runner.get_prompt_defaults(
                prompt.project_name, prompt.prompt_name
            )
            versions = self._runner.list_template_versions(
                prompt.project_name, prompt.prompt_name
            )
        except PromptRunnerError as error:
            if error.code in {"prompt_not_found", "prompt_not_in_project"}:
                return "missing"
            raise _runner_failure(error) from error
        current_versions = [version for version in versions if version.current]
        if len(current_versions) != 1:
            raise InitializationError(
                "Prompt Runner prompt state must contain one current version"
            )
        current = current_versions[0]
        expected_sha256 = hashlib.sha256(prompt.template.encode("utf-8")).hexdigest()
        matches = (
            current.sha256 == expected_sha256
            and defaults.model == prompt.model
            and defaults.reasoning_effort == prompt.reasoning_effort
            and defaults.risk_profile == prompt.risk_profile
        )
        return "current" if matches else "drifted"

    def synchronize_prompt(self, prompt: PromptDefinition) -> None:
        """Publish desired bytes/current version, then explicitly update defaults."""

        try:
            self._runner.register_prompt(
                prompt.project_name,
                prompt.template.encode("utf-8"),
                name=prompt.prompt_name,
                default_model=prompt.model,
                default_reasoning_effort=prompt.reasoning_effort,
                default_risk_profile=prompt.risk_profile,
                make_current=True,
            )
            self._runner.set_prompt_defaults(
                prompt.project_name,
                prompt.prompt_name,
                model=prompt.model,
                reasoning_effort=prompt.reasoning_effort,
                risk_profile=prompt.risk_profile,
            )
        except PromptRunnerError as error:
            raise _runner_failure(error) from error

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
        """Force one live run while consuming and forwarding progress in real time."""

        sink = _ReviewerProgressSink(
            prompt_name,
            live_event_handler=self._live_event_handler,
            live_event_stream=self._live_event_stream,
            stream_write_lock=self._live_event_write_lock,
        )
        try:
            outcome = self._runner.run(
                project_name,
                prompt_name,
                variables=variables,
                working_directory=working_directory,
                model=model,
                reasoning_effort=reasoning_effort,
                live=True,
                progress_sink=sink,
            )
        except PromptRunnerError as error:
            _require_live_event_success(sink, prompt_name)
            raise _runner_failure(error) from error
        _require_live_event_success(sink, prompt_name)
        if not isinstance(outcome, RunOutcome):
            raise InitializationError("Prompt Runner returned a non-live outcome")
        if self._requires_isolated_workspace and sink.isolated_workspace is None:
            raise InitializationError(
                f"Prompt Runner advertised no isolated workspace for {prompt_name}"
            )
        result = _run_outcome_record(outcome)
        if sink.isolated_workspace is not None:
            result["isolated_workspace"] = sink.isolated_workspace
        return result
