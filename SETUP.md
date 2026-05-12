# ScreenSee — Setup & Usage Guide

End-to-end instructions for taking a fresh Windows PC to a working
ScreenSee install. Linux/macOS notes are at the bottom.

> **Target platform:** Windows 10 (1903+) or Windows 11.
> **Python:** 3.10, 3.11, or 3.12.
> **Disk:** ~500 MB for Python + dependencies + FFmpeg.

---

## Step 1 — Install Python

1. Open <https://www.python.org/downloads/windows/> in a browser.
2. Download **Python 3.12.x** (or 3.11.x). Pick the **Windows installer
   (64-bit)**.
3. Run the installer.
4. **IMPORTANT:** on the first installer screen, tick
   **"Add python.exe to PATH"** at the bottom *before* clicking Install.
5. Choose **Install Now**.
6. When it finishes, close the installer.

Verify the install. Open a new **PowerShell** or **Command Prompt**
window (must be new — the existing ones don't see the new PATH) and run:

```powershell
python --version
pip --version
```

You should see something like `Python 3.12.4` and `pip 24.x …`. If
you get *"python is not recognized"*, the PATH checkbox didn't get
ticked. Re-run the installer and choose **Modify → Next → tick
"Add Python to environment variables" → Install**.

---

## Step 2 — Install FFmpeg

FFmpeg is the encoder ScreenSee pipes raw frames into. It must be on
your `PATH` so `subprocess.Popen("ffmpeg", …)` can find it.

1. Open <https://www.gyan.dev/ffmpeg/builds/> in a browser.
2. Under **release builds**, download
   **ffmpeg-release-essentials.zip**.
3. Extract the zip. You'll get a folder named something like
   `ffmpeg-7.0-essentials_build`. Inside is a `bin` folder containing
   `ffmpeg.exe`, `ffprobe.exe`, and `ffplay.exe`.
4. Move the whole extracted folder to a stable location, e.g.
   `C:\ffmpeg`. (Don't leave it in Downloads; if you delete that
   folder later FFmpeg goes with it.)
   The final path should look like `C:\ffmpeg\bin\ffmpeg.exe`.

Now add `C:\ffmpeg\bin` to your `PATH`:

1. Press **Win + S**, type **environment variables**, click
   **Edit the system environment variables**.
2. Click **Environment Variables…** at the bottom.
3. Under **User variables** (top half), select **Path** → **Edit…**.
4. Click **New** and paste `C:\ffmpeg\bin`.
5. **OK** → **OK** → **OK**. Close any open terminals.

Verify in a *new* terminal:

```powershell
ffmpeg -version
```

You should see a version banner. If you get *"ffmpeg is not
recognized"*, the PATH wasn't picked up — re-check the value, then
open a brand-new terminal.

---

## Step 3 — Get the ScreenSee source

```powershell
cd $HOME\Documents
git clone <repo-url> screenrec
cd screenrec
```

…or if you don't have git, download the repo as a ZIP from GitHub
and extract it to `C:\Users\<you>\Documents\screenrec`, then `cd`
into it.

---

## Step 4 — Create a virtual environment

Always use a venv. It keeps ScreenSee's dependencies isolated from
your system Python.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Your prompt will now show `(.venv)` at the front. If PowerShell
complains about *"running scripts is disabled"*, run this once
(elevated PowerShell):

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

…then retry the activate command.

> **CMD users:** activate with `.venv\Scripts\activate.bat` instead.

---

## Step 5 — Install Python dependencies

With the venv active:

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

This pulls in:

| Package           | What it does                                |
|-------------------|---------------------------------------------|
| `customtkinter`   | Dark, rounded Tk widgets for the UI         |
| `opencv-python`   | Frame decoding + zoom resampling            |
| `Pillow`          | Composition / cursor sprites / preview      |
| `numpy`           | Frame buffers                               |
| `pynput`          | Mouse + click event capture                 |
| `mss`             | Software screen capture (fallback)          |
| `windows-capture` | GPU screen capture, cursor exclusion (Win)  |

If `windows-capture` fails to install (older Windows builds
sometimes do), don't panic — ScreenSee detects that and falls back
to `mss`. Recording will still work; capture is just CPU-based.

---

## Step 6 — Run ScreenSee

Still in the activated venv, from the project root:

```powershell
python screensee.py
```

A window titled **ScreenSee** opens. You'll see:

- The product name and a big **Start Recording** button.
- A 30 / 60 fps toggle.
- Your display resolution.
- A **Capture chip** at the bottom telling you which backend will be
  used: green *"WGC (hardware, cursor excluded)"* if all is well,
  blue *"mss (software fallback, cursor excluded)"* if not. Both
  produce cursor-free frames on Windows.

---

## Using ScreenSee — typical flow

### 1. Record
- Click **Start Recording**. The button turns red and changes to
  **Stop Recording**. A timer ticks at the bottom.
- Switch to whatever you want to record (move the ScreenSee window
  off-screen or just Alt-Tab past it). Your mouse moves and clicks
  are being captured.
- Click the ScreenSee window's **Stop Recording** button when done.

If WGC fails silently you'll get a **Recording failed** dialog with
the workaround `pip uninstall windows-capture`. Otherwise the
welcome screen disappears and the editor opens.

### 2. Edit
The editor has three regions:

- **Preview pane (left):** scaled, composited live preview.
- **Transport bar (under preview):** Play/Pause, scrubber, time
  read-out.
- **Sidebar (right):** every adjustable setting, grouped into
  sections.

Sections:

| Section       | What it does                                              |
|---------------|-----------------------------------------------------------|
| **CANVAS**    | Output aspect ratio: Original / 16:9 / 1:1 / 4:3 / 9:16   |
| **BACKGROUND**| Tabs: **Gradient** swatches, **Color** swatches, **Image** picker. **Background blur** slider underneath. |
| **FRAME**     | Padding, Roundness, Shadow, Glass halo                    |
| **CURSOR**    | Cursor style picker (arrow / modern / triangle / hand / dot / ring), Cursor overlay toggle, Size, Smoothness, Auto-hide, Click ripples, Loop to start |
| **AUTO ZOOM** | Zoom on click toggle + zoom level                         |

Slider changes update the preview in real time (~40 ms debounce).
Chrome (background + shadow + glass) is cached, so dragging cursor
or zoom settings stays smooth.

### 3. Export
Click **Export MP4** at the top right.
- Choose a file path.
- A progress bar at the bottom of the preview pane shows
  encoder progress.
- A dialog confirms when it's done.

The exported file is an H.264 MP4 at the canvas's selected aspect
ratio, encoded at your recording fps with CRF 17 (visually
lossless).

### 4. New recording
Click **New Recording** at the top to discard the editor state and
return to the welcome screen.

---

## What ends up on disk

When you press Stop, ScreenSee writes a **bundle directory** to
your TEMP folder, named `screensee_<unix-time>.screensee/`:

```
screensee_1737000000.screensee/
├── raw.mkv          # cursor-free screen capture, H.264 CRF 18
├── events.json      # mouse + scroll events with timestamps
└── meta.json        # fps, region, frame_count, frame_times, backend
```

The MP4 export is whatever path you chose in the Export dialog —
the bundle stays in TEMP and is overwritten on the next recording.

You can `cd` into a bundle and inspect the JSONs to debug timing.

---

## Troubleshooting

### "ffmpeg not found on PATH"
You either skipped Step 2 or didn't open a new terminal after
updating PATH. Run `where ffmpeg` (PowerShell) or `which ffmpeg`
(Git Bash) — if it prints nothing, fix the PATH entry.

### "No frames were captured"
WGC failed at `start_free_threaded()`. The most common cause is
*"Toggling the capture border is not supported"* on older Windows
builds. Two fixes:
- Update Windows to the latest build (Win 11 22H2+ generally works), **or**
- Force the mss path: `pip uninstall windows-capture`

Then record again.

### Preview is choppy
With chrome caching the preview should hit 25–30 fps at 1080p. If
you see lower, check:
- You're not running Python from a Conda env with a slow Pillow
  build. Use the python.org Python.
- The recording resolution isn't unreasonably high (4K previews can
  be slow regardless).

### Cursor looks wrong / off-position
Some Windows DPI configurations report logical pixels in pynput
while we capture in physical pixels. Confirm by checking
`meta.json` — `region.width` and `region.height` should equal your
monitor's physical resolution. If they're scaled, file an issue
with the meta.json contents.

### Editor opens with black preview
Means the bundle's `raw.mkv` is malformed (encoder crashed). Look
for `ffmpeg` errors in the console where you launched ScreenSee.
Common cause: disk full on the TEMP drive.

---

## Linux / macOS notes

ScreenSee is Windows-first today. Cross-platform support is
*planned* but not finished:

- **Linux:** `mss` works for capture, `pynput` works for events,
  `ffmpeg` is available from your package manager. The main missing
  piece is the DPI / monitor enumeration code, which uses
  `ctypes.windll`. Patching that to fall back to `tkinter.winfo_*`
  would be a small change.
- **macOS:** `mss` works but is slow. The real plan here is a
  ScreenCaptureKit-based capture backend; would need a Swift helper
  binary. Not started yet.

If you want to try on Linux today, comment out the `windll` block
in `App.__init__` and let it use the 1920×1080 default. Most things
should work; expect missing-feature surprises.

---

## Updating

```powershell
cd C:\Users\<you>\Documents\screenrec
.\.venv\Scripts\Activate.ps1
git pull
pip install -r requirements.txt   # in case dependencies changed
python screensee.py
```

---

## Uninstalling

ScreenSee is just a Python script + a venv. To remove everything:

```powershell
Remove-Item -Recurse -Force C:\Users\<you>\Documents\screenrec
```

You may also want to clear leftover recording bundles from TEMP:

```powershell
Remove-Item -Recurse -Force $env:TEMP\screensee_*.screensee
```

FFmpeg and Python you can leave installed — they're useful for
other tools too.
