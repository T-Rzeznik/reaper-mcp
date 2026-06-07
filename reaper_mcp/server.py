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
def reaper_transport_stop() -> dict:
    """Stop the transport."""
    return _call("transport_stop")


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
    `reaper_add_midi_note`.
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
