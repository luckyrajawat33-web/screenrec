# ScreenSee Importer for After Effects

An ExtendScript script that imports a `.screensee` recording bundle
(produced by `screensee.py`) into After Effects and lays it out as a
fully editable composition.

Cursor artwork ships as `Cursor_Sprite.json` (Bodymovin/Lottie export)
and is reconstructed into native AE shape layers at import time — no
SVG dependency, no Bodymovin runtime needed.

## Install

1. Copy this whole `ae-plugin/` folder anywhere.
   `ScreenSeeImporter.jsx` and `Cursor_Sprite.json` must stay together.
2. Optional: drop `ScreenSeeImporter.jsx` (and `Cursor_Sprite.json`)
   into the AE Scripts folder so it shows up under **File > Scripts**:
   - **Windows:** `C:\Program Files\Adobe\Adobe After Effects <ver>\Support Files\Scripts\`
   - **macOS:** `/Applications/Adobe After Effects <ver>/Scripts/`
3. Restart AE if it was already open.

## Usage

1. **Record** with `python screensee.py`. The recorder writes a bundle
   to your TEMP folder named `screensee_<unix-time>.screensee/`.
2. In AE: **File > Scripts > Run Script File...** and pick
   `ScreenSeeImporter.jsx`.
3. Pick the `.screensee` folder when prompted.
4. AE opens a comp called **ScreenSee Edit**.

## What gets created

```
ScreenSee – <bundle-name>/
├── Sources/
│   └── raw.mkv                  ← imported footage
├── Recording                    ← precomp, source-resolution canvas
└── ScreenSee Edit               ← master comp (1920×1080)
```

### Main comp — `ScreenSee Edit`

Top to bottom in the timeline:

| Layer            | Role                                                         |
|------------------|--------------------------------------------------------------|
| `Recording Matte`| Rounded rectangle sized to the recording's **fit size** (the footprint it renders at with Zoom Level 1). Never reacts to zoom. Acts as alpha matte for Recording. |
| `Recording`      | Instance of the Recording precomp. Track Matte = Alpha. Scale + Position driven by Zoom Level / Zoom Position. |
| `Glass Halo`     | Soft white plate behind the recording. Size = recording fit size + halo on every edge; position is the fixed comp centre. **Zoom-independent.** |
| `Recording Shadow`| Rounded rectangle the exact size of the matte, sitting *below* the Glass Halo. Drop Shadow (Shadow-Only, Distance 0) so the blurred edge casts past the matte. The **Shadow** slider drives its Softness — a real, visible shadow the alpha matte can't clip. |
| `Background`     | Solid + Ramp gradient (BG Color A → B).                      |
| `Controls`       | Disabled null layer holding every adjustable slider/point/color. |

### Recording precomp — `Recording` (encoded-footage resolution)

| Layer (top → bottom)   | Role                                                          |
|------------------------|---------------------------------------------------------------|
| cursor sprites         | One shape layer per cursor in `Cursor_Sprite.json`, ordered by index — arrow on top. Each shows when the effective Style Index equals its slot. |
| `Cursor Position Null` | Position = Screen Pos (+ Smoothness). Cursors expression-link their Position to this null so their anchor (hotspot) lands exactly on it. |
| `Style Driver`         | Holds the cursor controls — see below. |
| `Screen Pos`           | Point Control "Screen Pos" — linear-keyed from MOVE events, times remapped through `frame_times`. |
| `Screen Recording Footage` | The raw.mkv footage.                                      |

**Style Driver controls:**

| Control            | Role                                                          |
|--------------------|---------------------------------------------------------------|
| `Auto Cursor`      | Checkbox, default **on**. On = the cursor follows the **Recorded Style** track (the OS cursor changes captured during recording — arrow → I-beam over text, hand over links, closed hand while dragging). Off = the cursor uses the manual **Cursor Style** picker. |
| `Cursor Style`     | Manual picker. A **Dropdown Menu Control** (AE 2020+) listing `Hidden` + every sprite in `Cursor_Sprite.json`; on older AE it falls back to a whole-number slider. Only takes effect when **Auto Cursor** is off. |
| `Recorded Style`   | Hold-keyed track baked from the recorder's `CURSOR_<style>` events. Drives the cursor when **Auto Cursor** is on. Not really meant to be edited by hand; visible so you can see/nudge its keyframes. |
| `Cursor Size`      | Cursor sprite size — each cursor's scale = Lottie intrinsic × `Cursor Size / 500`. |
| `Cursor Smoothness`| 0 = exact follow. >0 blends in `.smooth(0.1 + s × 0.5, 5)`. |

**Style Index:** `1` = Hidden, `2 .. N+1` = the cursor sprites in
`Cursor_Sprite.json` index order. Both the dropdown and the recorded
track use this numbering. **Adding a new cursor:** drop another layer
into `Cursor_Sprite.json` (give its opacity expression a unique
`=== <n>` index, or just let array order decide) and re-import — it
automatically joins the dropdown and gets its own shape layer.

Cursor shapes are baked from `Cursor_Sprite.json` so the file is
self-contained: paths, fills, strokes are reproduced as native AE
shape contents. Each cursor's anchor is set to the Lottie hotspot,
so the visible cursor tip lands exactly at the Screen Pos coordinate.

## Adjusting the look

Click the **Controls** null in `ScreenSee Edit` and open **Effect
Controls**. Every parameter is keyframable.

| Effect              | Default            | Lives on                | What it does                                            |
|---------------------|--------------------|-------------------------|---------------------------------------------------------|
| Padding             | 60                 | Main comp / Controls    | Empty space between recording and canvas edge. Recording is fit (not cover-cropped) inside the matte. |
| Roundness           | 14                 | Main comp / Controls    | Corner radius — shared by the matte, the shadow plate, and the halo so the rounded corners stay concentric. |
| Shadow              | 70                 | Main comp / Controls    | Softness of the `Recording Shadow` layer's drop shadow (Distance is fixed at 0, Opacity fixed). 0 = no visible shadow; higher = softer/wider. |
| Glass Halo          | 14                 | Main comp / Controls    | Width of the soft white plate behind the recording (no blur — keeps the edge crisp). Size = recording fit size + halo on every edge, position fixed at comp centre — **does not react to zoom**. |
| Halo Opacity        | 14                 | Main comp / Controls    | Fill opacity (%) of the Glass Halo plate. |
| Zoom Level          | 1.0                | Main comp / Controls    | Scale multiplier on the Recording precomp, **clamped to 1.0–2.0**. 1.0 = no zoom, fits inside matte. >1 zooms in with the matte clipping the overflow. Values outside the range snap back to the nearest bound. |
| Zoom Position       | (0.5, 0.5)         | Main comp / Controls    | Normalised pan target, **clamped to 0–1 on each axis**. Pan range = `max(0, zoomedSize − matteSize)`, so at Zoom Level 1 the slider has no effect; at >1, 0 puts the source's leading edge flush to the matte edge, 0.5 = centred, 1 = trailing edge flush. It can never expose empty matte. |
| BG Color A / B      | indigo / violet    | Main comp / Controls    | Top + bottom of the background gradient.                |
| Auto Cursor         | on                 | Recording / Style Driver | On = cursor follows the recorded OS cursor (`Recorded Style`); off = follows the manual `Cursor Style` picker. |
| Cursor Style        | first sprite       | Recording / Style Driver | Manual cursor picker — a dropdown of `Hidden` + every sprite (slider fallback on pre-2020 AE). Only used when `Auto Cursor` is off. |
| Recorded Style      | keyed              | Recording / Style Driver | Hold-keyed track baked from the recorder's `CURSOR_<style>` events. Used when `Auto Cursor` is on. |
| Cursor Size         | 200                | Recording / Style Driver | Cursor sprite size. Each cursor's scale = Lottie intrinsic × `Cursor Size / 500`. |
| Cursor Smoothness   | 0                  | Recording / Style Driver | 0 = exact follow. >0 blends in `.smooth(0.1 + s × 0.5, 5)`. |

## How zoom works (and why the matte stays put)

- `Recording Matte`, `Recording Shadow`, and `Glass Halo` all derive their size from one shared expression: `fit = Math.min(frameW/srcW, frameH/srcH)` — the recording's footprint at Zoom Level 1. None of them reference Zoom Level or Zoom Position, so they never move or resize when you zoom.
- `Recording` Scale = `Math.min(frameW/srcW, frameH/srcH) * 100 * clamp(Zoom Level, 1, 2)`. `Math.min` so the source **fits** inside the matte instead of cover-cropping it at Zoom Level 1.
- `Recording` Position offsets by `(clamp(Zoom Position, 0, 1) - 0.5) * max(0, scaledSrc - frame)`. The pan amount is the actual overflow past the matte, and Zoom Position is clamped to 0–1, so the recording edge can never pull inside the matte no matter what value is dialled in.

Only the footage precomp scales and pans; the matte, shadow, and halo stay locked together as a fixed rounded rectangle, and the recording slides inside it.

## Auto-pan to cursor (optional)

To follow the cursor automatically while zoomed, set Zoom Position via expression instead of a value. On **Recording → Transform → Position**, replace the `var zp = ...` line with:

```
var sp = thisLayer.source.layer("Screen Pos").effect("Screen Pos")("Point");
var zp = [sp[0] / thisLayer.source.width, sp[1] / thisLayer.source.height];
```

## Exporting

Standard **Composition > Add to Render Queue**. The alpha-matte
rounded-corner set-up is render-safe; cursor expressions sample at the
comp's render fps.

## Limitations

- **Canvas fixed at 1920×1080** at import time. Change via
  **Composition > Composition Settings...**; the expression-driven
  layout adapts.
- **No click ripples** in this build. The earlier ripple-per-click
  layers cluttered comps with hundreds of layers on long recordings.
  Add them back as a single shape with an expression-driven array of
  click times if you want them.
- **Zoom is uniform**, not per-click. Animate Zoom Level / Zoom
  Position keyframes manually around click points, or use the
  auto-pan-to-cursor expression above.
- **Cursor sprites are data-driven**: the dropdown and the `Cursor
  Style` index numbering come straight from `Cursor_Sprite.json`. Add
  or reorder sprites there and re-import — no script edits. The
  recorder side (`PYTHON_STYLE_TO_JSONIDX` in the script) only needs
  touching if you also teach `screensee.py` to emit a brand-new
  `CURSOR_<style>` event.
- **Drag tracking**: the recorder emits a one-shot `CURSOR_CLOSEDHAND`
  the moment a held-button drag starts and re-samples the real OS
  cursor when it ends, so the overlay shows the grab cursor through
  drags then reverts. Cursor *position* during drags already comes
  through the normal MOVE stream.

## Troubleshooting

- **"Missing Cursor_Sprite.json next to the script"** — the JSON file
  must sit in the same folder as `ScreenSeeImporter.jsx`.
- **"Selected folder is not a .screensee bundle"** — pick the bundle
  directory itself, not its parent. It must contain `raw.mkv`,
  `events.json`, and `meta.json`.
- **Cursor offset from where it should be** — each cursor's
  `Anchor Point` is the Lottie hotspot and its `Position` expression
  links to `Cursor Position Null`. If the offset is constant per
  cursor type, the hotspot in `Cursor_Sprite.json` is off. If it
  *grows over the clip*, the bundle predates the `frame_times` remap —
  re-import with the current script.
- **Cursor drifts late in the clip** — fixed: event keyframe times are
  remapped through `meta.frame_times` onto the footage's constant-fps
  playback timeline. Re-import an old bundle to pick up the fix.
- **Cursor too big / too small** — adjust `Cursor Size` on the
  `Style Driver` layer. Each cursor's scale = Lottie intrinsic ×
  `Cursor Size / 500`.
- **Cursor won't change automatically** — check `Auto Cursor` is on
  (Style Driver). With it off, the cursor follows the manual
  `Cursor Style` dropdown instead of the recorded track. If `Auto
  Cursor` is on and the cursor still doesn't change, `Recorded Style`
  on `Style Driver` has no keyframes — the recording carried no cursor
  samples (non-Windows capture, or the cursor sampler thread failed).
