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
```

The review directory must exist and be readable and traversable. It becomes the
working directory of the Bash launcher and Python reviewer process, the Prompt Runner
`--cwd`, and the `ARG_DIRECTORY` runtime variable. Tool-enabled Prompt Runner profiles
execute Codex inside a safety-created isolated workspace derived from that directory;
the original target path and isolated execution workspace are intentionally distinct.
`--model` and `--reasoning` apply to every specialist and the comparison for that run
without changing the defaults stored in prompt YAML or the Prompt Runner catalogue.

## Pipeline

1. Validate repository-owned YAML and synchronize the Prompt Runner catalogue.
2. Run `ANALYZE_PIPELINE`, `ANALYZE_BOUNDARIES`, `ANALYZE_NETWORKING`,
   `ANALYZE_INTEGRITY`, `ANALYZE_SECURITY`, `ANALYZE_PERFORMANCE`, and
   `ANALYZE_RECONNAISSANCE` in parallel.
3. Publish each successful result internally as `{{VAR:<PROMPT_NAME>_OUTPUT}}`.
4. Run `COMPARE_AGENT_REPORTS` only after all specialist reports succeed, with each
   report bound by prompt identity rather than completion order.
5. Atomically publish the synthesis to `/tmp/final_review.md` and emit the complete
   versioned result as one JSON object on stdout.

Progress from Prompt Runner is forwarded to stderr. Expected input, configuration,
catalogue, and execution failures return exit code `2`; no comparison or final report
is published from an incomplete specialist stage.

`ANALYZE_PIPELINE`, `ANALYZE_BOUNDARIES`, `ANALYZE_INTEGRITY`, `ANALYZE_SECURITY`,
`ANALYZE_PERFORMANCE`, and `ANALYZE_RECONNAISSANCE` use `BALANCED` execution.
`ANALYZE_NETWORKING` and `COMPARE_AGENT_REPORTS` use `NETWORKED_WORKSPACE` so their
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
- `REVIEW_DIRECTORY_CONTEXT` injects the same target boundary into all eight prompts.

The result contract is [docs/contracts/code-review-result.schema.json](docs/contracts/code-review-result.schema.json).
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
- The successful Markdown publication path is fixed at `/tmp/final_review.md`.
