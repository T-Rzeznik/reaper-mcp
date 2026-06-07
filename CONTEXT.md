# reaper-mcp — session context

Snapshot of state as of last working session. Goal: let Claude Code drive all of Reaper — list VSTs, add to tracks, switch presets, move faders, write automation, arm + record.

## Status: working end-to-end

The full loop is verified: Claude Code → MCP server → TCP → in-Reaper Python ReaScript → Reaper API.

Smoke-tested with the user's live project (`working_shjits.rpp`, 5 tracks, 140 BPM). 32 MCP tools registered. 338 plugins discovered (71 VST3, 24 VST, 223 JS, 2 CLAP; 18 instruments).

**Only thing the user needs to do to use it: restart Claude Code.** The `reaper` MCP server is already wired into `~/.claude.json` and will spawn on next launch. The bridge inside Reaper is currently running — it needs to be re-loaded from the action list any time Reaper is restarted (or installed as a startup action via SWS).

## Architecture

```
┌─────────────┐  stdio  ┌──────────────────┐  TCP 127.0.0.1:8765  ┌──────────────────┐
│ Claude Code │ ◄─────► │ MCP server       │ ◄──────────────────► │ Reaper bridge    │
│   (host)    │  MCP    │ reaper_mcp.server│  newline-JSON        │ ReaScript inside │
└─────────────┘         └──────────────────┘                      │ Reaper           │
                                │                                 └──────────────────┘
                                │ reads directly
                                ▼
                        Reaper cache files
                        (FX discovery only)
```

**Why split:** Reaper's embedded Python runs in the main thread and can only do brief work per tick. The bridge polls a non-blocking socket via `reaper.defer` so the DAW UI never freezes. The MCP server is a normal Python process — decouples MCP protocol from Reaper internals, and either side can be hot-reloaded.

**Undo:** every bridge command is wrapped in `Undo_BeginBlock`/`Undo_EndBlock`. One MCP call = one undo step in Reaper.

**FX discovery is host-side**, not bridge-side. The SWIG wrapper for `EnumInstalledFX` is broken (uses `char**` which can't roundtrip through Python). Instead the host parses Reaper's own scan-cache files: `reaper-vstplugins64.ini`, `reaper-clap-win64.ini`, `reaper-jsfx.ini`.

## Layout

```
reaper-mcp/
├── pyproject.toml             # mcp[cli]>=1.2.0, entry point reaper-mcp
├── README.md                  # user-facing install + usage
├── CONTEXT.md                 # this file
├── smoke_test.py              # standalone end-to-end check (run with the venv python)
├── reaper_mcp/
│   ├── __init__.py
│   ├── bridge.py              # synchronous TCP client. raises BridgeError on ok=false
│   └── server.py              # FastMCP server. all 32 @mcp.tool()s live here.
│                              # also contains the cache-file parsers for FX discovery.
└── reaper_scripts/
    └── reaper_mcp_bridge.py   # Python ReaScript. mirrored to %APPDATA%\REAPER\Scripts\
```

## Three gotchas we learned the hard way (don't re-discover these)

1. **`reaper_python` module exposes everything with the `RPR_` prefix.** There is NO friendly `reaper.X` import in Python ReaScript (that's Lua-only). Use `from reaper_python import *` and call `RPR_*` directly.

2. **`defer` and `atexit` take strings of code, not callables.** Correct: `RPR_defer("_tick()")`. The string is later `eval`'d in the script's global scope. Pass the function name as a string — passing a function reference silently does nothing.

3. **String-out functions need explicit buffer args, then unpack from a tuple.** Pattern: `RPR_GetTrackName(track, "", 256)` returns `(retval, track, name, sz)` → name at `[2]`. Tuple position depends on where the buffer sits in the C signature (1-indexed; the C return is at `[0]`). Helpers `_track_name`, `_fx_name`, `_fx_preset`, `_fx_param_name`, `_project_name` in the bridge encapsulate this.

   **Exception:** `RPR_EnumInstalledFX` is wrapped with `char**` and returns just the bool — output strings are unreachable from Python. Don't try to use it; parse the cache files instead.

## Tool surface (32 tools, all in `reaper_mcp/server.py`)

- **diagnostics**: `ping`, `get_project_info`
- **discovery (host-side)**: `list_installed_fx(filter, instruments_only)`
- **tracks**: `list_tracks`, `create_track`, `delete_track`, `get_track_state`, `rename_track`, `set_track_volume_db`, `set_track_pan`, `set_track_mute`, `set_track_solo`
- **FX on track**: `list_track_fx`, `add_fx_to_track`, `remove_fx`, `set_fx_enabled`
- **presets**: `list_fx_presets`, `set_fx_preset` (by name or index)
- **FX params**: `list_fx_params`, `set_fx_param` (param by index or name)
- **automation**: `add_envelope_point`, `clear_envelope`, `set_track_automation_mode`
- **recording**: `set_track_record_arm`, `set_track_record_input`
- **transport**: `transport_play`, `transport_stop`, `transport_record`, `transport_pause`, `set_cursor`, `set_tempo`
- **escape hatch**: `run_reaper_action(command_id)` for anything not wrapped

## Environment

- Reaper 7.49 x64 at `C:\Program Files\REAPER (x64)\` (user's machine, Windows)
- Python 3.12 x64 at `C:\Users\tommy\AppData\Local\Programs\Python\Python312\`
- ReaScript Python configured: directory `C:\Users\tommy\AppData\Local\Programs\Python\Python312` (no trailing slash!), DLL `python312.dll`
- Project venv: `reaper-mcp\.venv\` with reaper-mcp installed editable, mcp 1.27.1
- Bridge script lives at `C:\Users\tommy\AppData\Roaming\REAPER\Scripts\reaper_mcp_bridge.py` (also kept in repo at `reaper_scripts/`)
- MCP config: `C:\Users\tommy\.claude.json` (Claude Code) and `%APPDATA%\Claude\claude_desktop_config.json` (Claude Desktop) — both have `reaper` entry pointing at the venv python

## Resuming work

To pick up a session:

1. **Start Reaper** (if not already running).
2. **Load the bridge:** Actions → Show action list → search `reaper_mcp_bridge` → Run. Confirm console prints `[reaper-mcp] bridge listening on 127.0.0.1:8765`.
3. **Start Claude Code.** If it's already running, restart so it re-reads `.claude.json`.
4. **Verify with `ping` tool** before doing real work.

Outside Claude, run the end-to-end check yourself:

```powershell
& ".\.venv\Scripts\python.exe" smoke_test.py
```

## To extend

Adding a tool is mechanical: write a handler in the bridge, register it in `HANDLERS`, then add an `@mcp.tool()` wrapper in `server.py` calling `_call("your_method", ...)`. Reload bridge in Reaper, restart MCP server in Claude.

Use the helpers (`_track_at`, `_track_summary`, `_fx_name`, `_fx_preset`, `_resolve_param_index`) — they handle the buffer/null-pointer/index-lookup patterns. Wrap mutations in nothing — the dispatch loop already does `Undo_BeginBlock`/`Undo_EndBlock`.

## Not yet implemented (likely next slices)

- **MIDI editing**: insert MIDI items, add/edit/delete notes (`MIDI_InsertNote`, `MIDI_DeleteNote`, etc.)
- **Render / export**: trigger renders to file with format selection
- **Markers + regions**: create, name, navigate
- **FX chain templates**: load `.RfxChain` files onto tracks
- **Item-level operations**: select, move, copy, split items on the timeline
- **Send / receive routing**: create sends between tracks, set send levels
- **Take FX** (vs Track FX — same API family with `TakeFX_*` prefix)
- **Reading audio peaks** for visual feedback
- **MCU/control-surface-like batch ops** for transport with pre-roll, click, etc.

Most of these are a 20-line bridge handler plus a 5-line MCP tool. The plumbing is already in place.
