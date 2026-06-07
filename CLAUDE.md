# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An MCP server that lets an MCP client (Claude Code) drive the [Reaper](https://www.reaper.fm/) DAW — manage tracks, FX, presets, parameters, automation envelopes, recording, and the transport.

## The three-process architecture

This is the key thing to understand before touching anything. Control flows across three separate processes:

```
MCP client  ──stdio──▶  reaper_mcp.server  ──TCP 127.0.0.1:8765──▶  reaper_mcp_bridge.py
(Claude)                (this package)        newline-JSON          (Python ReaScript inside Reaper)
```

1. **`reaper_mcp/server.py`** — a FastMCP server (~51 tools). Every `@mcp.tool()` is a thin wrapper that calls `_call("method_name", ...)`, which forwards to the bridge over TCP. The one exception is `reaper_list_installed_fx` (see below). Started via `python -m reaper_mcp.server` or the `reaper-mcp` console script.
2. **`reaper_mcp/bridge.py`** — `BridgeClient`, a synchronous TCP client. Opens a fresh connection per call, sends one newline-terminated JSON request `{id, method, params}`, reads one newline-terminated JSON response `{id, ok, result|error}`. Raises `BridgeError` on `ok=false`; `server._call` catches it and returns `{"error": ...}` so the model can reason about failures instead of crashing.
3. **`reaper_scripts/reaper_mcp_bridge.py`** — a Python ReaScript that runs *inside* Reaper. It opens a non-blocking TCP listener and pumps it from `RPR_defer("_tick()")` so the DAW UI never blocks. `HANDLERS` maps method name → `h_*` function.

**A tool spans two files.** Adding/changing a capability almost always means editing both `server.py` (the `@mcp.tool` wrapper + its method name) and `reaper_scripts/reaper_mcp_bridge.py` (the matching `h_*` handler + its `HANDLERS` entry). The method-name string passed to `_call(...)` is the contract between them and must match the `HANDLERS` key exactly on both sides. Note the two namespaces deliberately differ: tools are `reaper_`-prefixed (`reaper_create_track`) but the wire method and handler are not (`create_track` / `h_create_track`).

## Tool conventions in server.py (mcp-builder standards)

These patterns are applied uniformly — match them when adding tools:
- **Naming:** tool names are `reaper_<verb>_<noun>`; the FastMCP server name is `reaper_mcp`.
- **Input validation:** parameters use `Annotated[type, Field(description=..., ge=..., le=...)]` (the per-parameter form keeps a *flat* input schema — a single `params: BaseModel` arg would nest everything under `params`). Constrained string choices are `Enum`s (`AutomationMode`, `EnvelopeTarget`, `EnvelopeShape`, `ResponseFormat`).
- **Annotations:** every `@mcp.tool` declares `readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint`.
- **Read vs mutation returns:** read tools take a `response_format` arg and return `str` via `_render()` / `_to_markdown()` (markdown default). Mutation tools return a small `dict` (FastMCP structured output).
- **Errors raise, never return.** `_call` raises `RuntimeError` on a `BridgeError` so FastMCP emits an `isError` result. Do **not** return `{"error": ...}` — with typed return annotations that triggers a FastMCP output-validation error instead of a clean message.

## Conventions that matter

- **Every handler call is one undo step.** `_dispatch` wraps each handler in `RPR_Undo_BeginBlock()` / `RPR_Undo_EndBlock("MCP: " + method, -1)`. New handlers automatically inherit this — don't add your own undo blocks.
- **The bridge is stdlib-only.** Reaper's embedded Python cannot install packages. `reaper_scripts/reaper_mcp_bridge.py` may only import the standard library plus `reaper_python` (the `RPR_*` SWIG API).
- **SWIG string-out pattern.** `RPR_*` functions that return strings take an empty placeholder string + buffer size, then the value comes back in the returned *tuple* at a specific index — e.g. `RPR_GetTrackName(track, "", 256)[2]`, `RPR_TrackFX_GetFXName(track, fx, "", 256)[3]`. The helper wrappers near the top of the bridge (`_track_name`, `_fx_name`, etc.) exist to hide this; reuse them.
- **Null pointers come back as strings.** Reaper returns null handles as `"0x0000000000000000"`-style strings, so use `_is_null_ptr()` rather than truthiness checks (see `_track_at`).
- **Known ReaScript Python binding quirks (verified live, REAPER 7.49/x64):**
  - **Pointer-returning `*_Value` calls return a float, not a handle.** `GetTrackSendInfo_Value(...,"P_DESTTRACK")` returns the track address as a `float` (e.g. `36813792.0`), while `GetTrack()` returns a `'(MediaTrack*)0x...'` string. To map one to the other, normalise both to ints — see `_ptr_to_int` / `_resolve_track_index`.
  - **`EnumProjectMarkers`/`EnumProjectMarkers2` don't return the marker name.** The `char**` name out-param isn't marshaled, so `list_markers` always reports `name == ""` even though the name is set (it shows in Reaper's UI). `add_marker` sets it via `AddProjectMarker2` + `SetProjectMarker3`; there's no known read-back path in this build. Identify markers by position/id.
- **dB vs linear.** Reaper stores volume as linear gain. `db_to_linear`/`linear_to_db` (bridge) bound `-150 dB` to `0.0`. The `set_track_volume_db` tool and `value_is_db` on `add_envelope_point` convert at the boundary.
- **`reaper_list_installed_fx` bypasses the bridge entirely.** The SWIG wrapper for `EnumInstalledFX` is broken, so `server.py` parses Reaper's own scan-cache `.ini` files directly from the Reaper config dir (`_reaper_config_dir`, overridable via `REAPER_CONFIG_DIR`). This works only because the MCP server runs on the same machine as Reaper. If adding FX-discovery behavior, edit the `_parse_*_cache` functions in `server.py`, not the bridge.

## Common commands

```powershell
# Install (editable) into the project venv
py -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -e .

# Run the MCP server standalone (it speaks MCP over stdio; mostly run by the client)
python -m reaper_mcp.server
```

The bridge is **not** launched from the shell — it runs inside Reaper. After editing `reaper_scripts/reaper_mcp_bridge.py`, copy it to `%APPDATA%\REAPER\Scripts\` and re-run it from Reaper's action list (Actions → ReaScript: Load), then restart the MCP server in the client. There is currently no test suite, linter config, or build step beyond the hatchling wheel build.

## Verifying a change end-to-end

There is no automated test harness. Two levels of checking:

- **Static (no Reaper needed):** `.venv\Scripts\python.exe -c "import reaper_mcp.server"` executes every `@mcp.tool` decorator and validates all input schemas; `python -m py_compile reaper_scripts/reaper_mcp_bridge.py` syntax-checks the bridge (it can't be imported off-Reaper — it does `from reaper_python import *`).
- **Live:** needs a running Reaper with the bridge loaded. Smoke-test with `reaper_ping` (returns Reaper's version), then exercise the specific tool you touched. Bridge exceptions come back as `{"ok": false, "error": ..., "trace": ...}` with a full traceback — read the `trace` field when a handler misbehaves. New handlers only take effect after re-loading the bridge in Reaper **and** restarting the MCP server.

## Config

`REAPER_MCP_HOST` (default `127.0.0.1`) and `REAPER_MCP_PORT` (default `8765`) — the port must match on **both** the server (`bridge.py`) and the bridge ReaScript. `REAPER_CONFIG_DIR` overrides where `list_installed_fx` looks for scan caches.
