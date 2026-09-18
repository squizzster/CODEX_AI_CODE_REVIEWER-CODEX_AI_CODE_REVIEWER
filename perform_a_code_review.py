#!/usr/bin/env -S uv run python
"""Executable project shim for the code-review pipeline."""

from codex_ai_code_reviewer.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
