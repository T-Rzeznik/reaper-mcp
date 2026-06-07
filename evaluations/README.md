# Evaluations

mcp-builder Phase 4 evaluation suite for `reaper-mcp`.

- `fixture.md` — the "MCP Eval Session" reference project the questions are keyed to.
- `reaper_eval.xml` — 10 read-only, single-answer questions (verifiable by string comparison).

## Why a fixture?

Eval answers must be **stable**. Unlike a SaaS API with a shared historical dataset, a Reaper
project is local and editable, so there is no naturally stable data to ask about. Instead the
answers are defined by a fixed reference project (`fixture.md`). Load that project first; then
every answer in `reaper_eval.xml` is deterministic.

## Running

The eval harness ships with the mcp-builder skill (`scripts/evaluation.py`). It launches the
server over stdio and drives it with an LLM:

```bash
export ANTHROPIC_API_KEY=...
python scripts/evaluation.py \
  -t stdio \
  -c "C:/Users/tommy/Desktop/CODING STUFF/reaper-mcp/.venv/Scripts/python.exe" \
  -a -m reaper_mcp.server \
  -o report.md \
  evaluations/reaper_eval.xml
```

Prerequisites: Reaper running with the bridge loaded **and** the `MCP Eval Session` project open,
otherwise the read tools have nothing to answer from.

## Note on verification

These answers are correct by construction of `fixture.md`. They have **not** yet been confirmed
end-to-end against a live Reaper (the bridge was offline when the suite was authored). Run the
harness once the fixture project is loaded to confirm, and adjust any answer the run reveals as
environment-dependent.
