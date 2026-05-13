# ScreenSee Importer for After Effects

An ExtendScript script that imports a `.screensee` recording bundle
(produced by `screensee.py`) into After Effects and lays it out as a
fully editable composition.

Recording stays in the Python app. Editing — every slider, every color,
the cursor sprite — happens here, with native AE keyframes and
expressions you can scrub or animate.

## Install

1. Copy this whole `ae-plugin/` folder to a path of your choice. The
   `cursors/` folder must stay alongside `ScreenSeeImporter.jsx` — the
   script loads the SVG sprites by relative path.
2. (Optional, makes the script appear under **File > Scripts**:)
   drop `ScreenSeeImporter.jsx` into your AE Scripts folder. Default
   locations:
   - **Windows:** `C:\Program Files\Adobe\Adobe After Effects <ver>\Support Files\Scripts\`
   - **macOS:** `/Applications/Adobe After Effects <ver>/Scripts/`
   Then drop the `cursors/` folder into the same place.
3. Restart AE if it was already open.

## Usage

1. **Record** with `python screensee.py` and stop when done. ScreenSee
   writes a bundle to your TEMP folder named
   `screensee_<unix-time>.screensee/`.
2. In AE: **File > Scripts > Run Script File...** and pick
   `ScreenSeeImporter.jsx`. Or, if installed in the Scripts folder,
   pick it from **File > Scripts**.
3. The script asks you to select the `.screensee` bundle folder. Pick
   the folder (not a file inside it).
4. Wait for the import. AE opens a comp called **ScreenSee Edit**.

## What gets created

```
ScreenSee – <bundle-name>/
├── Sources/
│   └── raw.mkv                ← imported footage
├── Cursors/
│   ├── cursor.svg
│   ├── textcursor.svg
│   ├── pointinghand.svg
│   ├── openhand.svg
│   └── closedhand.svg
├── Cursor Sprite              ← precomp, holds 5 sprites + Style Driver
└── ScreenSee Edit             ← master comp (1920x1080)
```

The **ScreenSee Edit** comp contains, top to bottom:

| Layer            | Role                                                 |
|------------------|------------------------------------------------------|
| `Cursor`         | Precomp instance — sprite swaps via Style Index, position from MOVE keyframes mapped through the Recording layer's live scale + offset. |
| `Ripple N`       | One per left click; circle that scales + fades over 0.45 s. Toggleable. |
| `Recording`      | Footage from `raw.mkv`. Scale + Drop Shadow driven by Controls null. |
| `Recording Mask` | Rounded rectangle, used as alpha matte for Recording. |
| `Glass Halo`     | Soft white plate behind the recording. |
| `Background`     | Solid + Ramp gradient + Gaussian Blur. |
| `Controls`       | Disabled null layer holding every adjustable slider/color/checkbox. |

## Adjusting the look

Click the **Controls** null layer and open its **Effect Controls**
panel. Every parameter is a normal AE control — you can drag, type a
value, set keyframes, animate, link from other expressions, etc.

| Effect                | What it changes                              |
|-----------------------|----------------------------------------------|
| Padding               | Empty space between recording and canvas edge|
| Roundness             | Corner radius of the recording                |
| Shadow                | Drop-shadow opacity + distance + softness     |
| Glass Halo            | Width + softness of the white plate behind   |
| Background Blur       | Gaussian blur on the gradient background     |
| BG Color A / B        | Top + bottom of the background gradient      |
| Cursor Size           | Cursor sprite scale                          |
| Cursor Smoothness     | 0 = exact follow; >0 blends in `.smooth()`   |
| Auto-hide Cursor      | Drops cursor opacity to 0 when idle          |
| Click Ripples         | Master toggle for all ripple layers          |
| Auto Zoom + Zoom Level| Scales the Recording layer up uniformly       |

### Cursor sprite per frame

The Cursor Sprite precomp contains a `Style Driver` null layer with a
**Style Index** slider that holds one keyframe per `CURSOR_<style>`
event from the recording. Each of the 5 sprite layers has an opacity
expression of the form `Math.round(styleIndex) === N ? 100 : 0`, so the
visible sprite swaps automatically when the OS cursor changed during
capture (text I-beam over text fields, hand over links, etc.).

To force a single sprite, scrub the Style Index slider — values 0..4 map
to arrow / text / pointer / openhand / closedhand.

## Exporting

Standard **Composition > Add to Render Queue**. The mask + matte set-up
is render-safe; the cursor expressions will be sampled at the comp's
render fps.

## Limitations

- **SVG fidelity:** AE imports SVGs as continuously-rasterized footage.
  On older AE versions some SVGs may render with incorrect geometry. If
  you see a broken cursor, open the `cursors/` folder and replace the
  offending file with a PNG of the same name (the script imports by
  filename, so any AE-readable format works).
- **Auto Zoom is uniform**, not per-click. The Python tool zooms in on
  each click and out again — replicating that here would mean baking
  thousands of scale keyframes on the Recording layer. Easier path:
  toggle Auto Zoom on, set Zoom Level, and animate manually with two
  keyframes per click of interest.
- **Many ripples = many layers.** Each click becomes a shape layer.
  Recordings with hundreds of clicks will be slow to load. Trim the
  recording first if needed.
- **Canvas is fixed at 1920x1080** at import time. To change: select the
  master comp, **Composition > Composition Settings...**, set the new
  size. Padding/scale expressions will recompute automatically.
- **No automated update.** If you re-record, run the script again on the
  new bundle; the old comp keeps its tweaks.

## Troubleshooting

- **"Selected folder is not a .screensee bundle"** — the script needs
  `raw.mkv`, `events.json`, `meta.json` in the picked folder. Make sure
  you picked the bundle directory, not its parent.
- **"Could not find cursors/ next to the script"** — the `cursors/`
  folder must sit beside `ScreenSeeImporter.jsx`. Don't move them
  apart.
- **Cursor sprite sits in the wrong place relative to the click** — the
  hotspot table in the script (`CURSOR_TIPS`) targets the visible tip
  of each SVG. If you replaced an SVG with a different design, update
  the matching row.
- **Expression errors after import** — check that your AE version is
  CC 2014 or newer. Older builds don't support the same `.smooth()` /
  `valueAtTime` semantics used by the cursor-position expression.
