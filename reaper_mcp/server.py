"""MCP server exposing Reaper control via the in-Reaper ReaScript bridge.

Tool design follows the mcp-builder guidelines:
- every tool name is prefixed with `reaper_` to avoid collisions with other servers
- inputs are validated by Pydantic via `Annotated[..., Field(...)]` parameters and Enums
  (the per-parameter form keeps a flat, idiomatic FastMCP input schema)
- every tool carries behavioural annotations (readOnlyHint / destructiveHint / ...)
- read tools accept a `response_format` (markdown for humans, json for machines)
- mutation tools return small structured dicts; failures raise so the client sees isError
"""

from __future__ import annotations

import json
import os
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field
from mcp.server.fastmcp import FastMCP

from .bridge import BridgeClient, BridgeError

mcp = FastMCP("reaper_mcp")
bridge = BridgeClient()


# ---------------------------------------------------------------------------
# Enums for constrained inputs
# ---------------------------------------------------------------------------

class ResponseFormat(str, Enum):
    """Output format for read tools."""
    MARKDOWN = "markdown"
    JSON = "json"


class AutomationMode(str, Enum):
    """Track automation mode."""
    TRIM = "trim"
    READ = "read"
    TOUCH = "touch"
    WRITE = "write"
    LATCH = "latch"
    LATCH_PREVIEW = "latch_preview"


class EnvelopeTarget(str, Enum):
    """Which envelope an automation tool acts on."""
    VOLUME = "volume"
    PAN = "pan"
    FX_PARAM = "fx_param"


class EnvelopeShape(str, Enum):
    """Interpolation shape for an automation point."""
    LINEAR = "linear"
    SQUARE = "square"
    SLOW = "slow"
    FAST_START = "fast_start"
    FAST_END = "fast_end"
    BEZIER = "bezier"


class GeminiModel(str, Enum):
    """Which Gemini model listens to the mix in reaper_analyze_mix."""
    FLASH = "gemini-2.5-flash"
    PRO = "gemini-2.5-pro"


# ---------------------------------------------------------------------------
# Shared helpers — keep tool bodies thin and DRY.
# ---------------------------------------------------------------------------

def _call(method: str, **params: Any) -> Any:
    """Forward a method to the in-Reaper bridge, raising on failure.

    Raising (rather than returning an error dict) lets FastMCP surface the
    message as an isError tool result the model can read and act on.
    """
    try:
        return bridge.call(method, **params)
    except BridgeError as e:
        raise RuntimeError(str(e)) from e


def _to_markdown(data: Any) -> str:
    """Render bridge data as compact human-readable markdown.

    Lists of dicts become a table; a dict becomes a bullet list; anything
    else is stringified. Centralised so every read tool formats identically.
    """
    if isinstance(data, list):
        if not data:
            return "_(no results)_"
        if all(isinstance(row, dict) for row in data):
            cols: list[str] = []
            for row in data:
                for key in row:
                    if key not in cols:
                        cols.append(key)
            header = "| " + " | ".join(cols) + " |"
            sep = "| " + " | ".join("---" for _ in cols) + " |"
            body = [
                "| " + " | ".join(_cell(row.get(c, "")) for c in cols) + " |"
                for row in data
            ]
            return "\n".join([header, sep, *body])
        return "\n".join(f"- {item}" for item in data)
    if isinstance(data, dict):
        return "\n".join(f"- **{k}**: {v}" for k, v in data.items())
    return str(data)


def _cell(value: Any) -> str:
    """Stringify a table cell, keeping markdown tables single-line."""
    return str(value).replace("|", "\\|").replace("\n", " ")


def _render(data: Any, fmt: ResponseFormat) -> str:
    """Format read-tool output in the requested representation."""
    if fmt == ResponseFormat.JSON:
        return json.dumps(data, indent=2, default=str)
    return _to_markdown(data)


# Default for the response_format parameter, reused across all read tools.
_FMT = Annotated[
    ResponseFormat,
    Field(description="'markdown' for human-readable output or 'json' for machine-readable"),
]


# ---------------------------------------------------------------------------
# FX discovery — parse Reaper's own cache files directly.
# The SWIG wrapper for EnumInstalledFX is broken (char** roundtrip), so we
# read the files Reaper writes after a plugin scan. These live in REAPER's
# user config dir, on the same machine as this MCP server.
# ---------------------------------------------------------------------------

def _reaper_config_dir() -> Path:
    override = os.environ.get("REAPER_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "REAPER"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "REAPER"
    return Path.home() / ".config" / "REAPER"


def _parse_vst_cache(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("["):
            continue
        if "=" not in line:
            continue
        filename, _, rest = line.partition("=")
        parts = rest.split(",", 2)
        if len(parts) < 3:
            continue  # hash-only entries: scanned but no plugin metadata
        name_and_flags = parts[2]
        flags: list[str] = []
        if "!!!" in name_and_flags:
            name, _, flag_str = name_and_flags.partition("!!!")
            flags = flag_str.split("!!!")
        else:
            name = name_and_flags
        is_instrument = any("VSTi" in f or "VST3i" in f for f in flags)
        is_vst3 = filename.lower().endswith(".vst3")
        if is_vst3:
            kind = "VST3i" if is_instrument else "VST3"
        else:
            kind = "VSTi" if is_instrument else "VST"
        out.append({
            "name": f"{kind}: {name.strip()}",
            "kind": kind,
            "is_instrument": is_instrument,
            "filename": filename,
        })
    return out


def _parse_clap_cache(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    current_file: str | None = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current_file = line[1:-1]
            continue
        if line.startswith("_=") or not current_file or "=" not in line:
            continue
        _plugin_id, _, rest = line.partition("=")
        if "|" not in rest:
            continue
        type_str, _, name = rest.partition("|")
        try:
            is_instrument = int(type_str) == 1
        except ValueError:
            is_instrument = False
        kind = "CLAPi" if is_instrument else "CLAP"
        out.append({
            "name": f"{kind}: {name.strip()}",
            "kind": kind,
            "is_instrument": is_instrument,
            "filename": current_file,
        })
    return out


def _parse_jsfx_cache(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line.startswith("NAME "):
            continue
        rest = line[len("NAME "):]
        if " " not in rest:
            continue
        script_path, _, quoted = rest.partition(" ")
        name = quoted.strip().strip('"')
        out.append({
            "name": name,  # already prefixed with "JS:"
            "kind": "JS",
            "is_instrument": False,
            "filename": script_path,
        })
    return out


def _discover_installed_fx() -> list[dict]:
    cfg = _reaper_config_dir()
    items: list[dict] = []
    items += _parse_vst_cache(cfg / "reaper-vstplugins64.ini")
    items += _parse_vst_cache(cfg / "reaper-vstplugins.ini")  # 32-bit, harmless if absent
    items += _parse_clap_cache(cfg / "reaper-clap-win64.ini")
    items += _parse_clap_cache(cfg / "reaper-clap.ini")
    items += _parse_jsfx_cache(cfg / "reaper-jsfx.ini")
    return items


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

@mcp.tool(
    name="reaper_ping",
    annotations={
        "title": "Ping Reaper Bridge",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_ping(response_format: _FMT = ResponseFormat.MARKDOWN) -> str:
    """Check that the Reaper bridge is alive and report Reaper's version.

    Call this first when anything fails — a clean response confirms both the
    MCP server and the in-Reaper bridge script are running and reachable.

    Returns: `{pong: bool, reaper_version: str}`.
    """
    return _render(_call("ping"), response_format)


@mcp.tool(
    name="reaper_get_project_info",
    annotations={
        "title": "Get Project Info",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_get_project_info(response_format: _FMT = ResponseFormat.MARKDOWN) -> str:
    """Return the current project's name, length, tempo, cursor position, transport state, and track count.

    Returns a dict: `{name, length_sec, tempo_bpm, cursor_sec, playing,
    paused, recording, track_count}`.
    """
    return _render(_call("get_project_info"), response_format)


# ---------------------------------------------------------------------------
# FX discovery
# ---------------------------------------------------------------------------

@mcp.tool(
    name="reaper_list_installed_fx",
    annotations={
        "title": "List Installed FX",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def reaper_list_installed_fx(
    name_filter: Annotated[
        str,
        Field(description="Case-insensitive substring to narrow results (e.g. 'serum', 'reacomp')", max_length=200),
    ] = "",
    instruments_only: Annotated[
        bool, Field(description="If true, return only synths/samplers (instruments)")
    ] = False,
    limit: Annotated[int, Field(description="Maximum results to return", ge=1, le=2000)] = 200,
    offset: Annotated[int, Field(description="Number of results to skip for pagination", ge=0)] = 0,
    response_format: _FMT = ResponseFormat.MARKDOWN,
) -> str:
    """List every FX plugin Reaper has scanned (VST2/VST3/CLAP/JS).

    Reads Reaper's own scan-cache files directly (no bridge round-trip). Use
    this to find the exact `fx_name` string to pass to `reaper_add_fx_to_track`.

    Each item: `name` (pass this to add_fx, e.g. "VST3i: Serum (Xfer Records)"),
    `kind` (VST/VSTi/VST3/VST3i/CLAP/CLAPi/JS), `is_instrument` (bool), `filename`.

    Result is paginated. The response wraps the page in
    `{total, count, offset, has_more, next_offset, items}`.
    """
    items = _discover_installed_fx()
    needle = name_filter.lower().strip()
    if needle:
        items = [it for it in items if needle in it["name"].lower()]
    if instruments_only:
        items = [it for it in items if it["is_instrument"]]
    items.sort(key=lambda it: it["name"].lower())

    total = len(items)
    page = items[offset:offset + limit]
    has_more = offset + len(page) < total
    payload = {
        "total": total,
        "count": len(page),
        "offset": offset,
        "has_more": has_more,
        "next_offset": offset + len(page) if has_more else None,
        "items": page,
    }
    if response_format == ResponseFormat.JSON:
        return json.dumps(payload, indent=2, default=str)
    head = f"**{total}** plugins matched (showing {len(page)} from offset {offset})"
    return head + "\n\n" + _to_markdown(page)


# ---------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------

@mcp.tool(
    name="reaper_list_tracks",
    annotations={
        "title": "List Tracks",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_list_tracks(response_format: _FMT = ResponseFormat.MARKDOWN) -> str:
    """List all tracks with index, name, volume (dB), pan, mute/solo/arm state, and FX count.

    Track `index` is 0-based and is the value every other track tool expects.
    """
    return _render(_call("list_tracks"), response_format)


@mcp.tool(
    name="reaper_get_track_state",
    annotations={
        "title": "Get Track State",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_get_track_state(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    response_format: _FMT = ResponseFormat.MARKDOWN,
) -> str:
    """Return name, volume (dB), pan, mute/solo/arm, and FX count for a single track."""
    return _render(_call("get_track_state", track_index=track_index), response_format)


@mcp.tool(
    name="reaper_create_track",
    annotations={
        "title": "Create Track",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def reaper_create_track(
    name: Annotated[str, Field(description="Name for the new track (empty for unnamed)", max_length=256)] = "",
    position: Annotated[int, Field(description="0-based insert position; -1 appends to the end", ge=-1)] = -1,
) -> dict:
    """Insert a new track. Returns the new track's state including its assigned index."""
    return _call("create_track", name=name, position=position)


@mcp.tool(
    name="reaper_delete_track",
    annotations={
        "title": "Delete Track",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def reaper_delete_track(
    track_index: Annotated[int, Field(description="0-based index of the track to delete", ge=0)],
) -> dict:
    """Delete the track at the given index. This is destructive and shifts later indices down by one."""
    return _call("delete_track", track_index=track_index)


@mcp.tool(
    name="reaper_rename_track",
    annotations={
        "title": "Rename Track",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_rename_track(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    name: Annotated[str, Field(description="New track name", min_length=1, max_length=256)],
) -> dict:
    """Rename a track. Returns the updated track state."""
    return _call("rename_track", track_index=track_index, name=name)


@mcp.tool(
    name="reaper_set_track_volume_db",
    annotations={
        "title": "Set Track Volume",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_set_track_volume_db(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    db: Annotated[float, Field(description="Volume in dB. 0 = unity; pass -150 (or lower) for -inf/silence", ge=-150.0, le=24.0)],
) -> dict:
    """Set a track's volume fader in dB. Returns the updated track state."""
    return _call("set_track_volume_db", track_index=track_index, db=db)


@mcp.tool(
    name="reaper_set_track_pan",
    annotations={
        "title": "Set Track Pan",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_set_track_pan(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    pan: Annotated[float, Field(description="-1.0 = hard left, 0 = center, +1.0 = hard right", ge=-1.0, le=1.0)],
) -> dict:
    """Set a track's pan. Returns the updated track state."""
    return _call("set_track_pan", track_index=track_index, pan=pan)


@mcp.tool(
    name="reaper_set_track_mute",
    annotations={
        "title": "Set Track Mute",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_set_track_mute(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    mute: Annotated[bool, Field(description="True to mute, False to unmute")],
) -> dict:
    """Mute or unmute a track. Returns the updated track state."""
    return _call("set_track_mute", track_index=track_index, mute=mute)


@mcp.tool(
    name="reaper_set_track_solo",
    annotations={
        "title": "Set Track Solo",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_set_track_solo(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    solo: Annotated[bool, Field(description="True to solo, False to un-solo")],
) -> dict:
    """Solo or un-solo a track. Returns the updated track state."""
    return _call("set_track_solo", track_index=track_index, solo=solo)


# ---------------------------------------------------------------------------
# Master track
# ---------------------------------------------------------------------------

@mcp.tool(
    name="reaper_get_master_track",
    annotations={
        "title": "Get Master Track",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_get_master_track(response_format: _FMT = ResponseFormat.MARKDOWN) -> str:
    """Return the master track's volume (dB), pan, mute state, and FX count."""
    return _render(_call("get_master_track"), response_format)


@mcp.tool(
    name="reaper_set_master_volume_db",
    annotations={
        "title": "Set Master Volume",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_set_master_volume_db(
    db: Annotated[float, Field(description="Master volume in dB. 0 = unity", ge=-150.0, le=24.0)],
) -> dict:
    """Set the master track's volume fader in dB."""
    return _call("set_master_volume_db", db=db)


# ---------------------------------------------------------------------------
# FX on a track
# ---------------------------------------------------------------------------

@mcp.tool(
    name="reaper_list_track_fx",
    annotations={
        "title": "List Track FX",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_list_track_fx(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    response_format: _FMT = ResponseFormat.MARKDOWN,
) -> str:
    """List FX on a track with each FX's index, name, current preset, enabled state, and param count.

    FX `index` is 0-based within the track's chain and is what the other FX
    tools expect.
    """
    return _render(_call("list_track_fx", track_index=track_index), response_format)


@mcp.tool(
    name="reaper_add_fx_to_track",
    annotations={
        "title": "Add FX to Track",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def reaper_add_fx_to_track(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    fx_name: Annotated[
        str,
        Field(
            description="Plugin name as returned by reaper_list_installed_fx, e.g. 'VST3i: Serum (Xfer Records)'. A bare name like 'Serum' works if unambiguous.",
            min_length=1,
            max_length=300,
        ),
    ],
    show_ui: Annotated[bool, Field(description="Pop open the plugin's floating window")] = False,
) -> dict:
    """Add an FX to the end of a track's chain.

    Returns `{track_index, fx_index, name}`. If the plugin can't be found the
    call fails — re-check the exact string via `reaper_list_installed_fx`.
    """
    return _call("add_fx_to_track", track_index=track_index, fx_name=fx_name, show_ui=show_ui)


@mcp.tool(
    name="reaper_remove_fx",
    annotations={
        "title": "Remove FX",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def reaper_remove_fx(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    fx_index: Annotated[int, Field(description="0-based FX index within the track chain", ge=0)],
) -> dict:
    """Remove an FX from a track's chain. Indices of later FX shift down by one."""
    return _call("remove_fx", track_index=track_index, fx_index=fx_index)


@mcp.tool(
    name="reaper_set_fx_enabled",
    annotations={
        "title": "Set FX Enabled",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_set_fx_enabled(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    fx_index: Annotated[int, Field(description="0-based FX index within the track chain", ge=0)],
    enabled: Annotated[bool, Field(description="True to enable, False to bypass")],
) -> dict:
    """Enable or bypass an FX (enabled=False bypasses it without removing it)."""
    return _call("set_fx_enabled", track_index=track_index, fx_index=fx_index, enabled=enabled)


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

@mcp.tool(
    name="reaper_list_fx_presets",
    annotations={
        "title": "List FX Presets",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_list_fx_presets(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    fx_index: Annotated[int, Field(description="0-based FX index within the track chain", ge=0)],
    response_format: _FMT = ResponseFormat.MARKDOWN,
) -> str:
    """List every preset available for an FX instance, plus the current selection.

    Returns `{count, current_index, presets:[{index, name}]}`.
    """
    return _render(
        _call("list_fx_presets", track_index=track_index, fx_index=fx_index),
        response_format,
    )


@mcp.tool(
    name="reaper_set_fx_preset",
    annotations={
        "title": "Set FX Preset",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_set_fx_preset(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    fx_index: Annotated[int, Field(description="0-based FX index within the track chain", ge=0)],
    preset_name: Annotated[
        str, Field(description="Preset name to select (use this OR preset_index)", max_length=300)
    ] = "",
    preset_index: Annotated[
        int, Field(description="Preset index to select; -1 means 'use preset_name instead'", ge=-1)
    ] = -1,
) -> dict:
    """Switch an FX to a preset by name or by index.

    Provide exactly one of `preset_name` or `preset_index`. Use
    `reaper_list_fx_presets` to discover valid values.
    """
    params: dict[str, Any] = {"track_index": track_index, "fx_index": fx_index}
    if preset_index >= 0:
        params["preset_index"] = preset_index
    else:
        params["preset_name"] = preset_name
    return _call("set_fx_preset", **params)


# ---------------------------------------------------------------------------
# FX parameters
# ---------------------------------------------------------------------------

@mcp.tool(
    name="reaper_list_fx_params",
    annotations={
        "title": "List FX Params",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_list_fx_params(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    fx_index: Annotated[int, Field(description="0-based FX index within the track chain", ge=0)],
    response_format: _FMT = ResponseFormat.MARKDOWN,
) -> str:
    """List an FX's parameters with current value, min, and max.

    Returns a list of `{index, name, value, min, max}`. Parameter values are
    plugin-native (often normalised 0..1) — read min/max before setting.
    """
    return _render(
        _call("list_fx_params", track_index=track_index, fx_index=fx_index),
        response_format,
    )


@mcp.tool(
    name="reaper_set_fx_param",
    annotations={
        "title": "Set FX Param",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_set_fx_param(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    fx_index: Annotated[int, Field(description="0-based FX index within the track chain", ge=0)],
    param: Annotated[
        str,
        Field(
            description="Parameter index as a string, or a (case-insensitive) parameter name from reaper_list_fx_params",
            min_length=1,
            max_length=200,
        ),
    ],
    value: Annotated[float, Field(description="New value, within the parameter's min..max range")],
) -> dict:
    """Set an FX parameter by index or name. Returns `{param_index, value, min, max}`."""
    return _call("set_fx_param", track_index=track_index, fx_index=fx_index, param=param, value=value)


# ---------------------------------------------------------------------------
# Automation envelopes
# ---------------------------------------------------------------------------

@mcp.tool(
    name="reaper_add_envelope_point",
    annotations={
        "title": "Add Envelope Point",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def reaper_add_envelope_point(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    target: Annotated[EnvelopeTarget, Field(description="Which envelope to write to")],
    time_sec: Annotated[float, Field(description="Point time in seconds from project start", ge=0.0)],
    value: Annotated[float, Field(description="Point value. For volume: linear gain unless value_is_db=True. For pan: -1..1. For fx_param: the parameter's native value")],
    fx_index: Annotated[int, Field(description="Required when target='fx_param': the 0-based FX index", ge=-1)] = -1,
    param: Annotated[str, Field(description="Required when target='fx_param': parameter index or name", max_length=200)] = "",
    shape: Annotated[EnvelopeShape, Field(description="Interpolation shape to the next point")] = EnvelopeShape.LINEAR,
    value_is_db: Annotated[bool, Field(description="For target='volume', interpret value as dB instead of linear gain")] = False,
) -> dict:
    """Insert one automation point on a track volume/pan or FX-parameter envelope.

    Tip: if the envelope can't be obtained, set the track to read mode first
    via `reaper_set_track_automation_mode(track_index, 'read')`.
    """
    params: dict[str, Any] = {
        "track_index": track_index,
        "target": target.value,
        "time_sec": time_sec,
        "value": value,
        "shape": shape.value,
        "value_is_db": value_is_db,
    }
    if target == EnvelopeTarget.FX_PARAM:
        params["fx_index"] = fx_index
        params["param"] = param
    return _call("add_envelope_point", **params)


@mcp.tool(
    name="reaper_clear_envelope",
    annotations={
        "title": "Clear Envelope",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_clear_envelope(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    target: Annotated[EnvelopeTarget, Field(description="Which envelope to clear")],
    fx_index: Annotated[int, Field(description="Required when target='fx_param'", ge=-1)] = -1,
    param: Annotated[str, Field(description="Required when target='fx_param': parameter index or name", max_length=200)] = "",
) -> dict:
    """Delete every point on an envelope. Same target semantics as reaper_add_envelope_point."""
    params: dict[str, Any] = {"track_index": track_index, "target": target.value}
    if target == EnvelopeTarget.FX_PARAM:
        params["fx_index"] = fx_index
        params["param"] = param
    return _call("clear_envelope", **params)


@mcp.tool(
    name="reaper_set_track_automation_mode",
    annotations={
        "title": "Set Track Automation Mode",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_set_track_automation_mode(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    mode: Annotated[AutomationMode, Field(description="Automation mode to apply")],
) -> dict:
    """Set a track's automation mode (trim/read/touch/write/latch/latch_preview).

    Set to 'read' for written envelopes to play back.
    """
    return _call("set_track_automation_mode", track_index=track_index, mode=mode.value)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

@mcp.tool(
    name="reaper_set_track_record_arm",
    annotations={
        "title": "Arm Track for Recording",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_set_track_record_arm(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    armed: Annotated[bool, Field(description="True to arm, False to disarm")],
) -> dict:
    """Arm or disarm a track for recording. Returns the updated track state."""
    return _call("set_track_record_arm", track_index=track_index, armed=armed)


@mcp.tool(
    name="reaper_set_track_record_input",
    annotations={
        "title": "Set Track Record Input",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_set_track_record_input(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    input: Annotated[
        int,
        Field(description="Record input. 0 = audio input 1. Encode MIDI as 4096 + (channel * 32) + device.", ge=0),
    ],
) -> dict:
    """Set a track's record input (audio channel or encoded MIDI input)."""
    return _call("set_track_record_input", track_index=track_index, input=input)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

@mcp.tool(
    name="reaper_transport_play",
    annotations={
        "title": "Play",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_transport_play() -> dict:
    """Start playback from the edit cursor."""
    return _call("transport_play")


@mcp.tool(
    name="reaper_transport_stop",
    annotations={
        "title": "Stop",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_transport_stop(
    force: Annotated[
        bool,
        Field(
            description=(
                "Stop even if Reaper is currently recording. Defaults to false: "
                "if a recording is in progress the call is refused so an in-progress "
                "take isn't ended unintentionally. Always confirm with the user "
                "before passing true."
            ),
        ),
    ] = False,
) -> dict:
    """Stop the transport.

    If Reaper is recording, the call is refused unless ``force=true`` — this
    avoids ending a take without the user's go-ahead. Ask the user first, then
    retry with ``force=true``.
    """
    return _call("transport_stop", force=force)


@mcp.tool(
    name="reaper_transport_record",
    annotations={
        "title": "Record",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def reaper_transport_record() -> dict:
    """Start recording on all armed tracks."""
    return _call("transport_record")


@mcp.tool(
    name="reaper_transport_pause",
    annotations={
        "title": "Pause",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_transport_pause() -> dict:
    """Pause the transport, keeping the cursor where it is."""
    return _call("transport_pause")


@mcp.tool(
    name="reaper_set_cursor",
    annotations={
        "title": "Set Edit Cursor",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_set_cursor(
    time_sec: Annotated[float, Field(description="Cursor position in seconds from project start", ge=0.0)],
    move_view: Annotated[bool, Field(description="Scroll the arrange view to follow the cursor")] = True,
) -> dict:
    """Move the edit cursor to a time in seconds. Returns the resulting cursor position."""
    return _call("set_cursor", time_sec=time_sec, move_view=move_view)


@mcp.tool(
    name="reaper_set_tempo",
    annotations={
        "title": "Set Tempo",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_set_tempo(
    bpm: Annotated[float, Field(description="Project tempo in beats per minute", gt=0.0, le=960.0)],
) -> dict:
    """Set the project tempo in BPM. Returns the resulting tempo."""
    return _call("set_tempo", bpm=bpm)


# ---------------------------------------------------------------------------
# Time selection & loop
# ---------------------------------------------------------------------------

@mcp.tool(
    name="reaper_set_time_selection",
    annotations={
        "title": "Set Time Selection",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_set_time_selection(
    start_sec: Annotated[float, Field(description="Selection start in seconds", ge=0.0)],
    end_sec: Annotated[float, Field(description="Selection end in seconds (must be >= start)", ge=0.0)],
) -> dict:
    """Set the project time selection (the loop/render range) to [start, end] seconds."""
    return _call("set_time_selection", start_sec=start_sec, end_sec=end_sec)


@mcp.tool(
    name="reaper_clear_time_selection",
    annotations={
        "title": "Clear Time Selection",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_clear_time_selection() -> dict:
    """Clear the project time selection."""
    return _call("clear_time_selection")


@mcp.tool(
    name="reaper_set_loop_enabled",
    annotations={
        "title": "Set Loop Enabled",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_set_loop_enabled(
    enabled: Annotated[bool, Field(description="True to enable looped playback, False to disable")],
) -> dict:
    """Enable or disable looped playback (repeat over the time selection)."""
    return _call("set_loop_enabled", enabled=enabled)


# ---------------------------------------------------------------------------
# Markers & regions
# ---------------------------------------------------------------------------

@mcp.tool(
    name="reaper_list_markers",
    annotations={
        "title": "List Markers and Regions",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_list_markers(response_format: _FMT = ResponseFormat.MARKDOWN) -> str:
    """List all project markers and regions.

    Each entry: `{index, is_region, position_sec, region_end_sec, name, id}`.
    `id` is the marker/region's displayed number — pass it to delete/goto tools.

    Known limitation: `name` may come back empty even when a marker has a name.
    REAPER's Python ReaScript build does not marshal the name out-parameter of
    EnumProjectMarkers, so names set via reaper_add_marker/reaper_add_region show
    in Reaper's UI but can't be read back here. Identify markers by position/id.
    """
    return _render(_call("list_markers"), response_format)


@mcp.tool(
    name="reaper_add_marker",
    annotations={
        "title": "Add Marker",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def reaper_add_marker(
    position_sec: Annotated[float, Field(description="Marker time in seconds", ge=0.0)],
    name: Annotated[str, Field(description="Marker label", max_length=256)] = "",
) -> dict:
    """Add a project marker at a time. Returns `{id, is_region, position_sec}`."""
    return _call("add_marker", position_sec=position_sec, name=name, is_region=False)


@mcp.tool(
    name="reaper_add_region",
    annotations={
        "title": "Add Region",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def reaper_add_region(
    start_sec: Annotated[float, Field(description="Region start in seconds", ge=0.0)],
    end_sec: Annotated[float, Field(description="Region end in seconds (must be >= start)", ge=0.0)],
    name: Annotated[str, Field(description="Region label", max_length=256)] = "",
) -> dict:
    """Add a project region spanning [start, end] seconds. Returns `{id, is_region, position_sec}`."""
    return _call("add_marker", position_sec=start_sec, region_end_sec=end_sec, name=name, is_region=True)


@mcp.tool(
    name="reaper_delete_marker",
    annotations={
        "title": "Delete Marker or Region",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_delete_marker(
    marker_id: Annotated[int, Field(description="The marker/region 'id' from reaper_list_markers", ge=0)],
    is_region: Annotated[bool, Field(description="True if deleting a region, False for a marker")] = False,
) -> dict:
    """Delete a project marker or region by its id."""
    return _call("delete_marker", marker_id=marker_id, is_region=is_region)


@mcp.tool(
    name="reaper_goto_marker",
    annotations={
        "title": "Go To Marker",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_goto_marker(
    marker_id: Annotated[int, Field(description="The marker 'id' from reaper_list_markers", ge=0)],
) -> dict:
    """Move the edit cursor to a marker by its id."""
    return _call("goto_marker", marker_id=marker_id)


# ---------------------------------------------------------------------------
# Track sends / routing
# ---------------------------------------------------------------------------

@mcp.tool(
    name="reaper_list_sends",
    annotations={
        "title": "List Track Sends",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_list_sends(
    track_index: Annotated[int, Field(description="0-based index of the source track", ge=0)],
    response_format: _FMT = ResponseFormat.MARKDOWN,
) -> str:
    """List the sends originating from a track.

    Each entry: `{send_index, dest_track_index, dest_track_name, volume_db, pan, muted}`.
    """
    return _render(_call("list_sends", track_index=track_index), response_format)


@mcp.tool(
    name="reaper_add_send",
    annotations={
        "title": "Add Track Send",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def reaper_add_send(
    src_track_index: Annotated[int, Field(description="0-based index of the source track", ge=0)],
    dest_track_index: Annotated[int, Field(description="0-based index of the destination track", ge=0)],
) -> dict:
    """Create a send from one track to another. Returns the new send's index."""
    return _call("add_send", src_track_index=src_track_index, dest_track_index=dest_track_index)


@mcp.tool(
    name="reaper_set_send_volume_db",
    annotations={
        "title": "Set Send Volume",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_set_send_volume_db(
    track_index: Annotated[int, Field(description="0-based index of the source track", ge=0)],
    send_index: Annotated[int, Field(description="0-based send index from reaper_list_sends", ge=0)],
    db: Annotated[float, Field(description="Send level in dB. 0 = unity", ge=-150.0, le=24.0)],
) -> dict:
    """Set the level of a track send in dB."""
    return _call("set_send_volume", track_index=track_index, send_index=send_index, db=db)


@mcp.tool(
    name="reaper_remove_send",
    annotations={
        "title": "Remove Track Send",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def reaper_remove_send(
    track_index: Annotated[int, Field(description="0-based index of the source track", ge=0)],
    send_index: Annotated[int, Field(description="0-based send index from reaper_list_sends", ge=0)],
) -> dict:
    """Remove a send from a track. Later send indices shift down by one."""
    return _call("remove_send", track_index=track_index, send_index=send_index)


# ---------------------------------------------------------------------------
# Media / MIDI items
# ---------------------------------------------------------------------------

@mcp.tool(
    name="reaper_list_items",
    annotations={
        "title": "List Track Items",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_list_items(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    response_format: _FMT = ResponseFormat.MARKDOWN,
) -> str:
    """List the media/MIDI items on a track.

    Each entry: `{index, position_sec, length_sec, muted, take_name}`. Item
    `index` is 0-based within the track and is what the item tools expect.
    """
    return _render(_call("list_items", track_index=track_index), response_format)


@mcp.tool(
    name="reaper_insert_midi_item",
    annotations={
        "title": "Insert MIDI Item",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def reaper_insert_midi_item(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    start_sec: Annotated[float, Field(description="Item start in seconds", ge=0.0)],
    end_sec: Annotated[float, Field(description="Item end in seconds (must be > start)", ge=0.0)],
) -> dict:
    """Create an empty MIDI item on a track spanning [start, end] seconds.

    Returns `{item_index, position_sec, length_sec}`. Add notes with
    `reaper_add_midi_note` (one note) or `reaper_add_midi_notes` (a whole part at once).
    """
    return _call("insert_midi_item", track_index=track_index, start_sec=start_sec, end_sec=end_sec)


@mcp.tool(
    name="reaper_add_midi_note",
    annotations={
        "title": "Add MIDI Note",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def reaper_add_midi_note(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    item_index: Annotated[int, Field(description="0-based item index from reaper_list_items", ge=0)],
    pitch: Annotated[int, Field(description="MIDI note number 0-127 (60 = middle C)", ge=0, le=127)],
    start_sec: Annotated[float, Field(description="Note start in seconds (project time)", ge=0.0)],
    length_sec: Annotated[float, Field(description="Note duration in seconds", gt=0.0)],
    velocity: Annotated[int, Field(description="Note velocity 1-127", ge=1, le=127)] = 96,
    channel: Annotated[int, Field(description="MIDI channel 0-15", ge=0, le=15)] = 0,
) -> dict:
    """Insert a single MIDI note into an existing MIDI item on a track."""
    return _call(
        "add_midi_note",
        track_index=track_index,
        item_index=item_index,
        pitch=pitch,
        start_sec=start_sec,
        length_sec=length_sec,
        velocity=velocity,
        channel=channel,
    )


@mcp.tool(
    name="reaper_add_midi_notes",
    annotations={
        "title": "Add MIDI Notes (batch)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def reaper_add_midi_notes(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    item_index: Annotated[int, Field(description="0-based item index from reaper_list_items", ge=0)],
    notes: Annotated[
        list[dict],
        Field(
            description="List of note objects to insert. Each note is "
                        "{pitch: int 0-127 (60=middle C), start_sec: float >=0 (project time), "
                        "length_sec: float >0, velocity?: int 1-127 (default 96), "
                        "channel?: int 0-15 (default 0)}.",
            min_length=1,
        ),
    ],
) -> dict:
    """Insert many MIDI notes into an existing MIDI item in one call.

    Prefer this over repeated `reaper_add_midi_note` when writing a whole part — it
    inserts every note with sorting deferred and sorts the take once at the end, which
    is far faster and is a single undo step. Returns `{inserted_count, notes}`.
    """
    return _call(
        "add_midi_notes",
        track_index=track_index,
        item_index=item_index,
        notes=notes,
    )


@mcp.tool(
    name="reaper_delete_item",
    annotations={
        "title": "Delete Item",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def reaper_delete_item(
    track_index: Annotated[int, Field(description="0-based track index", ge=0)],
    item_index: Annotated[int, Field(description="0-based item index from reaper_list_items", ge=0)],
) -> dict:
    """Delete a media/MIDI item from a track. Later item indices shift down by one."""
    return _call("delete_item", track_index=track_index, item_index=item_index)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

@mcp.tool(
    name="reaper_render_project",
    annotations={
        "title": "Render Project",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def reaper_render_project() -> dict:
    """Render the project to disk using Reaper's most recent render settings.

    This reuses whatever output path, bounds, and format were last configured
    in Reaper's Render dialog — set those up once manually first. Writes a file
    but does not modify the project.
    """
    return _call("render_project")


# ---------------------------------------------------------------------------
# Mix analysis (server-side DSP + Gemini — bypasses the bridge entirely)
#
# The bridge is stdlib-only and runs inside Reaper; audio DSP needs numpy etc,
# so analysis lives here in the server venv (like reaper_list_installed_fx).
# Precise numbers come from local DSP; Gemini *listens* to the file and grounds
# its feedback in those measurements. All heavy deps are imported lazily so the
# server still imports without the optional `[analyze]` extra installed.
# ---------------------------------------------------------------------------

# (name, low_hz, high_hz) — the bands reported for frequency balance.
_FREQ_BANDS = [
    ("sub", 20, 60),
    ("bass", 60, 120),
    ("low_mid", 120, 500),
    ("mid", 500, 2000),
    ("high_mid", 2000, 6000),
    ("air", 6000, 20000),
]


def _amp_db(x: float) -> float:
    """Amplitude ratio -> dB, floored so silence doesn't blow up to -inf."""
    import math
    return 20.0 * math.log10(x) if x > 1e-12 else -120.0


def _load_audio(path: str):
    """Read an audio file to float64 samples in [-1, 1], shape (frames, channels)."""
    import soundfile as sf
    data, rate = sf.read(path, always_2d=True, dtype="float64")
    return data, int(rate)


def _loudness_metrics(data, rate: float) -> dict:
    """EBU R128 / BS.1770 loudness, plus peak / crest / clipping (local DSP)."""
    import numpy as np
    import pyloudnorm as pyln

    meter = pyln.Meter(rate)
    try:
        integrated = float(meter.integrated_loudness(data))
    except Exception:
        integrated = float("nan")

    # Short-term (3 s window, 1 s hop) for max loudness and a percentile-based LRA.
    win, hop = int(3 * rate), int(1 * rate)
    n = data.shape[0]
    short_term = []
    if win > 0:
        for start in range(0, max(1, n - win + 1), hop):
            block = data[start:start + win]
            try:
                lv = float(meter.integrated_loudness(block))
            except Exception:
                continue
            if np.isfinite(lv) and lv > -70.0:
                short_term.append(lv)
    st_max = max(short_term) if short_term else integrated
    if len(short_term) >= 2:
        lra = float(np.percentile(short_term, 95) - np.percentile(short_term, 10))
    else:
        lra = 0.0

    peak = float(np.max(np.abs(data))) if data.size else 0.0
    rms = float(np.sqrt(np.mean(np.square(data)))) if data.size else 0.0
    clipped = int(np.sum(np.abs(data) >= 0.997))

    return {
        "integrated_lufs": round(integrated, 2) if integrated == integrated else None,
        "short_term_max_lufs": round(st_max, 2) if st_max == st_max else None,
        "loudness_range_lu": round(lra, 2),
        "sample_peak_dbfs": round(_amp_db(peak), 2),
        "rms_dbfs": round(_amp_db(rms), 2),
        "crest_factor_db": round(_amp_db(peak) - _amp_db(rms), 2),
        "clipped_samples": clipped,
    }


def _spectral_balance(data, rate: float) -> dict:
    """Welch-averaged power per named band, as % of total and dB below total."""
    import numpy as np

    mono = data.mean(axis=1)
    n = mono.shape[0]
    seg = 8192
    if n < seg:
        seg = 1 << max(8, int(np.floor(np.log2(max(n, 2)))))
        seg = min(seg, n)
    win = np.hanning(seg)
    step = max(seg // 2, 1)

    psd = np.zeros(seg // 2 + 1)
    count = 0
    for start in range(0, n - seg + 1, step):
        spec = np.fft.rfft(mono[start:start + seg] * win)
        psd += np.abs(spec) ** 2
        count += 1
    if count == 0:  # signal shorter than one segment
        spec = np.fft.rfft(mono[:seg] * win[:mono[:seg].shape[0]])
        psd = np.abs(spec) ** 2
        count = 1
    psd /= count

    freqs = np.fft.rfftfreq(seg, 1.0 / rate)
    total = float(psd.sum()) + 1e-20
    bands = {}
    for name, lo, hi in _FREQ_BANDS:
        p = float(psd[(freqs >= lo) & (freqs < hi)].sum())
        bands[name] = {
            "pct": round(100.0 * p / total, 1),
            "db": round(10.0 * np.log10(p / total + 1e-20), 1),
        }
    return bands


def _stereo_metrics(data) -> dict:
    """Correlation, mid/side width and L/R balance — mono compatibility cues."""
    import numpy as np

    if data.shape[1] < 2:
        return {"channels": 1, "note": "mono file — no stereo metrics"}

    left, right = data[:, 0], data[:, 1]
    if np.std(left) < 1e-9 or np.std(right) < 1e-9:
        corr = 1.0
    else:
        corr = float(np.corrcoef(left, right)[0, 1])

    mid, side = (left + right) / 2.0, (left - right) / 2.0
    mid_rms = float(np.sqrt(np.mean(mid ** 2)))
    side_rms = float(np.sqrt(np.mean(side ** 2)))
    l_rms = float(np.sqrt(np.mean(left ** 2)))
    r_rms = float(np.sqrt(np.mean(right ** 2)))

    return {
        "channels": 2,
        "correlation": round(corr, 3),
        "width_side_minus_mid_db": round(_amp_db(side_rms) - _amp_db(mid_rms), 1),
        "balance_l_minus_r_db": round(_amp_db(l_rms) - _amp_db(r_rms), 2),
    }


def _analyze_audio_file(path: str) -> dict:
    """Full local-DSP metric set for one audio file."""
    data, rate = _load_audio(path)
    return {
        "file": os.path.basename(path),
        "sample_rate": rate,
        "channels": int(data.shape[1]),
        "duration_sec": round(data.shape[0] / rate, 2) if rate else None,
        "loudness": _loudness_metrics(data, rate),
        "frequency_balance": _spectral_balance(data, rate),
        "stereo": _stereo_metrics(data),
    }


def _make_upload_proxy(src_path: str, max_rate: int = 24000) -> str:
    """Transcode to a small mono OGG/Vorbis for upload; fall back to the original.

    DSP runs on the full-quality file; Gemini only needs to *hear* the mix, so the
    upload is shrunk hard (mono, <=24 kHz, Vorbis VBR) — a few-minute song lands
    around 1 MB instead of tens of MB of WAV. On any failure the original path is
    returned so the analysis still proceeds (just a bigger upload).
    """
    import os
    import tempfile
    import soundfile as sf
    try:
        data, rate = _load_audio(src_path)
        mono = data.mean(axis=1)
        target = min(int(rate), max_rate)
        if target < int(rate):
            from math import gcd
            from scipy.signal import resample_poly
            g = gcd(target, int(rate))
            mono = resample_poly(mono, target // g, int(rate) // g)
            rate = target
        stem = os.path.splitext(os.path.basename(src_path))[0]
        out = os.path.join(tempfile.gettempdir(), stem + ".proxy.ogg")
        sf.write(out, mono.astype("float32"), int(rate), format="OGG", subtype="VORBIS")
        return out
    except Exception:
        return src_path


def _upload_gemini_file(client, path: str):
    """Upload audio to the Gemini Files API and wait until it's ACTIVE."""
    import time
    f = client.files.upload(file=path)
    for _ in range(60):
        state = getattr(getattr(f, "state", None), "name", str(getattr(f, "state", "")))
        if state == "ACTIVE":
            return f
        if state == "FAILED":
            raise RuntimeError(f"Gemini failed to process uploaded file '{path}'")
        time.sleep(1)
        f = client.files.get(name=f.name)
    return f


def _gemini_mix_feedback(
    metrics: dict,
    audio_path: str,
    ref_metrics: dict | None,
    ref_path: str | None,
    focus: str,
    model: GeminiModel,
) -> str:
    """Send the audio + measured metrics to Gemini and return its written critique."""
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Export your Google AI Studio key, e.g. "
            "$env:GEMINI_API_KEY='...' before starting the MCP server, or call with "
            "include_ai=false to get the measured metrics only."
        )
    client = genai.Client(api_key=api_key)

    extra = f"\n\nThe user specifically wants you to focus on: {focus}" if focus.strip() else ""
    prompt = (
        "You are a senior mixing and mastering engineer reviewing a track for an artist who "
        "works in REAPER. You are given (1) precise measurements computed by DSP and (2) the "
        "actual audio to listen to. TRUST THE MEASURED NUMBERS for loudness, peaks and band "
        "balance — do not invent your own figures — but use your ears for everything the "
        "numbers can't capture (tonal character, masking, transients, depth, musicality).\n\n"
        f"MEASURED METRICS (mix):\n```json\n{json.dumps(metrics, indent=2)}\n```\n"
    )
    if ref_metrics is not None:
        prompt += (
            f"\nMEASURED METRICS (reference track to compare against):\n```json\n"
            f"{json.dumps(ref_metrics, indent=2)}\n```\n"
        )
    prompt += (
        "\nWrite concise, actionable feedback in markdown with these sections:\n"
        "1. **Overall** — one-paragraph verdict and how close it is to release-ready.\n"
        "2. **Loudness & dynamics** — interpret LUFS / true-ish peak / crest / LRA; is it "
        "competitive and safe for streaming (~-14 LUFS) without crushing dynamics?\n"
        "3. **Frequency balance** — call out muddy / thin / harsh / dull bands using the "
        "measured distribution + what you hear.\n"
        "4. **Stereo & width** — mono compatibility (correlation), width, L/R balance.\n"
        "5. **Fixes in REAPER** — concrete moves with stock plugins (ReaEQ, ReaComp, ReaXcomp, "
        "JS stereo/width tools, ReaLimit) including rough frequencies / amounts.\n"
        "Be specific and brief; prefer bullet points over prose."
        + extra
    )

    proxies: list[str] = []
    main_proxy = _make_upload_proxy(audio_path)
    proxies.append(main_proxy)
    contents: list = [prompt, _upload_gemini_file(client, main_proxy)]
    if ref_path:
        ref_proxy = _make_upload_proxy(ref_path)
        proxies.append(ref_proxy)
        contents.append("Reference track audio follows:")
        contents.append(_upload_gemini_file(client, ref_proxy))

    try:
        resp = client.models.generate_content(model=model.value, contents=contents)
        return resp.text or "_(Gemini returned no text)_"
    finally:
        for pth in proxies:  # only delete proxies we created, never the source
            if pth.endswith(".proxy.ogg"):
                try:
                    os.remove(pth)
                except OSError:
                    pass


def _format_metrics_md(m: dict) -> str:
    """Render one file's metric dict as compact markdown."""
    lines = [
        f"**{m['file']}** — {m['duration_sec']}s, {m['channels']}ch @ {m['sample_rate']} Hz",
        "",
        "_Loudness / mastering_",
        _to_markdown(m["loudness"]),
        "",
        "_Frequency balance (% of energy / dB below total)_",
        _to_markdown([{"band": k, **v} for k, v in m["frequency_balance"].items()]),
        "",
        "_Stereo / width_",
        _to_markdown(m["stereo"]),
    ]
    return "\n".join(lines)


def _run_mix_analysis(
    audio_path: str,
    focus: str,
    reference_path: str,
    include_ai: bool,
    model: GeminiModel,
    response_format: ResponseFormat,
) -> str:
    """Shared core: measure one (or two) files, optionally ask Gemini, format output.

    Used by both reaper_analyze_mix (caller-supplied file) and reaper_analyze_project
    (freshly rendered file). The caller guarantees audio_path exists.
    """
    if reference_path and not os.path.isfile(reference_path):
        raise RuntimeError(f"Reference file not found: {reference_path}")

    try:
        metrics = _analyze_audio_file(audio_path)
        ref_metrics = _analyze_audio_file(reference_path) if reference_path else None
    except ModuleNotFoundError as e:
        raise RuntimeError(
            f"Missing analysis dependency ({e.name}). Install with: pip install -e .[analyze]"
        ) from e

    ai_feedback = None
    if include_ai:
        ai_feedback = _gemini_mix_feedback(
            metrics, audio_path, ref_metrics, reference_path or None, focus, model,
        )

    if response_format == ResponseFormat.JSON:
        return json.dumps(
            {"metrics": metrics, "reference_metrics": ref_metrics, "ai_feedback": ai_feedback},
            indent=2, default=str,
        )

    out = ["## Measured metrics", "", _format_metrics_md(metrics)]
    if ref_metrics is not None:
        out += ["", "### Reference", "", _format_metrics_md(ref_metrics)]
    if ai_feedback is not None:
        out += ["", "## AI mix feedback (Gemini)", "", ai_feedback]
    return "\n".join(out)


@mcp.tool(
    name="reaper_analyze_mix",
    annotations={
        "title": "Analyze Mix",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_analyze_mix(
    audio_path: Annotated[
        str,
        Field(description="Path to the rendered audio file to analyze (WAV/MP3/FLAC/AAC). "
                          "Render the project first (reaper_render_project) and pass that file."),
    ],
    focus: Annotated[
        str,
        Field(description="Optional free-text note on what to focus on, passed to Gemini "
                          "(e.g. 'the vocal sounds buried', 'too boomy on small speakers').",
              max_length=500),
    ] = "",
    reference_path: Annotated[
        str,
        Field(description="Optional path to a reference/commercial track to compare against."),
    ] = "",
    include_ai: Annotated[
        bool,
        Field(description="If true, send the audio + metrics to Gemini for written feedback. "
                          "If false, return only the measured DSP metrics (no API call)."),
    ] = True,
    model: Annotated[
        GeminiModel,
        Field(description="Gemini model that listens to the mix: 'gemini-2.5-flash' (fast/cheap) "
                          "or 'gemini-2.5-pro' (deeper analysis)."),
    ] = GeminiModel.FLASH,
    response_format: _FMT = ResponseFormat.MARKDOWN,
) -> str:
    """Analyze a rendered mix: local DSP measurements + AI listening feedback.

    Two layers. Local DSP (numpy/pyloudnorm) measures the trustworthy numbers —
    integrated LUFS, loudness range, sample peak, crest factor, clipped samples,
    per-band frequency balance, and stereo correlation/width/balance. Then (unless
    include_ai=false) Gemini is given BOTH the audio file and those measurements and
    returns grounded mix/master feedback with concrete REAPER fixes.

    Requires the optional deps: `pip install -e .[analyze]`, and GEMINI_API_KEY in the
    environment for the AI layer. Pass a reference_path to compare against a pro track.

    This reads files and calls an external API; it never modifies the Reaper project.
    """
    if not os.path.isfile(audio_path):
        raise RuntimeError(f"Audio file not found: {audio_path}")
    return _run_mix_analysis(
        audio_path, focus, reference_path, include_ai, model, response_format,
    )


class RenderBounds(str, Enum):
    """What span reaper_analyze_project renders before analyzing."""
    PROJECT = "project"
    TIME_SELECTION = "time_selection"


@mcp.tool(
    name="reaper_analyze_project",
    annotations={
        "title": "Analyze Project (render + analyze)",
        "readOnlyHint": False,  # writes a temp render file to disk
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def reaper_analyze_project(
    focus: Annotated[
        str,
        Field(description="Optional free-text note on what to focus on, passed to Gemini "
                          "(e.g. 'the vocal sounds buried', 'too boomy on small speakers').",
              max_length=500),
    ] = "",
    reference_path: Annotated[
        str,
        Field(description="Optional path to a reference/commercial track to compare against."),
    ] = "",
    bounds: Annotated[
        RenderBounds,
        Field(description="Render the whole 'project' or just the current 'time_selection'."),
    ] = RenderBounds.PROJECT,
    include_ai: Annotated[
        bool,
        Field(description="If true, send a small audio proxy + metrics to Gemini for written "
                          "feedback. If false, return only the measured DSP metrics (no API call)."),
    ] = True,
    model: Annotated[
        GeminiModel,
        Field(description="Gemini model that listens to the mix: 'gemini-2.5-flash' (fast/cheap) "
                          "or 'gemini-2.5-pro' (deeper analysis)."),
    ] = GeminiModel.FLASH,
    response_format: _FMT = ResponseFormat.MARKDOWN,
) -> str:
    """One-call mix check: quick-export the master mix, then analyze it.

    This is the convenient path — no need to pre-configure the render dialog or pass
    a file. It tells Reaper to render the master mix to a temp file (reusing your
    project's render codec, e.g. WAV), measures it with local DSP (LUFS, peak, crest,
    per-band balance, stereo width), then — unless include_ai=false — uploads a small
    compressed proxy plus those measurements to Gemini for grounded feedback.

    Requires the optional deps (`pip install -e .[analyze]`) and GEMINI_API_KEY for the
    AI layer. For best results set the project's render source to 'Master mix' and format
    to a single audio file. Writes a temp render but does not modify the project.
    """
    bounds_flag = 1 if bounds == RenderBounds.PROJECT else 2
    import tempfile
    result = _call(
        "render_mixdown",
        out_dir=tempfile.gettempdir(),
        token="reaper_mcp_mixdown",
        bounds_flag=bounds_flag,
    )
    audio_path = result.get("file") if isinstance(result, dict) else None
    if not audio_path or not os.path.isfile(audio_path):
        raise RuntimeError(f"Render did not produce a readable file (got: {result!r})")
    return _run_mix_analysis(
        audio_path, focus, reference_path, include_ai, model, response_format,
    )


# ---------------------------------------------------------------------------
# Escape hatch
# ---------------------------------------------------------------------------

@mcp.tool(
    name="reaper_run_action",
    annotations={
        "title": "Run Reaper Action",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def reaper_run_action(
    command_id: Annotated[int, Field(description="Numeric Reaper action command ID (from the Actions list)", ge=1)],
) -> dict:
    """Trigger any Reaper action by numeric command ID (Main_OnCommand).

    The escape hatch for actions not yet wrapped as dedicated tools. Look up
    IDs in Reaper's Actions list (right-click an action → Copy selected action
    command ID). Marked destructive because arbitrary actions can do anything.
    """
    return _call("run_action", command_id=command_id)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
