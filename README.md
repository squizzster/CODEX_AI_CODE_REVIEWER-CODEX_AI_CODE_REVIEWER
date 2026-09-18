# CODEX AI Code Reviewer

An experimental AI-assisted code-review system for evaluating changes and producing actionable review feedback.

## Development mode

**EXP** — rapid, evidence-led experimentation. The first useful review pipeline will be tested against representative code changes before the design is expanded.

## Current workflow

```bash
uv sync --group dev
./perform_a_code_review.py /path/to/repository
```

On startup, directories beneath `conf/projects/` define Prompt Runner projects and each
`.yaml` filename defines a prompt name. Each file stores its prompt text under a
`prompt: |` block scalar. Initialization registers missing projects and prompts, and
publishes a new immutable version when configured prompt bytes or defaults change.
`ANALYZE_PIPELINE`, `ANALYZE_BOUNDARIES`, and `ANALYZE_NETWORKING` then run live in
parallel. Their complete reports become the three inputs to `COMPARE_AGENT_REPORTS`,
whose audited synthesis is returned as the final `review`; the source reports remain in
`specialist_reviews`. Structured progress is forwarded to stderr and one final JSON
result is written to stdout. The audited Markdown report is also atomically published to
`/tmp/final_review.md` after successful completion. The sibling
`CODEX_PROMPT_RUNNER_SYSTEM` checkout is used by default; set
`CODEX_PROMPT_RUNNER_PROJECT_ROOT` to override its location.

Reusable Prompt Runner values live as YAML string scalars under `conf/vars/`; each
filename is its variable name. For example, `ANALYZE_HEADER.yaml` supplies
`{{VAR:ANALYZE_HEADER}}` when a later prompt build or run requests that variable.
Repository-owned variables may reference other repository-owned variables; these
references are resolved before Prompt Runner execution with missing-reference and cycle
validation. Runtime agent reports remain opaque and are never recursively expanded.
The required review directory is resolved, checked for read/traverse access, and exposed
to that same pipeline as the reserved runtime value `{{VAR:ARG_DIRECTORY}}`. The shared
`REVIEW_DIRECTORY_CONTEXT` injects this boundary identically into all four prompts.
