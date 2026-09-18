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
`.yaml` filename defines a prompt name. Initialization currently registers missing
projects and prompts; code analysis is the next experimental slice. The sibling
`CODEX_PROMPT_RUNNER_SYSTEM` checkout is used by default; set
`CODEX_PROMPT_RUNNER_PROJECT_ROOT` to override its location.

Reusable Prompt Runner values live as YAML string scalars under `conf/vars/`; each
filename is its variable name. For example, `ANALYZE_HEADER.yaml` supplies
`{{VAR:ANALYZE_HEADER}}` when a later prompt build or run requests that variable.
