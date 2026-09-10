# Python Quality Gates

[CONTRIBUTING.md](../../../CONTRIBUTING.md) defines the current checks and release
gates. Read [validation contracts](../../../docs/agent-guides/validation.md)
before changing tests, lint coverage, preflight, or evidence generation.

- Run affected offline tests while iterating. Run the required repository
  checks for a completed code change. The default pytest suite is offline;
  `-m live` uses the real browser and has separate preflight and ownership rules.
- Use `python -m scripts.lint_report` for Ruff, ESLint, and mypy. Preserve the
  existing formatting style and LF line endings.
- Public tool changes require the MCP description, both README tables, and both
  packaged caller skills to agree. Read
  [tool contracts](../../../docs/agent-guides/tool-contracts.md) and run
  `python -m scripts.check_tool_docs`.
- For changes confined to agent documentation, start with
  `python -m pytest tests/test_documentation_contract.py -q`.
- Run the release finalizer only when preparing a release candidate. Preserve
  sealed artifacts and put ad hoc evidence in a separate output directory.

Use [test_documentation_contract.py](../../../tests/test_documentation_contract.py)
as the example for checking published-guide links and machine-local exclusions.
