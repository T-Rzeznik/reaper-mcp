"""
Reaper MCP bridge — Python ReaScript that runs inside Reaper.

Drop in %APPDATA%\\REAPER\\Scripts\\ and launch from Reaper's action list
("New action..." -> "Load ReaScript..."  or just drag-drop the .py file
onto Reaper's window).

While it's running it serves newline-delimited JSON commands over TCP at
127.0.0.1:8765 (override with REAPER_MCP_PORT env var before launching Reaper).

Stdlib only — Reaper's embedded Python cannot install packages.
The Reaper Python ReaScript API is raw SWIG: every function is prefixed
with `RPR_`, and functions that return strings via out-buffers must be
called with an empty placeholder string + a buffer size, then unpacked
from the returned tuple.
"""

import json
import math
import os
import socket
import traceback

from reaper_python import *  # noqa: F401,F403 — pulls in RPR_* names


HOST = "127.0.0.1"
PORT = int(os.environ.get("REAPER_MCP_PORT", "8765"))

_server = None
_clients = {}  # client socket -> read buffer (bytes)


# ---------------------------------------------------------------------------
# value conversions
# ---------------------------------------------------------------------------

def db_to_linear(db):
    if db <= -150.0:
        return 0.0
    return 10.0 ** (db / 20.0)


def linear_to_db(lin):
    if lin <= 0.0:
        return -150.0
    return 20.0 * math.log10(lin)


# ---------------------------------------------------------------------------
# Reaper API helpers — wrap the buffer/tuple unpacking pattern.
# Each RPR_* string-out function returns (retval, *original_args_with_buf_modified).
# ---------------------------------------------------------------------------

def _track_name(track):
    return RPR_GetTrackName(track, "", 256)[2]


def _set_track_name(track, name):
    RPR_GetSetMediaTrackInfo_String(track, "P_NAME", name, True)


def _fx_name(track, fx):
    return RPR_TrackFX_GetFXName(track, fx, "", 256)[3]


def _fx_preset(track, fx):
    return RPR_TrackFX_GetPreset(track, fx, "", 256)[3]


def _fx_param_name(track, fx, param):
    return RPR_TrackFX_GetParamName(track, fx, param, "", 256)[4]


def _project_name(proj=0):
    return RPR_GetProjectName(proj, "", 256)[1]


def _is_null_ptr(p):
    if p is None or p == 0:
        return True
    if isinstance(p, str) and "0x0000000000000000" in p:
        return True
    return False


def _track_at(index):
    tr = RPR_GetTrack(0, int(index))
    if _is_null_ptr(tr):
        raise ValueError(f"no track at index {index}")
    return tr


def _track_summary(idx):
    tr = _track_at(idx)
    return {
        "index": idx,
        "name": _track_name(tr),
        "volume_db": linear_to_db(RPR_GetMediaTrackInfo_Value(tr, "D_VOL")),
        "pan": RPR_GetMediaTrackInfo_Value(tr, "D_PAN"),
        "muted": bool(RPR_GetMediaTrackInfo_Value(tr, "B_MUTE")),
        "solo": int(RPR_GetMediaTrackInfo_Value(tr, "I_SOLO")),
        "rec_arm": bool(RPR_GetMediaTrackInfo_Value(tr, "I_RECARM")),
        "fx_count": int(RPR_TrackFX_GetCount(tr)),
    }


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------

BRIDGE_VERSION = 5


def h_ping(_p):
    return {"pong": True, "reaper_version": RPR_GetAppVersion(), "bridge_version": BRIDGE_VERSION}


def h_get_project_info(_p):
    play_state = int(RPR_GetPlayState())
    return {
        "name": _project_name(0),
        "length_sec": float(RPR_GetProjectLength(0)),
        "tempo_bpm": float(RPR_Master_GetTempo()),
        "cursor_sec": float(RPR_GetCursorPosition()),
        "playing": bool(play_state & 1),
        "paused": bool(play_state & 2),
        "recording": bool(play_state & 4),
        "track_count": int(RPR_CountTracks(0)),
    }


def h_list_tracks(_p):
    n = int(RPR_CountTracks(0))
    return [_track_summary(i) for i in range(n)]


def h_create_track(p):
    pos = int(p.get("position", -1))
    if pos < 0:
        pos = int(RPR_CountTracks(0))
    RPR_InsertTrackAtIndex(pos, True)
    name = p.get("name")
    if name:
        tr = _track_at(pos)
        _set_track_name(tr, name)
    RPR_TrackList_AdjustWindows(False)
    RPR_UpdateArrange()
    return _track_summary(pos)


def h_delete_track(p):
    tr = _track_at(p["track_index"])
    RPR_DeleteTrack(tr)
    RPR_UpdateArrange()
    return {"deleted": True}


def h_get_track_state(p):
    return _track_summary(p["track_index"])


def h_set_track_volume_db(p):
    tr = _track_at(p["track_index"])
    RPR_SetMediaTrackInfo_Value(tr, "D_VOL", db_to_linear(float(p["db"])))
    return _track_summary(p["track_index"])


def h_set_track_pan(p):
    tr = _track_at(p["track_index"])
    val = max(-1.0, min(1.0, float(p["pan"])))
    RPR_SetMediaTrackInfo_Value(tr, "D_PAN", val)
    return _track_summary(p["track_index"])


def h_set_track_mute(p):
    tr = _track_at(p["track_index"])
    RPR_SetMediaTrackInfo_Value(tr, "B_MUTE", 1.0 if p["mute"] else 0.0)
    return _track_summary(p["track_index"])


def h_set_track_solo(p):
    tr = _track_at(p["track_index"])
    RPR_SetMediaTrackInfo_Value(tr, "I_SOLO", 2.0 if p["solo"] else 0.0)
    return _track_summary(p["track_index"])


def h_rename_track(p):
    tr = _track_at(p["track_index"])
    _set_track_name(tr, p["name"])
    return _track_summary(p["track_index"])


# ---- FX

def h_list_track_fx(p):
    tr = _track_at(p["track_index"])
    n = int(RPR_TrackFX_GetCount(tr))
    out = []
    for i in range(n):
        out.append({
            "index": i,
            "name": _fx_name(tr, i),
            "preset": _fx_preset(tr, i),
            "enabled": bool(RPR_TrackFX_GetEnabled(tr, i)),
            "param_count": int(RPR_TrackFX_GetNumParams(tr, i)),
        })
    return out


def h_add_fx_to_track(p):
    tr = _track_at(p["track_index"])
    fx_name = p["fx_name"]
    # -1000 = always create new instance at end of chain
    idx = int(RPR_TrackFX_AddByName(tr, fx_name, False, -1000))
    if idx < 0:
        raise ValueError(f"could not add FX '{fx_name}' (not found?)")
    if p.get("show_ui"):
        RPR_TrackFX_Show(tr, idx, 3)  # 3 = floating window
    return {"track_index": p["track_index"], "fx_index": idx, "name": _fx_name(tr, idx)}


def h_remove_fx(p):
    tr = _track_at(p["track_index"])
    ok = RPR_TrackFX_Delete(tr, int(p["fx_index"]))
    if not ok:
        raise ValueError("TrackFX_Delete failed")
    return {"removed": True}


def h_set_fx_enabled(p):
    tr = _track_at(p["track_index"])
    RPR_TrackFX_SetEnabled(tr, int(p["fx_index"]), bool(p["enabled"]))
    return {"enabled": bool(p["enabled"])}


def h_list_fx_presets(p):
    tr = _track_at(p["track_index"])
    fx = int(p["fx_index"])
    # TrackFX_GetPresetIndex(track, fx, &numPresetsOut) -> (current, track, fx, count)
    cur, _, _, count = RPR_TrackFX_GetPresetIndex(tr, fx, 0)
    names = []
    for i in range(int(count)):
        RPR_TrackFX_SetPresetByIndex(tr, fx, i)
        names.append({"index": i, "name": _fx_preset(tr, fx)})
    if cur >= 0:
        RPR_TrackFX_SetPresetByIndex(tr, fx, int(cur))
    return {"count": int(count), "current_index": int(cur), "presets": names}


def h_set_fx_preset(p):
    tr = _track_at(p["track_index"])
    fx = int(p["fx_index"])
    if "preset_index" in p:
        ok = RPR_TrackFX_SetPresetByIndex(tr, fx, int(p["preset_index"]))
    else:
        ok = RPR_TrackFX_SetPreset(tr, fx, p["preset_name"])
    if not ok:
        raise ValueError("preset not found / could not set")
    return {"preset": _fx_preset(tr, fx)}


def _resolve_param_index(tr, fx_index, param):
    if isinstance(param, int) or (isinstance(param, str) and param.lstrip("-").isdigit()):
        return int(param)
    needle = str(param).lower()
    n = int(RPR_TrackFX_GetNumParams(tr, fx_index))
    for i in range(n):
        if _fx_param_name(tr, fx_index, i).lower() == needle:
            return i
    for i in range(n):
        if needle in _fx_param_name(tr, fx_index, i).lower():
            return i
    raise ValueError(f"no FX param matching {param!r}")


def h_list_fx_params(p):
    tr = _track_at(p["track_index"])
    fx = int(p["fx_index"])
    n = int(RPR_TrackFX_GetNumParams(tr, fx))
    out = []
    for i in range(n):
        # TrackFX_GetParam(t, fx, p, &min, &max) -> (val, t, fx, p, min, max)
        val, _, _, _, mn, mx = RPR_TrackFX_GetParam(tr, fx, i, 0, 0)
        out.append({"index": i, "name": _fx_param_name(tr, fx, i), "value": val, "min": mn, "max": mx})
    return out


def h_set_fx_param(p):
    tr = _track_at(p["track_index"])
    fx = int(p["fx_index"])
    pi = _resolve_param_index(tr, fx, p["param"])
    RPR_TrackFX_SetParam(tr, fx, pi, float(p["value"]))
    val, _, _, _, mn, mx = RPR_TrackFX_GetParam(tr, fx, pi, 0, 0)
    return {"param_index": pi, "value": val, "min": mn, "max": mx}


# ---- automation

_SHAPE = {"linear": 0, "square": 1, "slow": 2, "fast_start": 3, "fast_end": 4, "bezier": 5}


def _envelope_for(p):
    tr = _track_at(p["track_index"])
    target = p["target"]
    if target == "volume":
        env = RPR_GetTrackEnvelopeByName(tr, "Volume")
    elif target == "pan":
        env = RPR_GetTrackEnvelopeByName(tr, "Pan")
    elif target == "fx_param":
        fx = int(p["fx_index"])
        pi = _resolve_param_index(tr, fx, p["param"])
        env = RPR_GetFXEnvelope(tr, fx, pi, True)
    else:
        raise ValueError(f"unknown envelope target {target!r}")
    if _is_null_ptr(env):
        raise ValueError(
            f"could not obtain {target} envelope. "
            f"Try set_track_automation_mode(...,'read') first, or for volume/pan "
            f"toggle the envelope visible in Reaper."
        )
    return env


def h_add_envelope_point(p):
    env = _envelope_for(p)
    shape = _SHAPE.get(str(p.get("shape", "linear")).lower(), 0)
    value = float(p["value"])
    if p.get("target") == "volume" and p.get("value_is_db"):
        value = db_to_linear(value)
    RPR_InsertEnvelopePoint(env, float(p["time_sec"]), value, shape, 0.0, False, False)
    RPR_Envelope_SortPoints(env)
    RPR_UpdateArrange()
    return {"inserted": True}


def h_clear_envelope(p):
    env = _envelope_for(p)
    RPR_DeleteEnvelopePointRange(env, -1e18, 1e18)
    RPR_UpdateArrange()
    return {"cleared": True}


def h_set_track_automation_mode(p):
    tr = _track_at(p["track_index"])
    modes = {"trim": 0, "read": 1, "touch": 2, "write": 3, "latch": 4, "latch_preview": 5}
    m = p["mode"]
    if isinstance(m, str):
        m = modes[m.lower()]
    RPR_SetTrackAutomationMode(tr, int(m))
    return {"mode": int(m)}


# ---- recording / transport

def h_set_track_record_arm(p):
    tr = _track_at(p["track_index"])
    RPR_SetMediaTrackInfo_Value(tr, "I_RECARM", 1.0 if p["armed"] else 0.0)
    return _track_summary(p["track_index"])


def h_set_track_record_input(p):
    tr = _track_at(p["track_index"])
    RPR_SetMediaTrackInfo_Value(tr, "I_RECINPUT", float(p["input"]))
    return _track_summary(p["track_index"])


def h_transport_play(_p):
    RPR_OnPlayButton()
    return {"playing": True}


def h_transport_stop(p):
    # GetPlayState bitfield: &1 playing, &2 paused, &4 recording.
    if (int(RPR_GetPlayState()) & 4) and not p.get("force", False):
        raise RuntimeError(
            "Reaper is currently recording. Refusing to stop the transport so an "
            "in-progress take isn't ended unintentionally. Confirm with the user "
            "that it's OK to stop, then call again with force=true."
        )
    RPR_OnStopButton()
    return {"stopped": True}


def h_transport_record(_p):
    RPR_Main_OnCommand(1013, 0)
    return {"recording": True}


def h_transport_pause(_p):
    RPR_OnPauseButton()
    return {"paused": True}


def h_set_cursor(p):
    RPR_SetEditCurPos(float(p["time_sec"]), bool(p.get("move_view", True)), bool(p.get("seek_play", False)))
    return {"cursor_sec": float(RPR_GetCursorPosition())}


def h_set_tempo(p):
    RPR_SetCurrentBPM(0, float(p["bpm"]), True)
    return {"tempo_bpm": float(RPR_Master_GetTempo())}


def h_run_action(p):
    RPR_Main_OnCommand(int(p["command_id"]), 0)
    return {"ran": int(p["command_id"])}


# ---- master track

def h_get_master_track(_p):
    m = RPR_GetMasterTrack(0)
    return {
        "volume_db": linear_to_db(RPR_GetMediaTrackInfo_Value(m, "D_VOL")),
        "pan": RPR_GetMediaTrackInfo_Value(m, "D_PAN"),
        "muted": bool(RPR_GetMediaTrackInfo_Value(m, "B_MUTE")),
        "fx_count": int(RPR_TrackFX_GetCount(m)),
    }


def h_set_master_volume(p):
    m = RPR_GetMasterTrack(0)
    RPR_SetMediaTrackInfo_Value(m, "D_VOL", db_to_linear(float(p["db"])))
    return {"volume_db": float(p["db"])}


# ---- time selection & loop

def h_set_time_selection(p):
    start = float(p["start_sec"])
    end = float(p["end_sec"])
    # GetSet_LoopTimeRange(isSet, isLoop, start, end, allowautoseek)
    RPR_GetSet_LoopTimeRange(True, False, start, end, False)
    RPR_UpdateArrange()
    return {"start_sec": start, "end_sec": end}


def h_clear_time_selection(_p):
    RPR_GetSet_LoopTimeRange(True, False, 0.0, 0.0, False)
    RPR_UpdateArrange()
    return {"cleared": True}


def h_set_loop_enabled(p):
    RPR_GetSetRepeat(1 if p["enabled"] else 0)
    return {"loop": bool(p["enabled"])}


# ---- markers & regions

def h_list_markers(p):
    out = []
    i = 0
    while True:
        # EnumProjectMarkers2(proj, idx, isrgn, pos, rgnend, name, markrgnindexnumber)
        ret = RPR_EnumProjectMarkers2(0, i, 0, 0.0, 0.0, "", 0)
        retval = ret[0]
        if not retval:
            break
        _, _, _, isrgn, pos, rgnend, name, num = ret
        out.append({
            "index": i,
            "is_region": bool(isrgn),
            "position_sec": float(pos),
            "region_end_sec": float(rgnend) if isrgn else None,
            "name": name,
            "id": int(num),
        })
        i += 1
    return out


def h_add_marker(p):
    pos = float(p["position_sec"])
    is_region = bool(p.get("is_region", False))
    rgnend = float(p.get("region_end_sec", pos))
    name = p.get("name", "") or ""
    # AddProjectMarker2(proj, isrgn, pos, rgnend, name, wantidx, color); wantidx=-1 auto-assigns.
    # The name arg doesn't persist through the Python binding, so set it explicitly
    # afterwards with SetProjectMarker3 (by the returned id number).
    num = RPR_AddProjectMarker2(0, is_region, pos, rgnend, name, -1, 0)
    if name:
        RPR_SetProjectMarker3(0, int(num), is_region, pos, rgnend, name, 0)
    RPR_UpdateArrange()
    return {"id": int(num), "is_region": is_region, "position_sec": pos, "name": name}


def h_delete_marker(p):
    is_region = bool(p.get("is_region", False))
    ok = RPR_DeleteProjectMarker(0, int(p["marker_id"]), is_region)
    if not ok:
        raise ValueError(f"no {'region' if is_region else 'marker'} with id {p['marker_id']}")
    RPR_UpdateArrange()
    return {"deleted": True}


def h_goto_marker(p):
    RPR_GoToMarker(0, int(p["marker_id"]), True)
    return {"cursor_sec": float(RPR_GetCursorPosition())}


# ---- track sends / routing

def _ptr_to_int(p):
    """Normalise a ReaScript pointer to an int address.

    Track handles come back as '(MediaTrack*)0x...' strings, but
    GetTrackSendInfo_Value(...,'P_DESTTRACK') returns the same address as a
    float. Reduce both to ints so they can be compared.
    """
    if isinstance(p, (int, float)):
        return int(p)
    if isinstance(p, str):
        idx = p.find("0x")
        if idx >= 0:
            hexchars = ""
            for ch in p[idx + 2:]:
                if ch in "0123456789abcdefABCDEF":
                    hexchars += ch
                else:
                    break
            if hexchars:
                return int(hexchars, 16)
    return None


def _resolve_track_index(ptr):
    """Map a MediaTrack pointer (string or float form) to its 0-based index."""
    target = _ptr_to_int(ptr)
    if target is None:
        return None
    n = int(RPR_CountTracks(0))
    for j in range(n):
        if _ptr_to_int(RPR_GetTrack(0, j)) == target:
            return j
    return None


def h_list_sends(p):
    tr = _track_at(p["track_index"])
    n = int(RPR_GetTrackNumSends(tr, 0))  # category 0 = track sends
    out = []
    for i in range(n):
        dest = RPR_GetTrackSendInfo_Value(tr, 0, i, "P_DESTTRACK")
        dest_idx = _resolve_track_index(dest)
        dest_name = _track_name(RPR_GetTrack(0, dest_idx)) if dest_idx is not None else ""
        out.append({
            "send_index": i,
            "dest_track_index": dest_idx,
            "dest_track_name": dest_name,
            "volume_db": linear_to_db(RPR_GetTrackSendInfo_Value(tr, 0, i, "D_VOL")),
            "pan": RPR_GetTrackSendInfo_Value(tr, 0, i, "D_PAN"),
            "muted": bool(RPR_GetTrackSendInfo_Value(tr, 0, i, "B_MUTE")),
        })
    return out


def h_add_send(p):
    src = _track_at(p["src_track_index"])
    dst = _track_at(p["dest_track_index"])
    idx = int(RPR_CreateTrackSend(src, dst))
    if idx < 0:
        raise ValueError("CreateTrackSend failed")
    return {
        "send_index": idx,
        "src_track_index": int(p["src_track_index"]),
        "dest_track_index": int(p["dest_track_index"]),
    }


def h_set_send_volume(p):
    tr = _track_at(p["track_index"])
    RPR_SetTrackSendInfo_Value(tr, 0, int(p["send_index"]), "D_VOL", db_to_linear(float(p["db"])))
    return {"send_index": int(p["send_index"]), "volume_db": float(p["db"])}


def h_remove_send(p):
    tr = _track_at(p["track_index"])
    ok = RPR_RemoveTrackSend(tr, 0, int(p["send_index"]))
    if not ok:
        raise ValueError("RemoveTrackSend failed")
    return {"removed": True}


# ---- media / MIDI items

def _item_at(tr, item_index):
    it = RPR_GetTrackMediaItem(tr, int(item_index))
    if _is_null_ptr(it):
        raise ValueError(f"no item at index {item_index}")
    return it


def h_list_items(p):
    tr = _track_at(p["track_index"])
    n = int(RPR_CountTrackMediaItems(tr))
    out = []
    for i in range(n):
        it = RPR_GetTrackMediaItem(tr, i)
        take = RPR_GetActiveTake(it)
        take_name = ""
        if not _is_null_ptr(take):
            take_name = RPR_GetTakeName(take)
        out.append({
            "index": i,
            "position_sec": float(RPR_GetMediaItemInfo_Value(it, "D_POSITION")),
            "length_sec": float(RPR_GetMediaItemInfo_Value(it, "D_LENGTH")),
            "muted": bool(RPR_GetMediaItemInfo_Value(it, "B_MUTE")),
            "take_name": take_name,
        })
    return out


def h_insert_midi_item(p):
    tr = _track_at(p["track_index"])
    start = float(p["start_sec"])
    end = float(p["end_sec"])
    # CreateNewMIDIItemInProj(track, starttime, endtime, qnInOptional); 0 -> times in seconds
    it = RPR_CreateNewMIDIItemInProj(tr, start, end, 0)
    if _is_null_ptr(it):
        raise ValueError("CreateNewMIDIItemInProj failed")
    # newly created item is appended; its index is the last one
    item_index = int(RPR_CountTrackMediaItems(tr)) - 1
    RPR_UpdateArrange()
    return {"item_index": item_index, "position_sec": start, "length_sec": end - start}


def h_add_midi_note(p):
    tr = _track_at(p["track_index"])
    it = _item_at(tr, p["item_index"])
    take = RPR_GetActiveTake(it)
    if _is_null_ptr(take):
        raise ValueError("item has no active take")
    start = float(p["start_sec"])
    end = start + float(p["length_sec"])
    start_ppq = RPR_MIDI_GetPPQPosFromProjTime(take, start)
    end_ppq = RPR_MIDI_GetPPQPosFromProjTime(take, end)
    # MIDI_InsertNote(take, selected, muted, startppq, endppq, chan, pitch, vel, noSortIn)
    RPR_MIDI_InsertNote(
        take, False, False, start_ppq, end_ppq,
        int(p.get("channel", 0)), int(p["pitch"]), int(p.get("velocity", 96)), False,
    )
    RPR_MIDI_Sort(take)
    RPR_UpdateArrange()
    return {"pitch": int(p["pitch"]), "start_sec": start, "length_sec": float(p["length_sec"])}


def h_add_midi_notes(p):
    tr = _track_at(p["track_index"])
    it = _item_at(tr, p["item_index"])
    take = RPR_GetActiveTake(it)
    if _is_null_ptr(take):
        raise ValueError("item has no active take")
    notes = p.get("notes") or []
    if not notes:
        raise ValueError("notes must be a non-empty list")
    inserted = []
    for n in notes:
        start = float(n["start_sec"])
        end = start + float(n["length_sec"])
        start_ppq = RPR_MIDI_GetPPQPosFromProjTime(take, start)
        end_ppq = RPR_MIDI_GetPPQPosFromProjTime(take, end)
        # noSortIn=True: defer sorting so the whole batch is sorted once, below
        RPR_MIDI_InsertNote(
            take, False, False, start_ppq, end_ppq,
            int(n.get("channel", 0)), int(n["pitch"]), int(n.get("velocity", 96)), True,
        )
        inserted.append(
            {"pitch": int(n["pitch"]), "start_sec": start, "length_sec": float(n["length_sec"])}
        )
    RPR_MIDI_Sort(take)
    RPR_UpdateArrange()
    return {"inserted_count": len(inserted), "notes": inserted}


def h_delete_item(p):
    tr = _track_at(p["track_index"])
    it = _item_at(tr, p["item_index"])
    ok = RPR_DeleteTrackMediaItem(tr, it)
    if not ok:
        raise ValueError("DeleteTrackMediaItem failed")
    RPR_UpdateArrange()
    return {"deleted": True}


# ---- rendering

def h_render_project(_p):
    # 41824 = File: Render project, using the most recent render settings
    RPR_Main_OnCommand(41824, 0)
    return {"rendered": True}


def h_render_mixdown(p):
    """Quick-export a master-mix file to a caller-chosen dir, return its path.

    Unlike render_project (which reuses the user's manual render dialog as-is),
    this temporarily overrides the output path/name, render source (master mix)
    and bounds so the server gets a single predictable file to analyze, then
    restores every setting it touched. The codec is left as whatever the project
    is set to (usually WAV) — the server transcodes a small proxy for upload.

    params: out_dir (str), token (filename stem, default 'reaper_mcp_mixdown'),
    bounds_flag (int: 1 = entire project, 2 = time selection).
    """
    import glob

    out_dir = p["out_dir"]
    token = p.get("token", "reaper_mcp_mixdown")
    bounds_flag = int(p.get("bounds_flag", 1))
    pattern = os.path.join(out_dir, token + ".*")

    # Save everything we're about to change so the project is left untouched.
    saved_file = RPR_GetSetProjectInfo_String(0, "RENDER_FILE", "", False)[3]
    saved_pat = RPR_GetSetProjectInfo_String(0, "RENDER_PATTERN", "", False)[3]
    saved_bounds = RPR_GetSetProjectInfo(0, "RENDER_BOUNDSFLAG", 0, False)
    saved_settings = RPR_GetSetProjectInfo(0, "RENDER_SETTINGS", 0, False)

    # Clear any stale output so the freshly produced file is unambiguous.
    for stale in glob.glob(pattern):
        try:
            os.remove(stale)
        except OSError:
            pass

    try:
        RPR_GetSetProjectInfo_String(0, "RENDER_FILE", out_dir, True)
        RPR_GetSetProjectInfo_String(0, "RENDER_PATTERN", token, True)
        RPR_GetSetProjectInfo(0, "RENDER_BOUNDSFLAG", bounds_flag, True)
        RPR_GetSetProjectInfo(0, "RENDER_SETTINGS", 0, True)  # 0 = master mix
        # 41824 = render using most recent settings, no dialog (synchronous).
        RPR_Main_OnCommand(41824, 0)
    finally:
        RPR_GetSetProjectInfo_String(0, "RENDER_FILE", saved_file, True)
        RPR_GetSetProjectInfo_String(0, "RENDER_PATTERN", saved_pat, True)
        RPR_GetSetProjectInfo(0, "RENDER_BOUNDSFLAG", saved_bounds, True)
        RPR_GetSetProjectInfo(0, "RENDER_SETTINGS", saved_settings, True)

    produced = sorted(glob.glob(pattern), key=os.path.getmtime)
    if not produced:
        raise RuntimeError(
            "Render produced no file. Set the project's render format to a single "
            "audio file (e.g. WAV or MP3) and render source to 'Master mix'. If using "
            "bounds_flag=2, make sure a time selection exists."
        )
    return {"file": produced[-1], "bounds_flag": bounds_flag}


HANDLERS = {
    "ping": h_ping,
    "get_project_info": h_get_project_info,
    "list_tracks": h_list_tracks,
    "create_track": h_create_track,
    "delete_track": h_delete_track,
    "get_track_state": h_get_track_state,
    "set_track_volume_db": h_set_track_volume_db,
    "set_track_pan": h_set_track_pan,
    "set_track_mute": h_set_track_mute,
    "set_track_solo": h_set_track_solo,
    "rename_track": h_rename_track,
    "list_track_fx": h_list_track_fx,
    "add_fx_to_track": h_add_fx_to_track,
    "remove_fx": h_remove_fx,
    "set_fx_enabled": h_set_fx_enabled,
    "list_fx_presets": h_list_fx_presets,
    "set_fx_preset": h_set_fx_preset,
    "list_fx_params": h_list_fx_params,
    "set_fx_param": h_set_fx_param,
    "add_envelope_point": h_add_envelope_point,
    "clear_envelope": h_clear_envelope,
    "set_track_automation_mode": h_set_track_automation_mode,
    "set_track_record_arm": h_set_track_record_arm,
    "set_track_record_input": h_set_track_record_input,
    "transport_play": h_transport_play,
    "transport_stop": h_transport_stop,
    "transport_record": h_transport_record,
    "transport_pause": h_transport_pause,
    "set_cursor": h_set_cursor,
    "set_tempo": h_set_tempo,
    "run_action": h_run_action,
    "get_master_track": h_get_master_track,
    "set_master_volume_db": h_set_master_volume,
    "set_time_selection": h_set_time_selection,
    "clear_time_selection": h_clear_time_selection,
    "set_loop_enabled": h_set_loop_enabled,
    "list_markers": h_list_markers,
    "add_marker": h_add_marker,
    "delete_marker": h_delete_marker,
    "goto_marker": h_goto_marker,
    "list_sends": h_list_sends,
    "add_send": h_add_send,
    "set_send_volume": h_set_send_volume,
    "remove_send": h_remove_send,
    "list_items": h_list_items,
    "insert_midi_item": h_insert_midi_item,
    "add_midi_note": h_add_midi_note,
    "add_midi_notes": h_add_midi_notes,
    "delete_item": h_delete_item,
    "render_project": h_render_project,
    "render_mixdown": h_render_mixdown,
}


# ---------------------------------------------------------------------------
# JSON-over-TCP plumbing
# ---------------------------------------------------------------------------

def _dispatch(req):
    method = req.get("method")
    handler = HANDLERS.get(method)
    if handler is None:
        return {"id": req.get("id"), "ok": False, "error": f"unknown method: {method}"}
    try:
        RPR_Undo_BeginBlock()
        try:
            result = handler(req.get("params") or {})
        finally:
            RPR_Undo_EndBlock("MCP: " + str(method), -1)
        return {"id": req.get("id"), "ok": True, "result": result}
    except Exception as e:
        return {
            "id": req.get("id"),
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "trace": traceback.format_exc(),
        }


def _process_client(sock):
    try:
        chunk = sock.recv(65536)
    except BlockingIOError:
        return True
    except (ConnectionResetError, OSError):
        return False
    if not chunk:
        return False
    _clients[sock] += chunk
    while b"\n" in _clients[sock]:
        line, _, rest = _clients[sock].partition(b"\n")
        _clients[sock] = rest
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            resp = {"ok": False, "error": f"bad JSON: {e}"}
        else:
            resp = _dispatch(req)
        try:
            sock.sendall((json.dumps(resp) + "\n").encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False
    return True


def _tick():
    global _server
    try:
        if _server is None:
            return
        try:
            while True:
                client, _addr = _server.accept()
                client.setblocking(False)
                _clients[client] = b""
        except BlockingIOError:
            pass
        except OSError:
            pass
        for sock in list(_clients.keys()):
            if not _process_client(sock):
                try:
                    sock.close()
                except OSError:
                    pass
                _clients.pop(sock, None)
    except Exception:
        # Never let an exception break the defer loop.
        RPR_ShowConsoleMsg("[reaper-mcp] tick error: " + traceback.format_exc() + "\n")
    RPR_defer("_tick()")


def _shutdown():
    global _server
    for sock in list(_clients.keys()):
        try:
            sock.close()
        except OSError:
            pass
    _clients.clear()
    if _server is not None:
        try:
            _server.close()
        except OSError:
            pass
        _server = None
    RPR_ShowConsoleMsg("[reaper-mcp] bridge stopped\n")


def _start():
    global _server
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((HOST, PORT))
    s.listen(8)
    s.setblocking(False)
    _server = s
    RPR_ShowConsoleMsg("[reaper-mcp] bridge listening on " + HOST + ":" + str(PORT) + "\n")
    RPR_atexit("_shutdown()")
    RPR_defer("_tick()")


_start()
