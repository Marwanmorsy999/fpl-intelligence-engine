# Agent Instructions

This project uses **CodeGraph CLI** for codebase indexing and analysis.

## Available Commands

- `codegraph status` - Check index status (up-to-date, pending changes)
- `codegraph sync` - Re-index changed files
- `codegraph query <symbol>` - Find symbol definitions/references
- `codegraph deps <file>` - Show dependencies for a file
- `codegraph impact <symbol>` - Show impact analysis for changes

## When to Use

Agents should run `codegraph status` at the start of any code exploration task to verify the index is current. If not, run `codegraph sync` before querying.

## Example Workflow

```bash
codegraph status          # Check if index is fresh
codegraph query User      # Find all User references
codegraph deps src/auth.py  # Show import graph
```