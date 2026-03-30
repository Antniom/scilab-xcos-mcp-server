This directory contains per-source markdown logs used by the `knowledge-and-error-log` skill.

Naming convention:
- Use the source filename with `.md` appended, e.g. `analyze_xcos.py.md`.

Entry format (markdown):
- Header: `### TIMESTAMP UTC — <Type>`
- `- **Summary:**` one-line summary
- optional body lines each prefixed with `- `
- `- **Files:**` comma separated affected files

Use `log_writer.py` next to this directory to append entries programmatically.
