"""
ScreenSee v3.0 — FocuSee-inspired screen recorder
Modern UI built with CustomTkinter

v3 changes vs v2:
- Capture backend: windows-capture (cursor excluded) with mss fallback
- Streaming pipeline: frames go straight to ffmpeg, never accumulate in RAM
- Single monotonic clock (perf_counter) shared by frames and input events
- Recordings persist as a .screensee bundle (raw.mkv + events.json + meta.json)
- Export is now a pure function of (bundle, settings); preview reads bundle on demand
"""

import os, sys, time, math, json, tempfile, threading, subprocess
import tkinter as tk
import customtkinter as ctk
from tkinter import messagebox, filedialog
import numpy as np

try:
    import cv2
    from PIL import Image, ImageDraw, ImageFilter, ImageTk
    import mss
    from pynput import mouse as pmouse
    HAS_DEPS = True
except ImportError as e:
    HAS_DEPS = False
    MISSING  = str(e)

# Optional: Windows.Graphics.Capture for true cursor exclusion.
# Falls back to mss (cursor will appear in raw footage) if not installed.
try:
    from windows_capture import WindowsCapture, Frame, InternalCaptureControl
    HAS_WGC = True
except ImportError:
    HAS_WGC = False

# ── Theme
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

VERSION  = "3.0"
APP_NAME = "ScreenSee"

CANVAS_PRESETS = {
    "Original": None,
    "16:9":     (1920, 1080),
    "1:1":      (1080, 1080),
    "4:3":      (1440, 1080),
    "9:16":     (1080, 1920),
}

GRADIENTS = [
    ("#0f0c29", "#302b63"),
    ("#1a1a2e", "#16213e"),
    ("#0f3460", "#533483"),
    ("#e94560", "#0f3460"),
    ("#11998e", "#38ef7d"),
    ("#c94b4b", "#4b134f"),
    ("#2980b9", "#6dd5fa"),
    ("#834d9b", "#d04ed6"),
    ("#fc4a1a", "#f7b733"),
    ("#43cea2", "#185a9d"),
    ("#ee0979", "#ff6a00"),
    ("#373b44", "#4286f4"),
]

SOLIDS = [
    "#1e1e2e", "#2d2d2d", "#0f172a", "#18181b",
    "#6366f1", "#8b5cf6", "#ec4899", "#ef4444",
    "#f97316", "#eab308", "#22c55e", "#14b8a6",
]


# ============================================================
#  MASS-SPRING-DAMPER  — FocuSee's exact physics algorithm
#
#  Reverse-engineered from VidHacker_Common.cs:
#    mouseMovementSpring: stiffness=470, damping=70,  mass=3
#    screenMovementSpring: stiffness=170, damping=50, mass=3
#
#  Formula (per timestep dt):
#    acceleration = (target - position) * stiffness / mass
#                 - velocity * damping / mass
#    velocity    += acceleration * dt
#    position    += velocity * dt
#
#  The mass gives it proper inertia — cursor overshoots slightly
#  then settles. This is exactly what makes FocuSee feel different
#  from a simple spring (no inertia) or 1€ filter (frequency-based).
# ============================================================
class MassSpringDamper:
    def __init__(self, stiffness=470.0, damping=70.0, mass=3.0, fps=30.0):
        self.k    = stiffness
        self.b    = damping
        self.m    = mass
        self.dt   = 1.0 / fps
        self.px = self.py = 0.0   # position
        self.vx = self.vy = 0.0   # velocity
        self._init = False

    def update(self, tx, ty, t=None):
        if not self._init:
            self.px, self.py = tx, ty
            self._init = True
            return tx, ty
        # acceleration = (target-pos)*k/m  -  vel*b/m
        ax = (tx - self.px) * self.k / self.m - self.vx * self.b / self.m
        ay = (ty - self.py) * self.k / self.m - self.vy * self.b / self.m
        self.vx += ax * self.dt
        self.vy += ay * self.dt
        self.px += self.vx * self.dt
        self.py += self.vy * self.dt
        return self.px, self.py

    def reset(self):
        self._init = False
        self.vx = self.vy = 0.0


# Keep SpringSmoother as alias for backward compat with UI
class SpringSmoother(MassSpringDamper):
    def __init__(self, stiffness=0.12, damping=0.75, fps=30.0):
        # Map 0-1 UI sliders → FocuSee physics ranges
        # stiffness: 0.05→100, 0.5→600  (maps to k)
        # damping:   0.5→20,  1.0→90   (maps to b)
        k = stiffness * 1200.0   # 0.12 → 144, feels close to FocuSee 470 at max
        b = damping   * 90.0     # 0.75 → 67.5, close to FocuSee 70 default
        super().__init__(stiffness=k, damping=b, mass=3.0, fps=fps)
        self.cx = self.cy = 0.0

    def update(self, tx, ty, t=None):
        self.cx, self.cy = super().update(tx, ty, t)
        return self.cx, self.cy

# Keep OneEuroFilter for reference
class _LowPass:
    def __init__(self):
        self.y = None
    def filter(self, value, alpha):
        if self.y is None: self.y = value
        self.y = alpha * value + (1.0 - alpha) * self.y
        return self.y

class OneEuroFilter:
    def __init__(self, freq=30.0, min_cutoff=0.8, beta=0.005, d_cutoff=1.0):
        self.freq=freq; self.min_cutoff=min_cutoff; self.beta=beta; self.d_cutoff=d_cutoff
        self._x=_LowPass(); self._dx=_LowPass(); self._last_t=None
    def _alpha(self, cutoff):
        te=1.0/max(self.freq,1.0); tau=1.0/(2.0*math.pi*cutoff)
        return 1.0/(1.0+tau/te)
    def filter(self, value, t=None):
        if self._last_t and t:
            self.freq=1.0/max(0.001,t-self._last_t)
        self._last_t=t
        prev=self._x.y if self._x.y is not None else value
        dv=(value-prev)*self.freq if self._x.y is not None else 0.0
        edx=self._dx.filter(dv,self._alpha(self.d_cutoff))
        cutoff=self.min_cutoff+self.beta*abs(edx)
        return self._x.filter(value,self._alpha(cutoff))
    def reset(self):
        self._x=_LowPass(); self._dx=_LowPass(); self._last_t=None



# ============================================================
#  ZOOM ENGINE  — cubic ease-in/out transitions
#  Uses timed animation segments instead of spring physics.
#  Fast zoom-in, holds, slow ease-out — matches FocuSee feel.
# ============================================================
class ZoomEngine:
    def __init__(self, fps=30.0):
        self.fps     = fps
        self.zoom    = 1.0
        self.cx      = 0.5
        self.cy      = 0.5
        # Current animation state
        self._anim   = None   # dict: start_zoom,end_zoom,start_cx,end_cx,...,t0,dur
        self._target_zoom = 1.0
        self._target_cx   = 0.5
        self._target_cy   = 0.5
        self._hold   = False

    @staticmethod
    def _ease_in_out_cubic(t):
        """Smooth cubic ease in/out — matches FocuSee's zoom feel"""
        t = max(0.0, min(1.0, t))
        if t < 0.5:
            return 4 * t * t * t
        else:
            return 1 - (-2*t + 2)**3 / 2

    @staticmethod
    def _ease_out_cubic(t):
        t = max(0.0, min(1.0, t))
        return 1 - (1 - t)**3

    @staticmethod
    def _ease_in_cubic(t):
        t = max(0.0, min(1.0, t))
        return t * t * t

    def trigger(self, nx, ny, z=2.0, t=None, in_dur=0.25):
        """Zoom in — only starts new animation if target changed"""
        if (self._target_zoom == z and
            abs(self._target_cx - nx) < 0.01 and
            abs(self._target_cy - ny) < 0.01):
            return  # already animating to this target
        self._anim = {
            "sz": self.zoom, "ez": z,
            "scx": self.cx, "ecx": nx,
            "scy": self.cy, "ecy": ny,
            "t0": t or time.time(),
            "dur": in_dur,
            "ease": self._ease_out_cubic
        }
        self._target_zoom = z
        self._target_cx   = nx
        self._target_cy   = ny

    def release(self, t=None, out_dur=0.35):
        """Zoom out — only starts new animation if not already zooming out"""
        if self._target_zoom <= 1.02:
            return  # already released or releasing
        self._anim = {
            "sz": self.zoom, "ez": 1.0,
            "scx": self.cx, "ecx": 0.5,
            "scy": self.cy, "ecy": 0.5,
            "t0": t or time.time(),
            "dur": out_dur,
            "ease": self._ease_in_out_cubic
        }
        self._target_zoom = 1.0
        self._target_cx   = 0.5
        self._target_cy   = 0.5

    def update(self, t=None):
        now = t or time.time()
        if self._anim:
            a   = self._anim
            u   = (now - a["t0"]) / max(0.001, a["dur"])
            eu  = a["ease"](u)
            self.zoom = a["sz"] + (a["ez"] - a["sz"]) * eu
            self.cx   = a["scx"] + (a["ecx"] - a["scx"]) * eu
            self.cy   = a["scy"] + (a["ecy"] - a["scy"]) * eu
            if u >= 1.0:
                self.zoom = a["ez"]; self.cx = a["ecx"]; self.cy = a["ecy"]
                self._anim = None
        return self.zoom, self.cx, self.cy

    def apply(self, frame):
        z = self.zoom
        if z <= 1.02:
            return frame
        h, w = frame.shape[:2]
        rw, rh = int(w/z), int(h/z)
        x1 = max(0, min(w-rw, int(self.cx*w - rw/2)))
        y1 = max(0, min(h-rh, int(self.cy*h - rh/2)))
        return cv2.resize(frame[y1:y1+rh, x1:x1+rw], (w,h),
                          interpolation=cv2.INTER_LINEAR)

    def reset(self):
        self.zoom=1.0; self.cx=0.5; self.cy=0.5
        self._anim=None; self._target_zoom=1.0



# ============================================================
#  CURSOR DRAWING
# ============================================================
ARROW_PTS = [(0,0),(0,28),(7,21),(12,32),(16,30),(11,19),(20,19)]
ARROW_SHADOW = [(x+2,y+3) for x,y in ARROW_PTS]

def draw_cursor(img_rgba, cx, cy, size=32, opacity=1.0, tilt=0.0):
    scale = size / 32.0
    scaled   = [(int(x*scale), int(y*scale)) for x,y in ARROW_PTS]
    shadow_p = [(int(x*scale), int(y*scale)) for x,y in ARROW_SHADOW]
    cur = Image.new("RGBA", (int(64*scale), int(64*scale)), (0,0,0,0))
    d   = ImageDraw.Draw(cur)
    d.polygon(shadow_p, fill=(0,0,0,int(60*opacity)))
    d.polygon(scaled,   fill=(255,255,255,int(255*opacity)))
    d.line([scaled[i] for i in range(len(scaled))] + [scaled[0]],
           fill=(0,0,0,int(200*opacity)), width=max(1,int(1.5*scale)))
    if abs(tilt) > 0.5:
        cur = cur.rotate(-tilt, resample=Image.BICUBIC, expand=False)
    ix, iy = int(cx)-2, int(cy)-2
    img_rgba.paste(cur, (ix, iy), cur)


# ============================================================
#  RIPPLE
# ============================================================
class Ripple:
    __slots__ = ["x","y","t","dur","color"]
    def __init__(self, x, y, color=(100,150,255,180), dur=0.4):
        self.x=x; self.y=y; self.t=time.time()
        self.dur=dur; self.color=color

    def draw(self, img_rgba, now):
        age  = now - self.t
        if age >= self.dur: return False
        prog  = age / self.dur
        ease  = 1-(1-prog)**2
        r     = int(ease * 55)
        alpha = int((1-prog) * 180)
        d = ImageDraw.Draw(img_rgba)
        c = self.color[:3] + (alpha,)
        x,y = int(self.x), int(self.y)
        d.ellipse([x-r,y-r,x+r,y+r], outline=c, width=2)
        return True


# ============================================================
#  BACKGROUND COMPOSITOR
# ============================================================
def make_canvas(cw, ch, bg_type, bg_val,
                padding, inset, roundness, shadow):
    canvas = Image.new("RGBA", (cw,ch), (20,20,30,255))
    d = ImageDraw.Draw(canvas)

    if bg_type == "gradient":
        c1 = tuple(int(bg_val[0][i:i+2],16) for i in (1,3,5))
        c2 = tuple(int(bg_val[1][i:i+2],16) for i in (1,3,5))
        for y in range(ch):
            t = y/ch
            r=int(c1[0]*(1-t)+c2[0]*t)
            g=int(c1[1]*(1-t)+c2[1]*t)
            b=int(c1[2]*(1-t)+c2[2]*t)
            d.line([(0,y),(cw,y)], fill=(r,g,b,255))
    elif bg_type == "solid":
        c = tuple(int(bg_val[i:i+2],16) for i in (1,3,5))
        d.rectangle([0,0,cw,ch], fill=c+(255,))
    return canvas


def composite(rec_pil, cw, ch, bg_type, bg_val,
              padding, inset, roundness, shadow):
    rw, rh = rec_pil.size
    avail_w = cw - padding*2
    avail_h = ch - padding*2
    sc  = min(avail_w/rw, avail_h/rh)
    ow  = int(rw*sc); oh = int(rh*sc)
    rx  = (cw-ow)//2;  ry = (ch-oh)//2

    canvas = make_canvas(cw, ch, bg_type, bg_val,
                          padding, inset, roundness, shadow)

    # Shadow
    if shadow > 0:
        sl = Image.new("RGBA",(cw,ch),(0,0,0,0))
        sd = ImageDraw.Draw(sl)
        off = max(1, shadow//8)
        alp = min(200, shadow*2)
        sd.rounded_rectangle(
            [rx+off,ry+off,rx+ow+off,ry+oh+off],
            radius=roundness, fill=(0,0,0,alp))
        sl = sl.filter(ImageFilter.GaussianBlur(max(2,shadow//6)))
        canvas = Image.alpha_composite(canvas, sl)

    rec_r = rec_pil.convert("RGBA").resize((ow,oh), Image.LANCZOS)

    # Rounded mask first (clip recording to rounded corners)
    if roundness > 0:
        mask = Image.new("L",(ow,oh),0)
        ImageDraw.Draw(mask).rounded_rectangle([0,0,ow-1,oh-1],
                                               radius=roundness, fill=255)
        rec_r.putalpha(mask)

    # Inset border — drawn ON TOP of recording, inside rounded corners
    # Like FocuSee's inset: a subtle bright inner stroke
    if inset > 0:
        border_layer = Image.new("RGBA",(ow,oh),(0,0,0,0))
        bd = ImageDraw.Draw(border_layer)
        bd.rounded_rectangle([0, 0, ow-1, oh-1],
                              radius=roundness,
                              outline=(255,255,255,180),
                              width=max(1, inset))
        rec_r = Image.alpha_composite(rec_r, border_layer)

    canvas.paste(rec_r,(rx,ry),rec_r)
    return canvas, rx, ry, ow, oh, sc


# ============================================================
#  RECORDER
#
#  v3 pipeline:
#    capture backend ──┐
#                      ├──► ffmpeg stdin (BGRA raw) ──► raw.mkv
#    pynput listener ──┘                              + events.json
#                                                     + meta.json
#
#  All timestamps are seconds-since-record-start using perf_counter,
#  so frame PTS and input events share one clock.
# ============================================================
class Recorder:
    def __init__(self, fps=30):
        self.fps         = fps
        self.bundle_path = None
        self.running     = False
        self._events     = []           # only input events live in RAM
        self._frame_idx  = 0
        self._last_write = -1.0
        self._region     = None
        self._t0         = 0.0
        self._lock       = threading.Lock()
        self._listener   = None
        self._ffmpeg     = None
        self._capture    = None         # windows-capture instance
        self._cap_thread = None         # mss fallback thread

    def _now(self):
        return time.perf_counter() - self._t0

    # ── Input event callbacks ──────────────────────────────────
    def _on_move(self, x, y):
        if self.running:
            with self._lock:
                self._events.append((self._now(), "MOVE", x, y))

    def _on_click(self, x, y, button, pressed):
        if self.running:
            t = "CLICK" if pressed else "RELEASE"
            b = "L" if button == pmouse.Button.left else "R"
            with self._lock:
                self._events.append((self._now(), f"{t}_{b}", x, y))

    def _on_scroll(self, x, y, dx, dy):
        if self.running:
            d = "UP" if dy > 0 else "DOWN"
            with self._lock:
                self._events.append((self._now(), f"SCROLL_{d}", x, y))

    # ── Frame write (called from capture backend) ──────────────
    def _write_frame(self, bgra):
        # Decimate to target fps; capture backend may deliver at monitor refresh.
        now = self._now()
        if now - self._last_write < (1.0 / self.fps) - 0.001:
            return
        self._last_write = now
        try:
            self._ffmpeg.stdin.write(bgra.tobytes())
            self._frame_idx += 1
        except (BrokenPipeError, OSError, ValueError):
            self.running = False

    # ── Backend 1: Windows.Graphics.Capture (cursor excluded) ──
    def _start_wgc(self, region):
        cap = WindowsCapture(
            cursor_capture=False,
            draw_border=False,
            monitor_index=1,
            window_name=None,
        )
        l, t, w, h = region["left"], region["top"], region["width"], region["height"]

        @cap.event
        def on_frame_arrived(frame: "Frame", ctrl: "InternalCaptureControl"):
            if not self.running:
                ctrl.stop(); return
            buf = frame.frame_buffer  # HxWx4 BGRA
            if (buf.shape[1], buf.shape[0]) != (w, h):
                buf = buf[t:t+h, l:l+w]
            self._write_frame(buf)

        @cap.event
        def on_closed():
            pass

        cap.start_free_threaded()
        self._capture = cap

    # ── Backend 2: mss fallback (cursor will be in footage) ────
    def _start_mss(self, region):
        def loop():
            iv = 1.0 / self.fps
            with mss.mss() as sct:
                while self.running:
                    t_loop = time.perf_counter()
                    img = np.asarray(sct.grab(region))  # BGRA
                    self._write_frame(img)
                    rem = iv - (time.perf_counter() - t_loop)
                    if rem > 0:
                        time.sleep(rem)
        self._cap_thread = threading.Thread(target=loop, daemon=True)
        self._cap_thread.start()

    # ── Public API ─────────────────────────────────────────────
    def start(self, region, bundle_dir):
        self.bundle_path = bundle_dir
        os.makedirs(bundle_dir, exist_ok=True)
        raw_path = os.path.join(bundle_dir, "raw.mkv")
        w, h = region["width"], region["height"]

        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{w}x{h}", "-pix_fmt", "bgra",
            "-r", str(self.fps), "-i", "pipe:0",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
            "-pix_fmt", "yuv420p", raw_path,
        ]
        try:
            self._ffmpeg = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            raise RuntimeError("ffmpeg not found on PATH")

        self._events.clear()
        self._frame_idx = 0
        self._last_write = -1.0
        self._region = region
        self._t0 = time.perf_counter()
        self.running = True

        if HAS_WGC:
            self._start_wgc(region)
        else:
            self._start_mss(region)

        self._listener = pmouse.Listener(
            on_move=self._on_move,
            on_click=self._on_click,
            on_scroll=self._on_scroll)
        self._listener.start()

    def stop(self):
        self.running = False
        if self._listener:
            try: self._listener.stop()
            except Exception: pass
        if self._capture:
            try: self._capture.stop()
            except Exception: pass
        if self._cap_thread:
            self._cap_thread.join(timeout=2.0)
        if self._ffmpeg:
            try: self._ffmpeg.stdin.close()
            except Exception: pass
            try: self._ffmpeg.wait(timeout=15)
            except subprocess.TimeoutExpired: self._ffmpeg.kill()

        # Persist event + meta tracks alongside raw.mkv
        with self._lock:
            events_out = [
                {"t": t, "type": et, "x": x, "y": y}
                for (t, et, x, y) in self._events
            ]
        with open(os.path.join(self.bundle_path, "events.json"), "w") as f:
            json.dump(events_out, f)
        with open(os.path.join(self.bundle_path, "meta.json"), "w") as f:
            json.dump({
                "version": VERSION,
                "fps": self.fps,
                "region": self._region,
                "frame_count": self._frame_idx,
                "duration": self._now(),
                "cursor_excluded": HAS_WGC,
                "backend": "windows-capture" if HAS_WGC else "mss",
            }, f, indent=2)
        return self.bundle_path


# ============================================================
#  PROCESSOR
# ============================================================
class Processor:
    def __init__(self, settings, progress_cb=None):
        self.s  = settings
        self.cb = progress_cb

    def prog(self, pct, msg):
        if self.cb: self.cb(pct, msg)

    def run(self, bundle_dir, out_path):
        # Load bundle
        try:
            with open(os.path.join(bundle_dir, "meta.json")) as f:
                meta = json.load(f)
            with open(os.path.join(bundle_dir, "events.json")) as f:
                events_raw = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            self.prog(0, f"Bad bundle: {e}"); return False

        raw_path = os.path.join(bundle_dir, "raw.mkv")
        src = cv2.VideoCapture(raw_path)
        if not src.isOpened():
            self.prog(0, "Failed to open raw.mkv"); return False

        # Event dicts → (t, type, x, y) tuples for downstream code
        events = [(e["t"], e["type"], e["x"], e["y"]) for e in events_raw]

        s   = self.s
        fps = meta.get("fps", s["fps"])
        reg = meta.get("region") or s["region"]
        sw, sh = reg["width"], reg["height"]

        preset = s["canvas_preset"]
        if preset and preset != "Original":
            cw, ch = CANVAS_PRESETS[preset]
        else:
            p = s["padding"]
            cw = sw + p*2 + 60
            ch = sh + p*2 + 60

        # Build zoom windows from clicks
        self.prog(2, "Analysing clicks...")
        clicks = [(t,x-reg["left"],y-reg["top"])
                  for (t,et,x,y) in events
                  if et == "CLICK_L"]
        zoom_wins = []
        if s["auto_zoom"]:
            for i,(t,cx,cy) in enumerate(clicks):
                end = t + s["zoom_dur"]
                if i+1 < len(clicks):
                    end = min(end, clicks[i+1][0]-0.05)
                zoom_wins.append({
                    "s": t-0.05, "e": end,
                    "nx": cx/sw,  "ny": cy/sh,
                    "z":  s["zoom_level"]
                })

        # FFmpeg
        self.prog(5, "Opening encoder...")
        cmd = [
            "ffmpeg","-y",
            "-f","rawvideo","-vcodec","rawvideo",
            "-s",f"{cw}x{ch}","-pix_fmt","bgr24",
            "-r",str(fps),"-i","pipe:0",
            "-c:v","libx264","-preset","fast",
            "-crf","17","-pix_fmt","yuv420p",
            out_path
        ]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            self.prog(0,"FFmpeg not found!"); return False

        # Use FocuSee's exact physics defaults
        # mouseMovementSpring: stiffness=470, damping=70, mass=3
        fps_val = s["fps"]
        smoother = MassSpringDamper(
            stiffness = s["stiffness"] * 1200.0,  # 0.12 slider → ~144 (feels like FocuSee at ~0.4)
            damping   = s["damping"]   * 90.0,    # 0.75 slider → ~67 (FocuSee default is 70)
            mass      = 3.0,
            fps       = fps_val
        )
        zoom_eng = ZoomEngine()
        smoother.reset(); zoom_eng.reset()

        ripples  = []
        used_rip = set()
        sorted_ev = sorted(events, key=lambda e:e[0])
        ev_i     = 0
        cur_x, cur_y = sw//2, sh//2
        n = meta.get("frame_count") or int(src.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        cur_opacity = 1.0
        idle_frames = 0

        self.prog(8, "Processing frames...")

        fi = 0
        while True:
            ok, frame = src.read()
            if not ok: break
            tf = fi / fps   # frame time relative to record start (shared clock with events)

            # Advance events
            while ev_i < len(sorted_ev) and sorted_ev[ev_i][0] <= tf:
                ev = sorted_ev[ev_i]
                et = ev[1]
                ex = ev[2]-reg["left"]
                ey = ev[3]-reg["top"]
                if et == "MOVE":
                    cur_x, cur_y = ex, ey
                elif et == "CLICK_L" and ev[0] not in used_rip:
                    used_rip.add(ev[0])
                    if s["click_ripple"]:
                        ripples.append(Ripple(ex,ey,(100,150,255,180)))
                ev_i += 1

            # Smooth cursor — mass-spring-damper physics
            sx, sy = smoother.update(cur_x, cur_y, t=tf)

            # Tilt — use physics velocity directly
            tilt = 0.0
            if s["cursor_tilt"]:
                spd = math.sqrt(smoother.vx**2 + smoother.vy**2)
                if spd > 0.5:
                    tilt = math.atan2(smoother.vy, smoother.vx)*(180/math.pi)*0.15

            # Auto-hide — cursor speed from physics velocity
            if s["auto_hide"]:
                spd = math.sqrt(smoother.vx**2 + smoother.vy**2)
                if spd < 0.5:
                    idle_frames += 1
                    if idle_frames > fps_val * 1.5:
                        cur_opacity = max(0.0, cur_opacity - 0.08)
                else:
                    idle_frames = 0
                    cur_opacity = min(1.0, cur_opacity + 0.25)
            else:
                cur_opacity = 1.0

            # Zoom — trigger/release with timestamps for cubic easing
            active = next((z for z in zoom_wins
                           if z["s"]<=tf<=z["e"]), None)
            if active:
                zoom_eng.trigger(active["nx"], active["ny"],
                                 active["z"], t=tf)
            else:
                zoom_eng.release(t=tf)
            z, zcx, zcy = zoom_eng.update(t=tf)

            # Apply zoom to frame
            zoomed = zoom_eng.apply(frame)

            # Convert to PIL
            img = Image.fromarray(cv2.cvtColor(zoomed, cv2.COLOR_BGR2RGBA))

            # ── Remap cursor position to zoomed frame space
            # When zoomed, visible region is a sub-rect of the full frame.
            # Cursor must be mapped from screen space → zoomed frame space.
            draw_sx, draw_sy = sx, sy
            if z > 1.02:
                rw_z = sw / z
                rh_z = sh / z
                x1_z = max(0, min(sw - rw_z, zoom_eng.cx * sw - rw_z / 2))
                y1_z = max(0, min(sh - rh_z, zoom_eng.cy * sh - rh_z / 2))
                draw_sx = (sx - x1_z) * z
                draw_sy = (sy - y1_z) * z

            # Draw cursor at remapped position
            if cur_opacity > 0.02:
                if fi == 0:
                    print(f"[DEBUG] Frame 0 cursor at ({draw_sx:.0f},{draw_sy:.0f}), zoom={z:.2f}, opacity={cur_opacity:.2f}")
                draw_cursor(img, draw_sx, draw_sy,
                            size=s["cursor_size"],
                            opacity=cur_opacity,
                            tilt=tilt)

            # Draw ripples — also remap positions when zoomed
            now = tf
            alive = []
            for r in ripples:
                rx_draw, ry_draw = r.x, r.y
                if z > 1.02:
                    rw_z = sw / z
                    rh_z = sh / z
                    x1_z = max(0, min(sw - rw_z, zoom_eng.cx * sw - rw_z / 2))
                    y1_z = max(0, min(sh - rh_z, zoom_eng.cy * sh - rh_z / 2))
                    rx_draw = (r.x - x1_z) * z
                    ry_draw = (r.y - y1_z) * z
                # Temporarily remap for drawing
                orig_x, orig_y = r.x, r.y
                r.x, r.y = rx_draw, ry_draw
                if r.draw(img, now):
                    alive.append(r)
                    r.x, r.y = orig_x, orig_y  # restore real coords
                else:
                    pass  # expired
            ripples = alive

            # Composite
            canvas,*_ = composite(
                img, cw, ch,
                s["bg_type"], s["bg_val"],
                s["padding"], s["inset"],
                s["roundness"], s["shadow"])

            bgr = cv2.cvtColor(np.array(canvas), cv2.COLOR_RGBA2BGR)
            try:
                proc.stdin.write(bgr.tobytes())
            except BrokenPipeError:
                break

            if fi%15==0:
                self.prog(8+int((fi/n)*87),
                          f"Frame {fi+1}/{n}  ({int(fi/n*100)}%)")
            fi += 1

        src.release()
        proc.stdin.close()
        proc.wait()
        self.prog(100,"Done!")
        return True


# ============================================================
#  UI
# ============================================================
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME}  v{VERSION}")
        self.geometry("460x900")
        self.resizable(False, True)
        self.configure(fg_color="#0d1117")

        self.recorder = Recorder()
        self.recording = False
        self.bundle_path = None
        self.bg_type = "gradient"
        self.bg_val  = GRADIENTS[0]
        self._rec_start = 0.0

        # Screen size
        try:
            import ctypes as _c
            _c.windll.shcore.SetProcessDpiAwareness(2)
            u = _c.windll.user32
            self.sw = u.GetSystemMetrics(0)
            self.sh = u.GetSystemMetrics(1)
        except Exception:
            self.sw, self.sh = 1920, 1080

        if not HAS_DEPS:
            ctk.CTkLabel(self, text=f"Missing deps: {MISSING}",
                         text_color="#ef4444").pack(pady=40)
            return

        self._build()

    # ── helpers
    def _label(self, parent, text, size=12, color="#94a3b8", bold=False):
        return ctk.CTkLabel(parent, text=text,
                            font=ctk.CTkFont("Segoe UI", size,
                                             weight="bold" if bold else "normal"),
                            text_color=color)

    def _section(self, parent, title):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(fill="x", padx=16, pady=(14,2))
        self._label(f, title, 10, "#475569", bold=True).pack(side="left")
        ctk.CTkFrame(f, height=1, fg_color="#1e2d4a").pack(
            side="left", fill="x", expand=True, padx=(8,0))

    def _slider(self, parent, label, attr, lo, hi, default,
                fmt=None, row_pad=(4,0)):
        if fmt is None:
            fmt = lambda v: str(int(float(v)))
        var = ctk.DoubleVar(value=default)
        setattr(self, attr, var)
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=row_pad)
        self._label(row, label, 11, "#94a3b8").pack(side="left")
        val_lbl = self._label(row, fmt(default), 11, "#6366f1", bold=True)
        val_lbl.pack(side="right")
        sl = ctk.CTkSlider(row, from_=lo, to=hi,
                           variable=var, width=200,
                           button_color="#6366f1",
                           button_hover_color="#8b5cf6",
                           progress_color="#6366f1",
                           command=lambda v, l=val_lbl, f=fmt: l.configure(text=f(v)))
        sl.pack(side="right", padx=(0,8))

    def _toggle(self, parent, label, attr, default=True):
        var = ctk.BooleanVar(value=default)
        setattr(self, attr, var)
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(4,0))
        self._label(row, label, 11, "#94a3b8").pack(side="left")
        ctk.CTkSwitch(row, variable=var, text="",
                      width=40, height=20,
                      button_color="#6366f1",
                      button_hover_color="#8b5cf6",
                      progress_color="#6366f1"
                      ).pack(side="right")

    # ── main build
    def _build(self):
        # Header
        hdr = ctk.CTkFrame(self, fg_color="#0d1117", height=80, corner_radius=0)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text=APP_NAME,
                     font=ctk.CTkFont("Segoe UI", 28, weight="bold"),
                     text_color="#ffffff").pack(pady=(16,0))
        ctk.CTkLabel(hdr, text=f"Screen: {self.sw} × {self.sh}",
                     font=ctk.CTkFont("Segoe UI", 10),
                     text_color="#475569").pack()

        # Scrollable settings
        scroll = ctk.CTkScrollableFrame(self, fg_color="#0d1117",
                                         scrollbar_button_color="#1e2d4a",
                                         scrollbar_button_hover_color="#6366f1")
        scroll.pack(fill="both", expand=True, padx=0, pady=0)

        # ── CANVAS SIZE
        self._section(scroll, "CANVAS SIZE")
        cp = ctk.CTkFrame(scroll, fg_color="transparent")
        cp.pack(fill="x", padx=16, pady=(4,0))
        self.canvas_var = ctk.StringVar(value="Original")
        self._preset_btns = {}
        for preset in ["Original","16:9","1:1","4:3","9:16"]:
            is_sel = (preset == "Original")
            b = ctk.CTkButton(cp, text=preset, width=68, height=30,
                              font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                              fg_color="#6366f1" if is_sel else "#1e2d4a",
                              hover_color="#8b5cf6",
                              corner_radius=6,
                              command=lambda p=preset: self._set_preset(p))
            b.pack(side="left", padx=3)
            self._preset_btns[preset] = b

        # ── STYLE
        self._section(scroll, "STYLE")
        self._toggle(scroll, "Fixed Zoom Part", "fixed_zoom_var", True)
        self._slider(scroll, "Padding",   "padding_var",   0, 120, 30)
        self._slider(scroll, "Inset",     "inset_var",     0, 20,  0)
        self._slider(scroll, "Roundness", "roundness_var", 0, 40,  8)
        self._slider(scroll, "Shadow",    "shadow_var",    0, 100, 60)

        # ── BACKGROUND
        self._section(scroll, "BACKGROUND")
        ctk.CTkLabel(scroll, text="Gradients",
                     font=ctk.CTkFont("Segoe UI",10),
                     text_color="#475569").pack(anchor="w", padx=16, pady=(6,2))

        # Gradient grid
        gg = ctk.CTkFrame(scroll, fg_color="transparent")
        gg.pack(fill="x", padx=16)
        self._bg_btns = []
        for i,(c1,c2) in enumerate(GRADIENTS):
            btn = tk.Canvas(gg, width=40, height=26,
                             highlightthickness=2,
                             highlightbackground="#6366f1" if i==0 else "#1e2d4a",
                             cursor="hand2")
            btn.grid(row=i//6, column=i%6, padx=3, pady=3)
            for x in range(40):
                t = x/40
                r=int(int(c1[1:3],16)*(1-t)+int(c2[1:3],16)*t)
                g=int(int(c1[3:5],16)*(1-t)+int(c2[3:5],16)*t)
                b=int(int(c1[5:7],16)*(1-t)+int(c2[5:7],16)*t)
                btn.create_line(x,0,x,26, fill=f"#{r:02x}{g:02x}{b:02x}")
            btn.bind("<Button-1>", lambda e,idx=i,b=btn: self._set_bg_grad(idx,b))
            self._bg_btns.append(btn)

        ctk.CTkLabel(scroll, text="Solid Colors",
                     font=ctk.CTkFont("Segoe UI",10),
                     text_color="#475569").pack(anchor="w", padx=16, pady=(10,2))
        sg = ctk.CTkFrame(scroll, fg_color="transparent")
        sg.pack(fill="x", padx=16, pady=(0,4))
        for i,col in enumerate(SOLIDS):
            btn = tk.Canvas(sg, width=32, height=22, bg=col,
                             highlightthickness=2,
                             highlightbackground="#1e2d4a",
                             cursor="hand2")
            btn.grid(row=i//8, column=i%8, padx=3, pady=3)
            btn.bind("<Button-1>", lambda e,c=col,b=btn: self._set_bg_solid(c,b))
            self._bg_btns.append(btn)

        # ── CURSOR
        self._section(scroll, "CURSOR")
        self._slider(scroll, "Size",      "cursor_size_var",  16, 64, 32)
        self._slider(scroll, "Stiffness", "stiffness_var",    0.05, 0.5, 0.12,
                     fmt=lambda v: f"{float(v):.2f}")
        self._slider(scroll, "Damping",   "damping_var",      0.5, 1.0, 0.75,
                     fmt=lambda v: f"{float(v):.2f}")
        self._toggle(scroll, "Cursor Tilt",    "tilt_var",     True)
        self._toggle(scroll, "Click Ripples",  "ripple_var",   True)
        self._toggle(scroll, "Auto-hide Idle", "autohide_var", True)

        # ── AUTO ZOOM
        self._section(scroll, "AUTO ZOOM")
        self._toggle(scroll, "Auto Zoom on Click", "autozoom_var", True)
        self._slider(scroll, "Zoom Level",    "zoomlevel_var",  1.2, 3.0, 2.0,
                     fmt=lambda v: f"{float(v):.1f}×")
        self._slider(scroll, "Zoom Duration", "zoomdur_var",    0.5, 4.0, 1.5,
                     fmt=lambda v: f"{float(v):.1f}s")

        # ── EXPORT
        self._section(scroll, "EXPORT")
        fps_row = ctk.CTkFrame(scroll, fg_color="transparent")
        fps_row.pack(fill="x", padx=16, pady=(4,8))
        ctk.CTkLabel(fps_row, text="FPS",
                     font=ctk.CTkFont("Segoe UI",11),
                     text_color="#94a3b8").pack(side="left")
        self.fps_var = ctk.StringVar(value="30")
        for f in ["24","30","60"]:
            ctk.CTkRadioButton(fps_row, text=f,
                               variable=self.fps_var, value=f,
                               radiobutton_width=16, radiobutton_height=16,
                               fg_color="#6366f1",
                               hover_color="#8b5cf6",
                               font=ctk.CTkFont("Segoe UI",11),
                               text_color="#94a3b8"
                               ).pack(side="left", padx=10)

        # ── Bottom bar
        bar = ctk.CTkFrame(self, fg_color="#111627", height=160, corner_radius=0)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        # Top accent line
        ctk.CTkFrame(bar, height=2, fg_color="#6366f1",
                     corner_radius=0).pack(fill="x")

        # Timer
        self.timer_var = ctk.StringVar(value="00:00.000")
        ctk.CTkLabel(bar, textvariable=self.timer_var,
                     font=ctk.CTkFont("Cascadia Code", 28, weight="bold"),
                     text_color="#6366f1").pack(pady=(10,0))

        self.status_var = ctk.StringVar(value="Ready to record")
        self.status_lbl = ctk.CTkLabel(bar, textvariable=self.status_var,
                                        font=ctk.CTkFont("Segoe UI", 10),
                                        text_color="#475569")
        self.status_lbl.pack()

        # Progress bar
        self.prog_var = ctk.DoubleVar(value=0)
        self.prog_bar = ctk.CTkProgressBar(bar, variable=self.prog_var,
                                            width=420,
                                            progress_color="#6366f1",
                                            fg_color="#1e2d4a")
        self.prog_bar.pack(pady=(4,0))
        self.prog_var.set(0)

        # Buttons
        btn_row = ctk.CTkFrame(bar, fg_color="transparent")
        btn_row.pack(pady=8)

        self.rec_btn = ctk.CTkButton(btn_row,
                                      text="Start Recording",
                                      width=160, height=38,
                                      font=ctk.CTkFont("Segoe UI",12,weight="bold"),
                                      fg_color="#22c55e",
                                      hover_color="#16a34a",
                                      corner_radius=8,
                                      command=self.toggle_recording)
        self.rec_btn.pack(side="left", padx=4)

        self.exp_btn = ctk.CTkButton(btn_row,
                                      text="Export MP4",
                                      width=120, height=38,
                                      font=ctk.CTkFont("Segoe UI",12,weight="bold"),
                                      fg_color="#1e2d4a",
                                      hover_color="#6366f1",
                                      state="disabled",
                                      corner_radius=8,
                                      command=self.export_mp4)
        self.exp_btn.pack(side="left", padx=4)

        self.prev_btn = ctk.CTkButton(btn_row,
                                       text="Preview",
                                       width=100, height=38,
                                       font=ctk.CTkFont("Segoe UI",12,weight="bold"),
                                       fg_color="#1e2d4a",
                                       hover_color="#6366f1",
                                       state="disabled",
                                       corner_radius=8,
                                       command=self.open_preview)
        self.prev_btn.pack(side="left", padx=4)

    # ── BG / preset helpers
    def _set_preset(self, p):
        self.canvas_var.set(p)
        for name,b in self._preset_btns.items():
            b.configure(fg_color="#6366f1" if name==p else "#1e2d4a")

    def _set_bg_grad(self, idx, btn):
        self.bg_type = "gradient"
        self.bg_val  = GRADIENTS[idx]
        for b in self._bg_btns:
            b.configure(highlightbackground="#1e2d4a")
        btn.configure(highlightbackground="#6366f1")

    def _set_bg_solid(self, col, btn):
        self.bg_type = "solid"
        self.bg_val  = col
        for b in self._bg_btns:
            b.configure(highlightbackground="#1e2d4a")
        btn.configure(highlightbackground="#6366f1")

    def _settings(self):
        return {
            "fps":         int(self.fps_var.get()),
            "region":      {"left":0,"top":0,"width":self.sw,"height":self.sh},
            "canvas_preset": self.canvas_var.get(),
            "padding":     int(self.padding_var.get()),
            "inset":       int(self.inset_var.get()),
            "roundness":   int(self.roundness_var.get()),
            "shadow":      int(self.shadow_var.get()),
            "bg_type":     self.bg_type,
            "bg_val":      self.bg_val,
            "cursor_size": int(self.cursor_size_var.get()),
            "stiffness":   float(self.stiffness_var.get()),
            "damping":     float(self.damping_var.get()),
            "cursor_tilt": self.tilt_var.get(),
            "click_ripple":self.ripple_var.get(),
            "auto_hide":   self.autohide_var.get(),
            "auto_zoom":   self.autozoom_var.get(),
            "zoom_level":  float(self.zoomlevel_var.get()),
            "zoom_dur":    float(self.zoomdur_var.get()),
        }

    # ── Recording
    def toggle_recording(self):
        if not self.recording:
            self.bundle_path = os.path.join(
                tempfile.gettempdir(),
                f"screensee_{int(time.time())}.screensee")
            self.recording = True
            self._rec_start = time.perf_counter()
            region = {"left":0,"top":0,"width":self.sw,"height":self.sh}
            self.recorder = Recorder(fps=int(self.fps_var.get()))
            try:
                self.recorder.start(region, self.bundle_path)
            except RuntimeError as e:
                messagebox.showerror("Recorder", str(e))
                self.recording = False
                return
            backend_msg = "Recording (cursor excluded)" if HAS_WGC else "Recording (mss — cursor will be in footage)"
            self.rec_btn.configure(text="Stop Recording",
                                    fg_color="#ef4444",
                                    hover_color="#dc2626")
            self.exp_btn.configure(state="disabled", fg_color="#1e2d4a")
            self.prev_btn.configure(state="disabled", fg_color="#1e2d4a")
            self.status_var.set(backend_msg)
            self.status_lbl.configure(text_color="#22c55e")
            self._tick()
        else:
            self.recording = False
            self.recorder.stop()
            self.rec_btn.configure(text="Start Recording",
                                    fg_color="#22c55e",
                                    hover_color="#16a34a")
            self.exp_btn.configure(state="normal", fg_color="#6366f1")
            self.prev_btn.configure(state="normal", fg_color="#1e2d4a")
            try:
                meta_path = os.path.join(self.bundle_path, "meta.json")
                with open(meta_path) as f:
                    meta = json.load(f)
                self.status_var.set(
                    f"Done — {meta['frame_count']} frames  •  "
                    f"{meta['duration']:.1f}s  •  bundle: {self.bundle_path}")
            except Exception:
                self.status_var.set(f"Done — bundle: {self.bundle_path}")
            self.status_lbl.configure(text_color="#475569")

    def _tick(self):
        if self.recording:
            e = time.perf_counter()-self._rec_start
            self.timer_var.set(
                f"{int(e//60):02d}:{int(e%60):02d}.{int((e%1)*1000):03d}")
            self.after(33, self._tick)

    # ── Export
    def export_mp4(self):
        if not self.bundle_path or not os.path.isdir(self.bundle_path):
            messagebox.showinfo("No Recording","Record first then export.")
            return
        out = filedialog.asksaveasfilename(
            defaultextension=".mp4",
            filetypes=[("MP4","*.mp4")],
            initialfile="screensee.mp4")
        if not out: return
        s = self._settings()
        bundle = self.bundle_path
        def run():
            p = Processor(s, progress_cb=self._on_progress)
            ok = p.run(bundle, out)
            if ok:
                messagebox.showinfo("Exported!",f"Saved:\n{out}")
            else:
                messagebox.showerror("Error",
                    "Export failed.\nCheck that FFmpeg is on PATH and the bundle is valid.")
        threading.Thread(target=run, daemon=True).start()

    def _on_progress(self, pct, msg):
        self.prog_var.set(pct/100)
        self.status_var.set(msg)

    # ── Preview
    def open_preview(self):
        if not self.bundle_path or not os.path.isdir(self.bundle_path):
            messagebox.showinfo("No Data","Record first then preview.")
            return

        raw_path = os.path.join(self.bundle_path, "raw.mkv")
        src = cv2.VideoCapture(raw_path)
        if not src.isOpened():
            messagebox.showerror("Preview", f"Failed to open {raw_path}")
            return
        total = int(src.get(cv2.CAP_PROP_FRAME_COUNT)) or 1

        win = ctk.CTkToplevel(self)
        win.title("Live Preview")
        win.geometry("720x520")
        win.configure(fg_color="#0d1117")
        win.protocol("WM_DELETE_WINDOW",
                     lambda: (src.release(), win.destroy()))

        PREV_W, PREV_H = 680, 380

        img_lbl = ctk.CTkLabel(win, text="")
        img_lbl.pack(pady=(12,4))

        ctk.CTkLabel(win,
                     text="Adjust sliders in main window — preview updates live",
                     font=ctk.CTkFont("Segoe UI",10),
                     text_color="#475569").pack()

        # Scrubber
        scr_row = ctk.CTkFrame(win, fg_color="transparent")
        scr_row.pack(fill="x", padx=16, pady=6)
        ctk.CTkLabel(scr_row, text="Frame",
                     font=ctk.CTkFont("Segoe UI",10),
                     text_color="#94a3b8").pack(side="left")
        frame_var = ctk.IntVar(value=min(30, total-1))
        scr = ctk.CTkSlider(scr_row,
                             from_=0, to=max(1, total-1),
                             variable=frame_var, width=560,
                             button_color="#6366f1",
                             button_hover_color="#8b5cf6",
                             progress_color="#6366f1")
        scr.pack(side="left", padx=8)

        ctk.CTkButton(win, text="Close",
                      width=100, height=32,
                      fg_color="#1e2d4a", hover_color="#6366f1",
                      command=lambda: (src.release(), win.destroy())
                      ).pack(pady=(0,10))

        win._img = None
        win._last = None

        def render(*_):
            if not win.winfo_exists(): return
            fi = int(frame_var.get())
            s  = self._settings()
            key = (fi, str(s))
            if key == win._last: return
            win._last = key

            src.set(cv2.CAP_PROP_POS_FRAMES, fi)
            ok, f = src.read()
            if not ok: return
            img = Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGBA))
            preset = s["canvas_preset"]
            if preset and preset != "Original":
                cw,ch = CANVAS_PRESETS[preset]
            else:
                p2 = s["padding"]*2
                cw = f.shape[1]+p2+60
                ch = f.shape[0]+p2+60

            cv_img,*_ = composite(img, cw, ch,
                                   s["bg_type"], s["bg_val"],
                                   s["padding"], s["inset"],
                                   s["roundness"], s["shadow"])
            asp = cw/ch
            if asp > PREV_W/PREV_H:
                pw=PREV_W; ph=int(PREV_W/asp)
            else:
                ph=PREV_H; pw=int(PREV_H*asp)
            small = cv_img.resize((pw,ph), Image.LANCZOS)
            lb = Image.new("RGBA",(PREV_W,PREV_H),(10,10,20,255))
            lb.paste(small,((PREV_W-pw)//2,(PREV_H-ph)//2))
            tk_img = ImageTk.PhotoImage(lb)
            img_lbl.configure(image=tk_img)
            win._img = tk_img

        scr.configure(command=render)
        render()

        def poll():
            if win.winfo_exists():
                render(); win.after(250, poll)
        win.after(250, poll)


# ============================================================
if __name__ == "__main__":
    app = App()
    app.mainloop()
