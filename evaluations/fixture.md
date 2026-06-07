# Evaluation fixture — "MCP Eval Session"

The questions in `reaper_eval.xml` are answered against this fixed reference project.
Because a Reaper project is user-specific and dynamic, the eval is only meaningful when
this exact project is loaded — the answers are stable *by construction* of this fixture.

Only stock Cockos FX are used (ReaComp / ReaEQ / ReaDelay / ReaVerbate), which ship with
every Reaper install, so the fixture is reproducible on any machine.

## Project settings
- Tempo: **124 BPM**
- Time selection: **32 s – 48 s**

## Tracks (0-based index)
| idx | name        | volume (dB) | pan  | FX chain              |
|-----|-------------|-------------|------|-----------------------|
| 0   | Drums       | 0.0         | 0.0  | ReaComp, ReaEQ        |
| 1   | Bass        | -3.0        | 0.0  | ReaEQ                 |
| 2   | Lead        | -6.0        | -0.3 | ReaDelay              |
| 3   | Pad         | -8.0        | 0.3  | (none)                |
| 4   | Reverb Bus  | -2.0        | 0.0  | ReaVerbate            |

## Sends
- Lead (2) → Reverb Bus (4) at **-6 dB**
- Pad (3) → Reverb Bus (4) at **-10 dB**

## Markers & regions
- Marker "Intro" at 0 s
- Marker "Verse" at 16 s
- Marker "Chorus" at 32 s
- Region "Chorus Section" 32 s – 48 s

## MIDI
- Bass (1): one MIDI item from 0 s – 8 s

## Known limitation (marker/region names)
Marker and region **names** are set in Reaper's UI but cannot be read back through
the ReaScript Python `EnumProjectMarkers` binding, so `reaper_list_markers` returns
empty `name` fields. The eval questions therefore identify markers/regions by
**position, type, and count** rather than name.

## Building the fixture
Start from an empty project. The fixture can be constructed entirely through this
server's write tools (`reaper_set_tempo`, `reaper_create_track`, `reaper_set_track_volume_db`,
`reaper_set_track_pan`, `reaper_add_fx_to_track`, `reaper_add_send`, `reaper_set_send_volume_db`,
`reaper_add_marker`, `reaper_add_region`, `reaper_set_time_selection`, `reaper_insert_midi_item`).
The exact FX name strings come from `reaper_list_installed_fx` (e.g. `"VST: ReaComp (Cockos)"`);
copy them verbatim, as Reaper matches by exact suffix.
