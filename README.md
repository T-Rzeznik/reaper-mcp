# reaper-mcp

An MCP server that gives Claude Code (or any MCP client) full control of [Reaper](https://www.reaper.fm/).

Claude can:

- List every VST/VST3/CLAP/JS/AU plugin you have scanned
- Add plugins to tracks, set parameters, and pick presets
- Move volume faders, pan, mute, solo (track and master)
- Write automation envelopes (track volume/pan or any FX parameter)
- Set up track sends / routing
- Create/rename/delete tracks; create/delete media & MIDI items and write MIDI notes (one at a time or a whole part in one batch call)
- Add markers and regions; set the time selection and toggle looping
- Arm tracks and drive the transport (play / stop / record)
- Render the project using the last-used render settings
- Trigger any Reaper action by command ID (escape hatch)

All 54 tools are namespaced with a `reaper_` prefix (e.g. `reaper_create_track`) so they
don't collide with other MCP servers. Read tools accept a `response_format` argument
(`markdown` for humans, `json` for machines).

## How it works

```
Claude Code  ──stdio──▶  reaper-mcp server  ──TCP 127.0.0.1:8765──▶  Reaper bridge ReaScript
   (MCP)                  (this package)                              (runs inside Reaper)
```

The bridge is a Python ReaScript that lives inside Reaper. It opens a non-blocking
TCP listener and polls it from `reaper.defer` so the DAW UI never freezes.
Every mutation is wrapped in `Undo_BeginBlock` / `Undo_EndBlock`, so anything Claude does
is a single undo step.

## Prerequisites

- Reaper installed (default location: `C:\Program Files\REAPER (x64)\`)
- Python ReaScript enabled in Reaper. Open **Options → Preferences → Plug-ins → ReaScript**
  and point "Custom path to Python dll" at your Python install (e.g. `C:\Users\<you>\AppData\Local\Programs\Python\Python311\`).
  Restart Reaper. The page should say `Python loaded successfully`.
- Python 3.10+ on your host machine for the MCP server itself.

## Install

```powershell
cd "C:\Users\tommy\Desktop\CODING STUFF\reaper-mcp"
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

## Launch the bridge inside Reaper

1. Copy `reaper_scripts\reaper_mcp_bridge.py` into `%APPDATA%\REAPER\Scripts\`.
2. In Reaper: **Actions → Show action list → ReaScript: Load → pick the file → Run**.
3. You should see `[reaper-mcp] bridge listening on 127.0.0.1:8765` in the ReaScript console.

Optional: in the action list, right-click the loaded action and **"Add to toolbar"**, so you can start the bridge with one click. To make it auto-start with Reaper, install [SWS Extension](https://www.sws-extension.org/) and use **SWS: Set startup action**.

## Wire it up to Claude Code

Add to your Claude Code MCP config (`%APPDATA%\Claude\claude_desktop_config.json` for Claude Desktop, or `~/.claude.json` / project settings for Claude Code):

```json
{
  "mcpServers": {
    "reaper": {
      "command": "C:\\Users\\tommy\\Desktop\\CODING STUFF\\reaper-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "reaper_mcp.server"]
    }
  }
}
```

Restart Claude Code. Verify with the **`reaper_ping`** tool — it should return Reaper's version.

## Try it

Ask Claude things like:

- *"List every synth I have installed."* → `reaper_list_installed_fx` filtered to instruments
- *"Make a new track called 'Bass', drop Serum on it, and switch to the first preset."* → `reaper_create_track` → `reaper_add_fx_to_track` → `reaper_list_fx_presets` → `reaper_set_fx_preset`
- *"Automate the volume of track 1 to fade in over the first 4 seconds."* → `reaper_add_envelope_point` × 2
- *"Send track 2 to a reverb bus and pull the send down 6 dB."* → `reaper_add_send` → `reaper_set_send_volume_db`
- *"Drop a 2-bar MIDI clip on track 3 and write a C major chord."* → `reaper_insert_midi_item` → `reaper_add_midi_notes` (all 3 notes in one call)
- *"Mark the chorus at 32 seconds."* → `reaper_add_marker`
- *"Arm track 1 and start recording."* → `reaper_set_track_record_arm` → `reaper_transport_record`

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `REAPER_MCP_HOST` | `127.0.0.1` | Where the MCP server looks for the bridge |
| `REAPER_MCP_PORT` | `8765` | Bridge TCP port (set on **both** sides if you change it) |

## Troubleshooting

- **`could not reach Reaper bridge`** — the bridge script isn't running. Re-load it via the action list. Check Reaper's ReaScript console for errors.
- **`Python ReaScript not loaded`** in Reaper — point Preferences → Plug-ins → ReaScript at a Python install of the same bitness (Python 3.x x64 for Reaper x64) and restart.
- **`could not add FX 'X' (not found?)`** — call `reaper_list_installed_fx` and copy the exact name (including the `VST3:` / `VST3i:` prefix). Reaper matches by exact suffix.
- **Preset name doesn't match** — some plugins expose presets as `.fxp` files in `%APPDATA%\REAPER\presets\vst-<plugin>\`. Call `reaper_list_fx_presets` to see what Reaper actually sees.
- **Automation doesn't seem to do anything** — set the track to `read` mode: `reaper_set_track_automation_mode(idx, "read")`.

## Adding new capabilities

To add a tool:

1. Write an `h_<method>` handler in `reaper_scripts/reaper_mcp_bridge.py` and register it in `HANDLERS`.
2. Add a `@mcp.tool(name="reaper_<verb_noun>", annotations={...})` wrapper in `reaper_mcp/server.py`
   that calls `_call("<method>", ...)`. Validate inputs with `Annotated[type, Field(...)]` and `Enum`
   types, give read tools a `response_format` argument, and let failures raise (do **not** return an
   error dict — `_call` raises so FastMCP reports it as an `isError` result).
3. Re-load the bridge script in Reaper (Actions list → ReaScript: Load) and restart the MCP server in Claude Code.

The method-name string is the contract between the two files and must match exactly on both sides.

## Evaluations

`evaluations/reaper_eval.xml` holds read-only eval questions (mcp-builder Phase 4) for checking that an
LLM can drive the server. See `evaluations/README.md` for how to run them and verify answers against a
live project.
