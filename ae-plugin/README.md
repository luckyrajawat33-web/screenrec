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
| `Recording Matte`| Rounded rectangle sized to the recording's **fit size** (the footprint it renders at with Zoom Level 1) — the recording precomp's exact footprint with rounded corners. Never reacts to zoom. Acts as alpha matte for Recording. |
| `Recording`      | Instance of the Recording precomp. Track Matte = Alpha. Scale + Position driven by Zoom Level / Zoom Position. |
| `Recording Shadow`| Rounded rectangle the exact size of the matte, sitting just below the recording. Has a Drop Shadow (Shadow-Only, Distance 0) so the blurred edge casts past the matte. The **Shadow** slider drives its Softness — a real, visible shadow the alpha matte can't clip. |
| `Glass Halo`     | Soft white plate behind the recording. Size = recording fit size + halo on every edge; position is the fixed comp centre. **Zoom-independent** — only the footage inside the matte zooms. |
| `Background`     | Solid + Ramp gradient (BG Color A → B).                      |
| `Controls`       | Disabled null layer holding every adjustable slider/point/color. |

### Recording precomp — `Recording` (source resolution, e.g. 2560×1440)

| Layer (top → bottom)   | Role                                                          |
|------------------------|---------------------------------------------------------------|
| `Arrow Cursor`         | Style Index === 1                                             |
| `textcursor`           | Style Index === 2                                             |
| `Pointinghand Cursor`  | Style Index === 3                                             |
| `Openhand Cursor`      | Style Index === 4                                             |
| `Closedhand Cursor`    | Style Index === 5                                             |
| `Cursor Position Null` | Position = Screen Pos (+ Smoothness). Cursors expression-link their Position to this null so their anchor (hotspot) lands exactly on it. |
| `Style Driver`         | Holds **Style Index** (hold-keyed from CURSOR_<style> events; snapped to whole numbers), **Cursor Size**, and **Cursor Smoothness**. Lives here, not in the main Controls, so the cursor knobs are alongside the cursors they drive. |
| `Screen Pos`           | Point Control "Screen Pos" — linear-keyed from MOVE events.   |
| `Screen Recording Footage` | The raw.mkv footage.                                      |

**Style Index levels:** `0` = no cursor (hidden), `1` = arrow, `2` = text,
`3` = pointer, `4` = open hand, `5` = closed hand. The slider snaps to
whole numbers via a `Math.round(value)` self-expression — there are no
fractional in-between states. Set it to `0` (or anything outside 1–5) to
hide the cursor entirely.

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
| Zoom Level          | 1.0                | Main comp / Controls    | Scale multiplier on the Recording precomp, **clamped to 1.0–2.0**. 1.0 = no zoom, fits inside matte. >1 zooms in with the matte clipping the overflow. Values outside the range snap back to the nearest bound. |
| Zoom Position       | (0.5, 0.5)         | Main comp / Controls    | Normalised pan target, **clamped to 0–1 on each axis**. Pan range = `max(0, zoomedSize − matteSize)`, so at Zoom Level 1 the slider has no effect; at >1, 0 puts the source's leading edge flush to the matte edge, 0.5 = centred, 1 = trailing edge flush. It can never expose empty matte. |
| BG Color A / B      | indigo / violet    | Main comp / Controls    | Top + bottom of the background gradient.                |
| Style Index         | arrow (1)          | Recording / Style Driver | Which cursor sprite is visible — `0` hides it, `1`–`5` = arrow / text / pointer / open hand / closed hand. Hold-keyed from CURSOR_<style> events and snapped to whole numbers. Scrub to override. |
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
- **Cursor mapping is fixed**: 0=hidden, 1=arrow, 2=text, 3=pointer,
  4=openhand, 5=closedhand. Reorder by editing `STYLE_TO_INDEX` in the
  script (the importer shifts the JSON's 0-based indices by +1).

## Troubleshooting

- **"Missing Cursor_Sprite.json next to the script"** — the JSON file
  must sit in the same folder as `ScreenSeeImporter.jsx`.
- **"Selected folder is not a .screensee bundle"** — pick the bundle
  directory itself, not its parent. It must contain `raw.mkv`,
  `events.json`, and `meta.json`.
- **Cursor offset from where it should be** — `Cursor Position Null`'s
  anchor point should be at `[0, 0]` and each cursor's `Position`
  should be `[0, 0]`. The Lottie anchor goes on each cursor shape
  layer's `Anchor Point`, which is the hotspot.
- **Matte appears to scale with zoom** — open the actual
  `Rectangle Path 1 → Size` expression on `Recording Matte`. It should
  contain only `Padding`. If you see a reference to Recording's scale,
  the project is from an older version of the script — re-import or
  paste in the correct expression.
- **Cursor too big / too small** — adjust `Cursor Size` on the
  Controls layer. The scale expression is `sz / 500 × 100`, calibrated
  for the 500×500 Lottie source. To change the formula, edit
  `Cursor Position Null → Transform → Scale`.
