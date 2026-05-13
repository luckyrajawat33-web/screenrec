# ScreenSee — Architecture

A walkthrough of the internals: what every block does, how data flows
between them, and the design decisions worth knowing before changing
anything. Pairs with `SETUP.md` (install/usage). Source of truth is
the code; this doc is the map.

> Single-file codebase by design (`screensee.py`, ~1900 lines). Easier
> to read end-to-end than a sprawling package while the project is
> small. Splitting becomes worthwhile once we ship a non-Windows
> backend or replace the PIL compositor with a GPU one.

---

## 1. Big picture: two-pass recorder

ScreenSee is a **two-pass** screen recorder:

```
┌────────────┐         ┌──────────────────────┐         ┌────────────┐
│  Recorder  │ ──────► │  .screensee bundle   │ ──────► │  Renderer  │
│            │  write  │   raw.mkv            │  read   │  (preview  │
│  capture + │         │   events.json        │         │   + export)│
│  events    │         │   meta.json          │         │            │
└────────────┘         └──────────────────────┘         └────────────┘
```

**Why two passes?**
- Effects can be re-tuned without re-recording. Slide the cursor
  smoothness, change the background gradient, swap the cursor sprite
  — every setting is a function over the bundle, not over the
  hardware.
- The recorder's job becomes simple and predictable: drop raw frames
  into ffmpeg, log input events, persist timestamps. No compositing
  during capture means no frame drops from rendering pressure.
- Preview and export run the **same** code over the same bundle.
  What you see in the editor is what gets exported.

The bundle is the contract between the two halves.

---

## 2. File map

```
screensee.py     single-file app
  ├─ deps & feature flags  (top, ~lines 1-60)
  ├─ Theme + constants     (GRADIENTS, SOLIDS, CANVAS_PRESETS)
  ├─ Physics
  │    MassSpringDamper      cursor + camera spring
  │    SpringSmoother        thin alias (back-compat)
  │    OneEuroFilter         legacy, unused at runtime
  │    ZoomEngine            timed ease-in/out for zoom level
  ├─ Cursor system
  │    ARROW_PTS / HAND_PTS  polygon definitions
  │    CURSOR_STYLES         registry of cursor styles
  │    _load_svg_sprite      cairosvg rasteriser + cache
  │    _draw_sprite          builds sprite for one style
  │    draw_cursor           draws sprite + click/drag rings
  ├─ Ripple                  click-ripple class
  ├─ Compositor
  │    make_canvas           background only (gradient/solid/image)
  │    build_chrome          bg + shadow + glass halo
  │    paste_recording       fast per-frame: rec → chrome
  │    composite             back-compat wrapper
  ├─ Animation
  │    precompute_track      events + meta -> per-frame state
  │    render_frame          one composited PIL frame
  │    canvas_dims           resolve cw, ch from settings
  ├─ Recorder                capture pipeline
  ├─ Processor               export (bundle -> MP4)
  └─ App (ctk.CTk)           UI shell
       welcome screen
       editor screen
       sidebar + preview + transport
cursors/                     SVG cursor sprites
SETUP.md                     install/usage
ARCHITECTURE.md              this file
requirements.txt
```

---

## 3. The bundle format

A `.screensee` "bundle" is a directory written under TEMP:

```
screensee_1737120000.screensee/
├── raw.mkv          H.264 (CRF 18, GOP fps/2), cursor-excluded
├── events.json      [{"t":1.234,"type":"MOVE","x":1000,"y":540}, ...]
└── meta.json        fps, region, frame_count, frame_times[], backend
```

**Why these three files?**

- **raw.mkv** is the cursor-free screen capture. We pick a short GOP
  (`-g fps/2`, ~0.5 s between keyframes) so editor scrubbing seeks
  to a nearby keyframe instead of decoding 2 s of GOP. That's the
  difference between a 50 ms scrub and a 500 ms scrub.
- **events.json** holds every pynput event (MOVE / CLICK_L / CLICK_R
  / RELEASE_L / RELEASE_R / SCROLL_UP / SCROLL_DOWN) with
  perf_counter-relative timestamps and global screen coords. The
  renderer needs these to draw the synthetic cursor and click
  ripples on top of the cursor-free footage.
- **meta.json** carries the things the renderer needs to align time
  and space — `fps`, `region` (which monitor / what dimensions),
  `frame_count`, `frame_times` (an explicit per-frame content-time
  array; see §7), and `backend` (was this recorded with WGC or
  mss?).

The bundle is **self-contained**: with these three files you can
reproduce any export at any point in the future. Settings live
client-side and are applied at render time.

---

## 4. Recorder

```
┌──── pynput Listener thread ────┐
│  on_move / on_click / on_scroll│  ──► self._events list
│  perf_counter() - t0           │      (timestamped events)
└────────────────────────────────┘

┌──── capture backend ───────────┐
│  WGC.on_frame_arrived  OR      │  ──► _write_frame(bgra, content_t)
│  mss .grab loop                │      decimate to target fps
│  perf_counter() - t0           │      write BGRA to ffmpeg stdin
│                                │      append content_t to frame_times
└────────────────────────────────┘

ffmpeg subprocess:  rawvideo BGRA  ──►  libx264 (ultrafast/CRF 18, GOP fps/2)  ──►  raw.mkv
```

### Backends

| Backend           | API                          | Cursor in frames? | Perf      |
|-------------------|------------------------------|-------------------|-----------|
| **WGC**           | Windows.Graphics.Capture     | No (we set `cursor_capture=False`) | GPU, low CPU |
| **mss**           | BitBlt + GetDIBits           | No (BitBlt doesn't capture cursor on Windows) | CPU, ~25 fps at 1440p |

Both produce cursor-free frames on Windows. WGC is preferred when
available; the `_active_backend` field on `Recorder` records what
actually ran (WGC can throw at session-start time on older Windows
builds — see §11).

### Threading model

- **Main thread** runs Tk's event loop and the App.
- **Capture thread** (mss path) is created by `_start_mss`; a Python
  `threading.Thread` running `loop()`.
- **WGC worker thread** is internal to the `windows-capture` library;
  it invokes our `on_frame_arrived` callback.
- **Listener thread** is `pynput.mouse.Listener` running its own
  thread.

Everything that touches `self._events` or writes to ffmpeg holds
`self._lock`. Frame writes through `_write_frame` are not lock-bound
because frames are produced by a single thread per backend.

### Decimation

`_write_frame` enforces the target fps:

```python
def _write_frame(self, bgra, content_t=None):
    t = self._now() if content_t is None else content_t
    if t - self._last_write < (1.0 / self.fps) - 0.001:
        return
    self._last_write = t
    self._ffmpeg.stdin.write(bgra.tobytes())
    self._frame_times.append(t)
    self._frame_idx += 1
```

WGC delivers frames at monitor refresh (60 Hz typically); decimating
to 30 fps means we keep ~every-other frame. The `content_t` argument
is the **time the pixels actually represent** — for mss it's
stamped before `.grab()` (which may take 10-30 ms to return); for
WGC it's the callback start time. This is what makes the cursor land
on the right frame (see §7).

---

## 5. Animation engine — `precompute_track()`

The function that bridges the bundle and the renderer.

**Input:** `events`, `meta`, settings dict `s`.
**Output:** a list, one dict per frame, of cursor/zoom/ripple state.

```python
track[fi] = {
    "sx": ...,  "sy": ...,          # smoothed cursor in source-pixel space
    "zoom": ..., "zcx": ..., "zcy": ...,  # zoom level + camera centre (normalised)
    "pressed": bool, "dragging": bool,
    "ripples": [(rx, ry, age, dur), ...],
    "opacity": float, "tilt": 0.0,
}
```

### Cursor smoothing — MassSpringDamper

Two-coordinate mass-spring-damper:

```
acceleration = (target - position) * k / m - velocity * b / m
velocity    += acceleration * dt
position    += velocity * dt
```

`k` (stiffness), `b` (damping), `m` (mass) come from the
**Smoothness** slider (`_settings()` maps slider 0..1 → `k=470..120`,
`b = 0.92 * 2√(km)` so damping is always 0.92 of critical).

At slider 0 the spring is **bypassed**: `sx, sy = cur_x, cur_y`
exactly. This matters because the synthetic cursor is the only
cursor (footage is cursor-free), so users want pixel-accurate
follow.

### Camera pan — second spring

The zoom *level* animates via `ZoomEngine` (timed cubic ease-in-out,
in=1.0 s / out=1.4 s — not a spring). The zoom *centre* is driven
by a separate, much gentler spring (`k=30, b=21, m=3`, overdamped,
~1.4 s settle).

Crucially, the camera target isn't the raw cursor — it's a blend:

```python
target = 0.6 * click_anchor + 0.4 * (cursor / sw)
```

This matches Screen Studio's behaviour: the camera anchors on the
click point and only leans toward the cursor when it strays. Pure
cursor-following with a faster spring produces a "swimming" feel.

### Click ripples

Every CLICK_L is queued. Each frame emits the currently-alive
ripples for that timestamp, defined as `0 <= tf - t_start < 0.45 s`.
Drawing happens in the renderer — `precompute_track` just decides
which ripples are alive on which frame.

### Loop-to-start blend

If `s["loop_cursor"]` is on, the last ~0.8 s of `track` has its
`sx, sy` eased back to `track[0]["sx"], track[0]["sy"]` with an
out-cubic. This is post-processing on the precomputed track — no
spring interaction, just a blend over the last N frames.

---

## 6. Renderer — `build_chrome` + `paste_recording`

```
            ┌─── once per (cw, ch, padding, bg, ...) ───┐
            │  build_chrome():                          │
            │   make_canvas (gradient/solid/image)      │
            │   bg blur (optional)                      │
            │   shadow (rounded rect + GaussianBlur)    │
            │   glass halo (frosted plate + rim blur)   │
            │  returns (canvas, rx, ry, ow, oh)         │
            └───────────────────────────────────────────┘
                              │
                              ▼
                       chrome cache
                              │
            ┌──────────── per-frame ────────────┐
            │  raw_bgr  --crop+resize on zoom-->│
            │  PIL convert                      │
            │  draw ripples                     │
            │  draw cursor sprite               │
            │  paste_recording onto chrome      │
            └───────────────────────────────────┘
                              │
                              ▼
                     composited PIL frame
```

### Why split `composite` into `build_chrome` + `paste_recording`?

Chrome (background gradient + shadow blur + glass blur) is the slow
part — each pass through `make_canvas` does ~1080 ImageDraw line
calls for the gradient; the shadow and glass each do a `GaussianBlur`.
At 1080p the chrome alone is ~90 ms.

But chrome inputs change rarely: the user slides padding once and
then watches playback; the gradient never changes. So we cache the
chrome on the App, keyed by

```python
(cw, ch, bg_type, bg_val, sw, sh,
 padding, inset, roundness, shadow, glass, bg_blur)
```

and reuse it for every frame whose chrome inputs match. The fast
path becomes: decode raw frame → zoom → draw cursor/ripples →
**`paste_recording`** (resize, rounded mask, paste). Total ~25 ms.

That's the difference between an ~8 fps preview and a ~30 fps
preview.

The export `Processor` builds chrome **once** at the top of
`run()` and reuses it for every frame.

### Zoom application

`render_frame` does the zoom crop at full source resolution:

```python
if z > 1.02:
    rw, rh = int(w/z), int(h/z)
    x1, y1 = _zoom_origin(zcx, zcy, z, sw, sh)
    zoomed = cv2.resize(raw_bgr[y1:y1+rh, x1:x1+rw], (w, h), INTER_LINEAR)
```

The cursor is drawn on this `zoomed` (source-resolution) image, so
its position must be remapped:

```python
draw_sx = (sx - x1z) * z
draw_sy = (sy - y1z) * z
```

Ripples are remapped the same way.

`_zoom_origin` clamps the crop to `[0, sw-rw/z]` and `[0, sh-rh/z]`
so we never read past the source frame.

---

## 7. Time, the unsexy detail that makes everything line up

Everything in the recorder uses `time.perf_counter()` against a
single `self._t0` set at record-start:

```python
def _now(self):
    return time.perf_counter() - self._t0
```

So `events.json` timestamps and `meta.json[frame_times]` are on the
**same clock**. The renderer can ask "what events happened at frame
fi's content time?" and get the right answer.

`frame_times` exists because `frame_index / fps` is **not** the
right time for an arbitrary frame. WGC delivers at monitor refresh
and we decimate; mss spends real time inside `.grab()`. The actual
content-time of frame N drifts up to half a frame from `N/fps`.

`precompute_track` uses `frame_times[fi]` when available, falling
back to `fi/fps` for old bundles. That's why click ripples now
appear on the frame the click actually corresponds to, not a frame
or two early/late.

---

## 8. Cursor system

```
CURSOR_STYLES (registry)
   ├─ "arrow"      kind="svg"      cursor.svg          tip (0.30, 0.18)
   ├─ "pointer"    kind="svg"      pointinghand.svg    tip (0.45, 0.06)
   ├─ "openhand"   kind="svg"      openhand.svg        tip (0.50, 0.30)
   ├─ "closedhand" kind="svg"      closedhand.svg      tip (0.50, 0.40)
   ├─ "text"       kind="svg"      textcursor.svg      tip (0.50, 0.50)
   ├─ "arrow_poly" kind="polygon"  ARROW_PTS           tip (0, 0)
   ├─ "modern"     kind="polygon"  ...
   ├─ "triangle"   kind="polygon"  ...
   ├─ "dot"        kind="circle"   diameter 16
   └─ "ring"       kind="ring"     diameter 22
```

**Each style has a tip**, a normalised (or absolute, for polygons)
point inside the sprite that anchors to `(cx, cy)` when drawn.
Without the tip the cursor would be offset.

### Sprite construction

`_draw_sprite(style, scale, opacity)` returns `(sprite, ax, ay)`.

- For `kind="polygon"`: draws shadow + fill + outline on a
  `64*scale` transparent canvas using `ImageDraw.polygon`.
- For `kind="svg"`: calls `_load_svg_sprite(file, target_h)` which
  invokes `cairosvg.svg2png` and caches by `(file_name, height)`.
  Rasterised at 2× target, then Lanczos-shrunk so antialiased edges
  survive the downstream canvas resize.
- For `kind="circle"` / `"ring"`: ImageDraw ellipses.

Opacity is applied per-pixel for SVGs (alpha channel multiply) and
inline as part of fill colour for polygons.

### Auto-state swap

`draw_cursor(... pressed=, dragging=, style=)`:

```python
eff_style = "closedhand" if dragging else style
sprite, ax, ay = _draw_sprite(eff_style, scale, opacity)
img_rgba.paste(sprite, (cx-ax, cy-ay), sprite)

if pressed:
    # accent ring overlay (works for any style)
    # bigger ring + filled inner dot when dragging
```

`precompute_track` decides `dragging = pressed and spd > 8`, so the
state arrives at `draw_cursor` pre-computed.

### Graceful fallback

If `cairosvg` failed to import OR the SVG file is missing,
`_draw_sprite` for `kind="svg"` falls through to
`_draw_sprite("arrow_poly", ...)`. The cursor never disappears.
The picker also hides the SVG options entirely if `cairosvg` is
absent so you don't see five buttons that all render the same
fallback arrow.

---

## 9. App shell

CustomTkinter, two states:

```
┌────────────── App ──────────────┐
│  state: welcome  |  editor       │
└──────────────────────────────────┘

WELCOME                             EDITOR
┌──────────────────┐                ┌────── top bar ──────┐
│                  │                │  ScreenSee   Export │
│    ScreenSee     │                ├──── preview ────────┤
│                  │       Stop     │                     │
│  [ Start Rec ]   │  ──────────►   │      [ canvas ]     │
│                  │                │                     │
│  30 / 60 fps     │                ├── transport ────────┤
│  Capture: WGC    │                │  ▶ ── scrub ── 0:08 │
│                  │                ├── sidebar  ─────────┤
└──────────────────┘                │  Canvas, BG, Frame, │
                                    │  Cursor, AutoZoom   │
                                    └─────────────────────┘
```

### State switch
- `_show_welcome()`: packs the welcome frame, forgets the editor.
- `_show_editor()`: builds the editor frame lazily on first call,
  packs it, then schedules `_load_bundle_into_preview()` 150 ms
  later so widgets are sized before the first render.

### Settings → render

```
slider change
   │ (Tk variable trace_add 'write')
   ▼
_request_render(recompute_track=True)
   │ debounces with self.after(40, _do_render)
   ▼
_do_render
   │ builds track_key from s; if changed -> precompute_track(...)
   │ calls _render_current_frame
   ▼
_render_current_frame
   │ src.set(POS_FRAMES, fi); src.read()
   │ builds chrome_key from s; if changed -> build_chrome(...) ─► self._chrome
   │ render_frame(frame, track[fi], s, ..., chrome=self._chrome)
   │ fit to preview size, ImageTk.PhotoImage, swap on label
```

Two caches in flight here:
- **Track cache**, invalidated when cursor/zoom/click physics
  inputs change.
- **Chrome cache**, invalidated when chrome inputs (bg/padding/...)
  change.

Most slider drags only touch one or the other.

### Playback

`_toggle_play` flips `self._playing`; `_tick_play` advances
`_cur_frame` and reschedules itself with `self.after(50 ms, ...)`.
Preview cadence is capped at 20 fps to keep PIL compositing under
control. Scrubbing goes through `_on_scrub` which is guarded by
`_programmatic` so programmatic `scrub_var.set()` during playback
doesn't fire a feedback loop.

---

## 10. Processor — export

The export equivalent of `_render_current_frame`, but iterates the
whole bundle and pipes BGR frames to ffmpeg:

```python
def run(self, bundle_dir, out_path):
    meta, events_raw = load(bundle_dir)
    track = precompute_track(events_raw, meta, s)
    chrome = build_chrome(...)             # once

    src = cv2.VideoCapture(bundle_dir / "raw.mkv")
    proc = subprocess.Popen(["ffmpeg", ..., "-c:v","libx264","-crf","17"])

    for fi in range(len(track)):
        ok, frame = src.read()
        if not ok: break
        canvas = render_frame(frame, track[fi], s, sw, sh, cw, ch, chrome=chrome)
        proc.stdin.write(rgba_to_bgr(canvas).tobytes())
        if fi % 15 == 0: progress_cb(...)
```

Encoder: `libx264 -preset fast -crf 17`. CRF 17 is visually
lossless for screen content. Container is `.mp4` (H.264) for
download/share friendliness — different from `raw.mkv` which prefers
mkv for keyframe density.

`Processor` runs on a background thread (`threading.Thread`
spawned from `App.export_mp4`) so the UI doesn't freeze during
export. Progress updates land back on the App via the
`progress_cb` (`_on_progress`), which writes to a `DoubleVar` for
the progress bar — Tk variables are thread-safe to write.

---

## 11. Graceful degradation, everywhere

The app is full of paths where a dependency fails and we keep going:

| What fails              | What we do                                              |
|-------------------------|---------------------------------------------------------|
| `windows-capture` import| `HAS_WGC=False` → mss path                              |
| WGC `start_free_threaded()` raises (Windows build bug) | Catch, log, transparently start `_start_mss`            |
| `cairosvg` import       | `HAS_CAIROSVG=False` → SVG styles hidden from picker; polygon arrow used |
| Individual SVG file missing | `_draw_sprite` falls back to `arrow_poly`              |
| ffmpeg not on PATH      | `Recorder.start` raises RuntimeError → "Recorder" dialog |
| Bundle has < 5 frames   | Welcome dialog "Recording failed" with workaround       |
| `raw.mkv` won't decode  | Editor refuses to enter, shows error                    |
| Image bg file unreadable| `make_canvas` silently uses the default near-black bg   |

Rule of thumb: failure modes the user can't fix themselves (a
Windows bug we have no control over) get **silently** worked around;
failure modes the user must fix (no ffmpeg) get a **loud** dialog.

---

## 12. Performance notes

### Preview FPS bottlenecks (in priority order)

1. **Chrome generation** — gradient + shadow blur + glass blur.
   *Solved by chrome caching* (§6). Was ~90 ms; now ~0 ms after
   first render.
2. **H.264 keyframe seek** — `VideoCapture.set(POS_FRAMES)` decodes
   from previous keyframe forward. *Solved by short GOP* in the
   recorder (`-g fps/2`). ~30 ms → ~10 ms scrub.
3. **`cv2.resize` zoom crop** — ~5 ms at 1080p, ~15 ms at 1440p.
   Acceptable.
4. **PIL `fromarray` + paste** — ~5 ms. Acceptable.
5. **Final downscale to preview size** — ~5 ms with LANCZOS.
   Acceptable.

Realistic preview budget after the optimisations: ~25 ms per frame,
i.e. 30-40 fps capped at the 20 fps tick interval.

### Memory

The app keeps essentially nothing in RAM:
- Recorder streams to ffmpeg; no frame list.
- Bundle lives on disk.
- Preview decodes one frame at a time (plus the cached chrome image).
- Track is in RAM but it's small: ~12 floats per frame × frame_count
  × 8 bytes = ~3 MB for a 60 s 30 fps recording.

---

## 13. Where to look first (common tasks)

| You want to...                            | Start here                              |
|-------------------------------------------|------------------------------------------|
| Add a new background type                 | `make_canvas` + chrome key + sidebar tabs |
| Add a new cursor shape                    | `CURSOR_STYLES` + `_draw_sprite`         |
| Tune the spring constants                 | `_settings()` (mapping) + `precompute_track` |
| Change zoom/pan timing                    | `ZoomEngine.trigger/release` defaults + `precompute_track` screen spring |
| Add a new event type (e.g. keystrokes)    | `Recorder._on_*` callbacks + `events.json` schema + `precompute_track` |
| Make export use a different codec         | `Processor.run` ffmpeg cmd               |
| Add a new sidebar setting                 | `_init_vars` (Tk var) + `_settings()` (include in dict) + `_build_sidebar` (slider/toggle) + use in `render_frame` or `precompute_track` |
| Replace PIL compositor with GPU one       | `build_chrome` + `paste_recording`; render_frame's per-frame ops |

---

## 14. Known gaps / future work

- **Real OS cursor capture** — what would let us show I-beam over
  text, hand over links, etc. Needs Win32 `GetCursorInfo` polled on
  a background thread, a `(HICON → PIL sprite)` cache, and a
  cursor-state track in the bundle. Sizable but well-scoped.
- **GPU compositor** — PIL is the perf floor right now. A
  `moderngl` or `pyglet` shader pass for chrome + paste would
  unlock 4K previews. Big refactor.
- **Cross-platform** — Recorder is Windows-only. macOS wants
  ScreenCaptureKit (Swift helper). Linux wants PipeWire/dmabuf.
- **Wallpaper presets** — UI ready (Wallpaper would be a 4th tab in
  the BACKGROUND segmented), needs curated PNGs.
- **Audio capture** — not in the bundle today; would need WASAPI
  loopback on Windows, then a parallel audio track in `raw.mkv` and
  the export `.mp4`.

---

## 15. Conventions

- `cx, cy` always refer to the cursor position in **source-pixel
  space** unless the variable name says otherwise. After a zoom
  remap they become `draw_sx, draw_sy` in **zoomed-frame space**.
- `sw, sh` are the source (capture region) dimensions; `cw, ch` the
  final canvas dimensions. `ow, oh` are the recording's size on the
  canvas; `rx, ry` its top-left on the canvas.
- Spring constants: `k` stiffness, `b` damping, `m` mass. We always
  pin damping to 0.92 of critical for the cursor smoother, so it
  reads as one degree of freedom (the `Smoothness` slider).
- The bundle is **append-only** during a recording. Mid-record
  failure should leave a partial-but-coherent bundle on disk.
