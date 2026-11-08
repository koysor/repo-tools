---
name: marimo-helper
description: Rules for writing and editing marimo notebooks (reactive cells, unique global names, browser/Pyodide limits). Use when creating, editing or fixing any marimo notebook in the koysor repos.
---

# Marimo Notebook Development Skill

Marimo is a reactive notebook environment where each cell is a Python function and the notebook is a dependency graph of those cells. These rules apply in every repo; the repo's own `CLAUDE.md` and its `new-notebook` skill (where there is one) add the repo-specific parts.

## Core rules

### 1. Variable isolation and redefinition
Marimo fails if a global variable is defined in more than one cell.
- **Loop and temporary variables:** prefix them with an underscore (e.g. `for _sym, _df in multi_data.items():`) to keep them local to the cell.
- **Common names:** avoid generic globals like `df`, `data`, `fig`, `ax`, `x` or `symbol` unless they are the sole definition in the notebook. Prefer descriptive, unique names like `df_aapl` or `fig_convergence`.

### 2. Reactive dependency management
Marimo tracks dependencies through the arguments of each `@app.cell` function and the names it returns.
- If cell B uses a variable defined in cell A, cell A must return it and cell B must take it as an argument.
- Avoid hidden state, mutation of another cell's objects, or side effects that bypass the dependency graph.

### 3. Structure
- Put imports in one cell at the top (`import marimo as mo` plus the packages needed) and return them.
- End a cell with the object to display (a figure, `mo.md(...)`, a widget); widgets are read through `.value` in other cells.
- Use `mo.show_code()` where the code itself is part of the lesson, so it also appears in the read-only published view.

### 4. Where the notebook runs
- Notebooks in a repo's `notebooks/` folder are published to GitHub Pages as WebAssembly and run in Pyodide: no network, files, databases or the repo's own library. Check the repo's `CLAUDE.md` or `new-notebook` skill for exactly which imports are allowed and where notebooks that need live data go (e.g. `notebooks/local/` in quant-finance).

## Validation

- Check that no global name is defined in two cells.
- Run `uv run marimo check <path>` and fix what it reports.
- Run the notebook as a script (`MPLBACKEND=Agg uv run python <path>`) to confirm every cell executes.
