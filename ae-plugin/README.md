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
| `Recording Matte`| Rounded rectangle. **Size depends only on Padding/Roundness.** Never scales with zoom. Acts as alpha matte for Recording. |
| `Recording`      | Instance of the Recording precomp. Track Matte = Alpha. Scale + Position driven by Auto Zoom / Zoom Level / Zoom Position. |
| `Glass Halo`     | Soft white plate behind the recording.                       |
| `Background`     | Solid + Ramp gradient (BG Color A → B) + Gaussian Blur.      |
| `Controls`       | Disabled null layer holding every adjustable slider/checkbox/point/color. |

### Recording precomp — `Recording` (source resolution, e.g. 2560×1440)

| Layer (top → bottom)   | Role                                                          |
|------------------------|---------------------------------------------------------------|
| `Arrow Cursor`         | Style Index === 0                                             |
| `textcursor`           | Style Index === 1                                             |
| `Pointinghand Cursor`  | Style Index === 2                                             |
| `Openhand Cursor`      | Style Index === 3                                             |
| `Closedhand Cursor`    | Style Index === 4                                             |
| `Cursor Position Null` | Parent of the 5 cursors. Position = Screen Pos (+ smoothing); Scale = Cursor Size / 500 × 100. |
| `Style Driver`         | Slider Control "Style Index" — hold-keyed from CURSOR_<style> events. |
| `Screen Pos`           | Point Control "Screen Pos" — linear-keyed from MOVE events.   |
| `Screen Recording Footage` | The raw.mkv footage.                                      |

Cursor shapes are baked from `Cursor_Sprite.json` so the file is
self-contained: paths, fills, strokes are reproduced as native AE
shape contents. Each cursor's anchor is set to the Lottie hotspot,
so the visible cursor tip lands exactly at the Screen Pos coordinate.

## Adjusting the look

Click the **Controls** null in `ScreenSee Edit` and open **Effect
Controls**. Every parameter is keyframable.

| Effect              | Default            | What it does                                            |
|---------------------|--------------------|---------------------------------------------------------|
| Padding             | 60                 | Empty space between recording and canvas edge.          |
| Roundness           | 14                 | Recording corner radius (also affects the matte).       |
| Shadow              | 70                 | Drop-shadow opacity / distance / softness.              |
| Glass Halo          | 14                 | Width + softness of the white plate behind the recording.|
| Auto Zoom           | off                | Master toggle for zooming the recording into the matte. |
| Zoom Level          | 2.0                | Multiplier when Auto Zoom is on.                        |
| Zoom Position       | (0.5, 0.5)         | Normalised point in source to centre when zoomed. `[0.5, 0.5]` = no pan; `[0, 0]` = top-left of source; `[1, 1]` = bottom-right. |
| BG Color A / B      | indigo / violet    | Top + bottom of the background gradient.                |
| Cursor Size         | 200                | Cursor sprite size. Expression scales by `sz / 500 × 100`. |
| Cursor Smoothness   | 0                  | 0 = exact cursor follow. >0 blends in `.smooth()`.      |

## How zoom works (and why the matte stays put)

- `Recording Matte` size is built from `[thisComp.width - pad*2, thisComp.height - pad*2]`.
  It has **no zoom in the expression** — only Padding and Roundness.
- `Recording` Scale is `cover-fit × Zoom Level` when Auto Zoom is on.
- `Recording` Position offsets by `(Zoom Position - 0.5) × source × scale`
  so the chosen source point lands at the matte centre. Only applied
  when Auto Zoom is on **and** Zoom Level > 1.

Result: the visible window stays the same size (the matte never moves),
the footage zooms inside it, and you pan by dragging the Zoom Position
point.

## Auto-pan to cursor (optional)

To make the zoomed footage follow the cursor automatically, replace
the Zoom Position expression with:

```
var sp = thisLayer.source.layer("Screen Pos").effect("Screen Pos")("Point");
[sp[0] / thisLayer.source.width, sp[1] / thisLayer.source.height];
```

Drop that on the **Recording → Transform → Position** expression in
place of the `var zp = ...` line.

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
- **Auto Zoom is uniform**, not per-click. Animate Zoom Level / Zoom
  Position keyframes manually around click points, or use the
  auto-pan-to-cursor expression above.
- **Cursor mapping is fixed**: arrow=0, text=1, pointer=2, openhand=3,
  closedhand=4. Reorder by editing `STYLE_TO_INDEX` in the script and
  the opacity expression indices in `Cursor_Sprite.json`.

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
