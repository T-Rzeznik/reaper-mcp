# Development Status — reaper-mcp

_Last updated: 2026-06-07_

Snapshot of where the build-out left off so work can resume cleanly.

---

## Summary

Expanded and hardened the MCP server using the **mcp-builder** skill: applied its
quality standards to the existing tools, added new capability areas, set up
auto-start, and built a live evaluation fixture. Server now exposes **51 tools**.

---

## Done

### Server refactor (`reaper_mcp/server.py`)
- All tools renamed with a **`reaper_` prefix** (e.g. `reaper_create_track`).
- Inputs validated with **`Annotated[..., Field(...)]`** + **Enums**
  (`ResponseFormat`, `AutomationMode`, `EnvelopeTarget`, `EnvelopeShape`) — flat schemas.
- Every tool has **behavioural annotations** (`readOnlyHint` / `destructiveHint` /
  `idempotentHint` / `openWorldHint`).
- Read tools take **`response_format`** (markdown default / json) via `_render`/`_to_markdown`.
- Mutation tools return small **structured dicts**.
- **Errors raise** (FastMCP `isError`) instead of returning `{"error": ...}` —
  fixes an output-validation crash on tools with typed return annotations.
- `reaper_list_installed_fx` gained pagination.

### New capability tools (server + matching bridge handlers)
Master track, time selection & loop, markers & regions, track sends/routing,
media/MIDI items (incl. MIDI note writing), render-with-last-settings.

### Bridge (`reaper_scripts/reaper_mcp_bridge.py`)
- New `h_*` handlers for all of the above, registered in `HANDLERS`.
- `ping` now returns **`bridge_version`** (reload detector).
- **Bug fix — send destination:** `GetTrackSendInfo_Value(...,"P_DESTTRACK")` returns
  a float, not a handle; resolved via `_ptr_to_int` / `_resolve_track_index`. **Verified live.**

### Environment / runtime (this machine)
- Python ReaScript already configured (x64 Python 3.12) — confirmed in `reaper.ini`.
- Bridge deployed to `%APPDATA%\REAPER\Scripts\reaper_mcp_bridge.py`.
- **Auto-start configured:** `%APPDATA%\REAPER\Scripts\__startup.lua` runs the bridge
  action on every REAPER launch (native mechanism, no SWS). **Verified working.**

### Docs & evals
- `README.md`, `CLAUDE.md` updated for new names/conventions + ReaScript quirks.
- `evaluations/` — 10-question read-only suite (`reaper_eval.xml`), reference
  project spec (`fixture.md`), runner notes (`evaluations/README.md`).
- All 10 eval answers verified against the live fixture.

---

## Known limitation (not fixable from the handler)

**Marker/region names can't be read back.** REAPER's Python ReaScript build doesn't
marshal the `char**` name out-param of `EnumProjectMarkers`/`EnumProjectMarkers2`.
Names are *written* (set via `AddProjectMarker2` + `SetProjectMarker3`, visible in
Reaper's ruler) but `reaper_list_markers` returns empty `name` fields. Markers are
identified by position/id. Eval questions were reworded to avoid name read-back.

---

## Current runtime state (will reset when REAPER restarts)

- **Running bridge: v4** (still has a temp `h_diag` handler in memory).
- **Deployed/source bridge: v5** (clean, `h_diag` removed) — loads on next REAPER launch.
- A throwaway **untitled tab** holds the freshly built fixture (NOT saved to disk).
- `working_shjits.rpp` is restored clean (reloaded from its untouched 6/5 disk save).

---

## Next steps / TODO

- [ ] **Reconnect the MCP server** in Claude Code (`/mcp` → reconnect `reaper`) so the
      new `reaper_`-prefixed tools replace the old unprefixed ones on the client side.
      (All build/test so far was driven directly over the bridge socket via `BridgeClient`,
      so the client was never restarted.)
- [ ] Optionally **save the fixture** as `MCP Eval Session.rpp` for repeatable eval runs
      (currently only live in the untitled tab).
- [ ] Optionally **run the eval harness**: `python scripts/evaluation.py -t stdio
      -c <venv python> -a -m reaper_mcp.server evaluations/reaper_eval.xml`
      (needs `ANTHROPIC_API_KEY`; billable).
- [ ] Smoke-test the remaining new tools not exercised yet: `reaper_render_project`,
      `reaper_delete_item`, `reaper_remove_send`, `reaper_goto_marker`,
      `reaper_set_loop_enabled`, `reaper_clear_time_selection`, `reaper_set_master_volume_db`.
- [ ] (Stretch) Investigate any alternative read path for marker names (project state
      chunk parsing) if names-in-listing become important.

---

## How to resume / verify

```powershell
# Static checks (no REAPER needed)
.\.venv\Scripts\python.exe -c "import reaper_mcp.server"          # should import; 51 tools
.\.venv\Scripts\python.exe -m py_compile reaper_scripts\reaper_mcp_bridge.py

# Live check (REAPER running, bridge auto-started)
.\.venv\Scripts\python.exe -c "from reaper_mcp.bridge import BridgeClient; print(BridgeClient().call('ping'))"
# -> {'pong': True, 'reaper_version': '7.49/x64', 'bridge_version': 5}  (5 after next REAPER restart)
```

Note: the bridge can be driven directly via `reaper_mcp.bridge.BridgeClient` (raw TCP)
without restarting the MCP server — useful for testing handler changes. New bridge
handlers only take effect after REAPER reloads the script (restart REAPER → `__startup.lua`).

---

## Operating note

Before ANY write operation against a live REAPER, call `reaper_get_project_info` first
and confirm it's an empty/scratch project — auto-start reopens the user's last project,
which is easy to mistake for empty.
