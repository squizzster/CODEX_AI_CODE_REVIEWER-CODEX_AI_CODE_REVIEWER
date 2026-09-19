# CODEX AI Code Reviewer

Parallel AI-assisted code review with an audited final synthesis.

## Status

Development mode: **ALPHA**. The primary workflow and failure paths are tested, the
CLI result is versioned, and the architecture catalogue is validated. Interfaces may
still change before a stable release.

## Run

Requirements: Python 3.12+, `uv`, and a sibling `CODEX_PROMPT_RUNNER_SYSTEM`
checkout. Override the runner location with `CODEX_PROMPT_RUNNER_PROJECT_ROOT`.

```bash
uv sync --locked --group dev
./run_the_code_review /absolute/or/relative/repository
./run_the_code_review --model gpt-5.6-luna --reasoning max /repository
./run_the_code_review /repository --model gpt-5.6-luna --reasoning max
./run_the_code_review --reports-directory /archive/code-reviews /repository
```

The review directory must exist and be readable and traversable. It becomes the
working directory of the Bash launcher and Python reviewer process, the Prompt Runner
`--cwd`, and the `ARG_DIRECTORY` runtime variable. Tool-enabled Prompt Runner profiles
execute Codex inside a safety-created isolated workspace derived from that directory;
the original target path and isolated execution workspace are intentionally distinct.
`--model` and `--reasoning` apply to every specialist and the comparison for that run
without changing the defaults stored in prompt YAML or the Prompt Runner catalogue.
Reports default to this project's `reports/` directory. A relative
`--reports-directory` is resolved from the directory where the launcher was invoked.

## Pipeline

1. Validate repository-owned YAML and synchronize the Prompt Runner catalogue.
2. Consume each Prompt Runner JSONL event stream as it arrives. When an executing
   event carries `isolated_workspace`, call `create_runner_work_space()` to create
   that workspace, a `source_code_read_only_link` symlink to the reviewed directory,
   `final_reports/`, `tmp/`, `temp_scripts/`, `scratch_pad/`, and `README.md`. The
   function returns `1` on success and `0` on failure.
3. Run `ANALYZE_PIPELINE`, `ANALYZE_BOUNDARIES`, `ANALYZE_NETWORKING`,
   `ANALYZE_INTEGRITY`, `ANALYZE_SECURITY`, and `ANALYZE_PERFORMANCE` in parallel.
4. Publish each successful result internally as `{{VAR:<PROMPT_NAME>_OUTPUT}}`.
5. Run `COMPARE_AGENT_REPORTS` only after all specialist reports succeed, with each
   report bound by prompt identity rather than completion order.
6. Atomically publish all seven Markdown reports under
   `reports/<project_name>/<review_started_at_utc>/` and emit the complete
   versioned result as one JSON object on stdout.

The reviewer parses and handles every Prompt Runner event before forwarding its
original JSONL record to stderr. Cached results and custom executors legitimately omit
`isolated_workspace`, so they do not trigger workspace initialization. Expected input,
configuration, catalogue, execution, and publication failures return exit code `2`;
no report directory is published from an incomplete pipeline.

Each published filename follows `<PROMPT_NAME>_OUTPUT.md`, including
`ANALYZE_SECURITY_OUTPUT.md` and `COMPARE_AGENT_REPORTS_OUTPUT.md`. The comparison
report is stored with the others in a run directory identified by a sortable UTC ID
such as `2026-09-19T15-47-45.12Z`;
each review record retains its own Prompt Runner execution ID in the JSON result.

`ANALYZE_RECONNAISSANCE` is temporarily disabled. Its prompt and specialist variable
remain configured, but it is not executed or included in the comparison or result.

`ANALYZE_PIPELINE`, `ANALYZE_BOUNDARIES`, `ANALYZE_INTEGRITY`, and
`ANALYZE_PERFORMANCE` use `BALANCED` execution. `ANALYZE_NETWORKING`,
`ANALYZE_SECURITY`, and `COMPARE_AGENT_REPORTS` use `NETWORKED_WORKSPACE` so their
shell checks can reach network resources when justified. All tool-enabled executions
remain isolated from the source repository by Prompt Runner.

## Configuration

- `conf/projects/<PROJECT>/<PROMPT>.yaml` owns prompt text and execution policy.
- Invocation-level `--model` and `--reasoning` values override only their corresponding
  YAML defaults; risk profiles continue to come from YAML.
- `conf/vars/<VARIABLE>.yaml` owns reusable string values.
- Reconnaissance uses its self-contained survey instructions without `ANALYZE_HEADER`;
  the six deep specialists use the shared defect-analysis instructions.
- Repository variables may reference `{{VAR:NAME}}`; missing references and cycles
  fail before execution.
- `ARG_DIRECTORY` and every `<PROMPT_NAME>_OUTPUT` are reserved runtime values.
  Configured variables and callers cannot override them.
- A prompt output becomes available only after that prompt succeeds. Specialist
  outputs remain opaque and are supplied to the comparison by stable prompt name,
  independent of parallel completion order.
- `REVIEW_DIRECTORY_CONTEXT` injects the same target boundary into all active prompts.

The v2 result contract is [docs/contracts/code-review-result.schema.json](docs/contracts/code-review-result.schema.json).
It exposes `report_project_name`, `report_run_id`, `report_directory`, and a complete
`report_paths` map. `specialist_reviews` contains the six active specialist reports;
`ANALYZE_RECONNAISSANCE` and its output remain absent while that specialist is disabled.
Machine-readable ownership and workflow records are under `docs/architecture/modules/`
and `docs/architecture/features/`.

## Verify

```bash
uv run pytest
uv run ruff check .
uv build
uvx --from "git+https://github.com/squizzster/MODULAR_VERTICAL_ARCHITECTURE-MODULAR_VERTICAL_ARCHITECTURE.git@v0.3.0" mva validate
```

## Current limits

- Specialist reports are passed to the comparison command as process arguments, so
  host argument-size limits bound unusually large combined reports.
- Runs have no persisted resume state. Process interruption relies on Prompt Runner
  and operating-system child-process termination.
- Generated report runs are retained until an operator removes or archives them.
- Prompt Runner does not yet wait for an acknowledgment after advertising the
  workspace path. Adding that handshake remains a TODO.
