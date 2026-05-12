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

VERSION  = "3.7"
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

    def trigger(self, nx, ny, z=2.0, t=None, in_dur=1.0):
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
            "ease": self._ease_in_out_cubic,
        }
        self._target_zoom = z
        self._target_cx   = nx
        self._target_cy   = ny

    def release(self, t=None, out_dur=1.4):
        """Zoom out — only starts new animation if not already zooming out"""
        if self._target_zoom <= 1.02:
            return  # already released or releasing
        self._anim = {
            "sz": self.zoom, "ez": 1.0,
            "scx": self.cx, "ecx": 0.5,
            "scy": self.cy, "ecy": 0.5,
            "t0": t or time.time(),
            "dur": out_dur,
            "ease": self._ease_in_out_cubic,
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

def draw_cursor(img_rgba, cx, cy, size=32, opacity=1.0, tilt=0.0,
                pressed=False, dragging=False):
    # `tilt` is accepted for back-compat but intentionally ignored:
    # rotating the sprite around its centre moves the tip away from
    # (cx, cy) and produces the visible "bending" that v3.1 had.
    scale = size / 32.0
    scaled   = [(int(x*scale), int(y*scale)) for x,y in ARROW_PTS]
    shadow_p = [(int(x*scale), int(y*scale)) for x,y in ARROW_SHADOW]
    cur = Image.new("RGBA", (int(64*scale), int(64*scale)), (0,0,0,0))
    d   = ImageDraw.Draw(cur)
    d.polygon(shadow_p, fill=(0,0,0,int(60*opacity)))
    d.polygon(scaled,   fill=(255,255,255,int(255*opacity)))
    d.line([scaled[i] for i in range(len(scaled))] + [scaled[0]],
           fill=(0,0,0,int(200*opacity)), width=max(1,int(1.5*scale)))
    # Sprite tip lives at sprite (0,0); the small -2 offset accounts for
    # the antialiased outline so the visible tip lands on (cx, cy).
    img_rgba.paste(cur, (int(cx)-2, int(cy)-2), cur)

    if pressed and opacity > 0.02:
        ring_r = int(14 * scale * (1.4 if dragging else 1.0))
        ring_a = int((140 if dragging else 200) * opacity)
        ring_w = max(2, int(2 * scale))
        rd = ImageDraw.Draw(img_rgba)
        rd.ellipse([int(cx)-ring_r, int(cy)-ring_r,
                    int(cx)+ring_r, int(cy)+ring_r],
                   outline=(99, 102, 241, ring_a), width=ring_w)
        if dragging:
            inner_r = int(5 * scale)
            rd.ellipse([int(cx)-inner_r, int(cy)-inner_r,
                        int(cx)+inner_r, int(cy)+inner_r],
                       fill=(99, 102, 241, int(110 * opacity)))


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
              padding, inset, roundness, shadow, glass=0):
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

    # Glass halo — translucent frosted plate behind the recording.
    # Shares the recording's roundness so it reads as a window frame.
    if glass > 0:
        gx, gy = rx - glass, ry - glass
        gw, gh = ow + glass*2, oh + glass*2
        gr     = roundness + glass // 2
        glass_layer = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glass_layer)
        # Soft fill: very translucent white, blurred for a frosted look.
        gd.rounded_rectangle([gx, gy, gx+gw, gy+gh],
                             radius=gr,
                             fill=(255, 255, 255, 35))
        glass_layer = glass_layer.filter(
            ImageFilter.GaussianBlur(max(2, glass // 2)))
        # Crisper bright edge so the halo reads as a glass rim.
        rim = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        ImageDraw.Draw(rim).rounded_rectangle(
            [gx, gy, gx+gw, gy+gh],
            radius=gr,
            outline=(255, 255, 255, 90),
            width=max(1, glass // 6))
        glass_layer = Image.alpha_composite(glass_layer, rim)
        canvas = Image.alpha_composite(canvas, glass_layer)

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
#  ANIMATION TRACK + FRAME RENDER
#  Shared between live preview and final export, so the preview
#  shows exactly what will be exported.
# ============================================================
def precompute_track(events, meta, s):
    """Walk through every frame once, producing the cursor/zoom state.
    Returns list[dict] indexed by frame number. Cheap (~ms per 1k frames)."""
    fps    = meta["fps"]
    n      = meta.get("frame_count") or 0
    reg    = meta["region"]
    sw, sh = reg["width"], reg["height"]
    # Per-frame content time. Falls back to fi/fps for older bundles, but
    # using the recorded times avoids the half-frame-ish drift between
    # capture delivery and "frame N is at N/fps".
    ftimes = meta.get("frame_times") or [fi / fps for fi in range(n)]
    if n <= 0:
        return []

    # Smoothness=0 bypasses the spring entirely so the synthetic cursor
    # lands exactly where pynput recorded the event — no lag. The spring
    # still applies for stylised motion when Smoothness > 0.
    use_spring = s["stiffness"] > 0.001
    smoother = (
        MassSpringDamper(stiffness=s["stiffness"]*1200.0,
                          damping  =s["damping"]*90.0,
                          mass=3.0, fps=fps)
        if use_spring else None
    )
    zoom_eng = ZoomEngine(fps=fps)
    # Separate, gentler spring for the zoom-camera centre. Tuned for a
    # Screen-Studio-style premium pan: ~1 s to settle, slightly
    # overdamped so there is no overshoot at the end of a fast cursor
    # move. (FocuSee's defaults were k=170/b=50; the camera felt snappy
    # by comparison.)
    screen = MassSpringDamper(stiffness=50.0, damping=25.7,
                              mass=3.0, fps=fps)

    evs = sorted([
        (e["t"], e["type"], e["x"]-reg["left"], e["y"]-reg["top"])
        for e in events
    ], key=lambda x: x[0])

    clicks = [(t,x,y) for t,et,x,y in evs if et == "CLICK_L"]
    zoom_wins = []
    if s["auto_zoom"]:
        for i,(t,cx,cy) in enumerate(clicks):
            end = t + s["zoom_dur"]
            if i+1 < len(clicks):
                end = min(end, clicks[i+1][0]-0.05)
            zoom_wins.append({
                "s": t-0.05, "e": end,
                "nx": cx/sw,  "ny": cy/sh,
                "z":  s["zoom_level"],
            })

    track       = []
    cur_x, cur_y = sw//2, sh//2
    prev_x, prev_y = cur_x, cur_y
    pressed     = False
    last_press_t = -1.0
    cur_opacity = 1.0
    idle_frames = 0
    ev_i        = 0
    ripple_dur  = 0.45
    fired_rips  = []   # list of (t_start, x, y)

    for fi in range(n):
        tf = ftimes[fi] if fi < len(ftimes) else fi / fps
        while ev_i < len(evs) and evs[ev_i][0] <= tf:
            t, et, ex, ey = evs[ev_i]
            if et == "MOVE":
                cur_x, cur_y = ex, ey
            elif et == "CLICK_L":
                pressed = True
                last_press_t = t
                if s["click_ripple"]:
                    fired_rips.append((t, ex, ey))
            elif et == "RELEASE_L":
                pressed = False
            ev_i += 1

        if use_spring:
            sx, sy = smoother.update(cur_x, cur_y, t=tf)
            spd    = math.sqrt(smoother.vx**2 + smoother.vy**2)
        else:
            sx, sy = cur_x, cur_y
            # Velocity from raw cursor deltas — keeps auto-hide working
            # in the bypassed-spring path.
            spd = math.hypot((cur_x - prev_x) * fps,
                             (cur_y - prev_y) * fps)
        prev_x, prev_y = cur_x, cur_y

        tilt = 0.0

        if s["auto_hide"]:
            if spd < 0.5:
                idle_frames += 1
                if idle_frames > fps * 1.5:
                    cur_opacity = max(0.0, cur_opacity - 0.08)
            else:
                idle_frames = 0
                cur_opacity = min(1.0, cur_opacity + 0.25)
        else:
            cur_opacity = 1.0

        active = next((zw for zw in zoom_wins if zw["s"] <= tf <= zw["e"]),
                      None)
        if active:
            # Zoom level animates with ease-in-out via ZoomEngine. The centre
            # is driven separately by `screen` against the live cursor so the
            # camera pans to keep the pointer in frame instead of clamping
            # to where the click happened.
            zoom_eng.trigger(0.5, 0.5, active["z"], t=tf)
            target_cx, target_cy = sx / sw, sy / sh
        else:
            zoom_eng.release(t=tf)
            target_cx, target_cy = 0.5, 0.5
        z = zoom_eng.update(t=tf)[0]
        zcx, zcy = screen.update(target_cx, target_cy, t=tf)

        active_rips = [
            (rx, ry, tf - rt, ripple_dur)
            for (rt, rx, ry) in fired_rips
            if 0 <= tf - rt < ripple_dur
        ]

        dragging = pressed and spd > 8.0

        track.append({
            "sx": sx, "sy": sy,
            "tilt": tilt, "opacity": cur_opacity,
            "zoom": z, "zcx": zcx, "zcy": zcy,
            "pressed": pressed, "dragging": dragging,
            "ripples": active_rips,
        })

    # Loop cursor position — Screen Studio's "return to start" trick. Over
    # the last ~0.8s of the clip, ease the smoothed cursor coords back to
    # where the recording opened, so a looping clip doesn't snap.
    if s.get("loop_cursor") and len(track) > 4:
        loop_frames = min(int(0.8 * fps), len(track) - 1)
        start_sx = track[0]["sx"]
        start_sy = track[0]["sy"]
        for i in range(loop_frames):
            fi_loop = len(track) - loop_frames + i
            u  = (i + 1) / loop_frames
            ea = 1.0 - (1.0 - u) ** 3   # ease-out cubic
            e  = track[fi_loop]
            e["sx"] = e["sx"] * (1.0 - ea) + start_sx * ea
            e["sy"] = e["sy"] * (1.0 - ea) + start_sy * ea

    return track


def _zoom_origin(zcx, zcy, z, sw, sh):
    rw_z = sw / z
    rh_z = sh / z
    x1 = max(0, min(sw - rw_z, zcx * sw - rw_z / 2))
    y1 = max(0, min(sh - rh_z, zcy * sh - rh_z / 2))
    return x1, y1


def render_frame(raw_bgr, st, s, sw, sh, cw, ch):
    """Render one composited preview/export frame from the raw screen capture
    plus the precomputed animation state."""
    z = st["zoom"]
    if z > 1.02:
        h, w = raw_bgr.shape[:2]
        rw, rh = int(w/z), int(h/z)
        x1 = max(0, min(w-rw, int(st["zcx"]*w - rw/2)))
        y1 = max(0, min(h-rh, int(st["zcy"]*h - rh/2)))
        zoomed = cv2.resize(raw_bgr[y1:y1+rh, x1:x1+rw], (w, h),
                            interpolation=cv2.INTER_LINEAR)
    else:
        zoomed = raw_bgr

    img = Image.fromarray(cv2.cvtColor(zoomed, cv2.COLOR_BGR2RGBA))

    sx, sy = st["sx"], st["sy"]
    if z > 1.02:
        x1z, y1z = _zoom_origin(st["zcx"], st["zcy"], z, sw, sh)
        draw_sx = (sx - x1z) * z
        draw_sy = (sy - y1z) * z
    else:
        draw_sx, draw_sy = sx, sy

    # Ripples first, so cursor sits on top.
    if st["ripples"]:
        rd = ImageDraw.Draw(img)
        for (rx, ry, age, dur) in st["ripples"]:
            if z > 1.02:
                x1z, y1z = _zoom_origin(st["zcx"], st["zcy"], z, sw, sh)
                rxd = (rx - x1z) * z
                ryd = (ry - y1z) * z
            else:
                rxd, ryd = rx, ry
            prog = age / dur
            ease = 1 - (1 - prog) ** 2
            rad  = int(ease * 55)
            alpha = int((1 - prog) * 180)
            rd.ellipse([rxd-rad, ryd-rad, rxd+rad, ryd+rad],
                       outline=(100, 150, 255, alpha), width=2)

    if st["opacity"] > 0.02 and s.get("show_cursor", True):
        draw_cursor(img, draw_sx, draw_sy,
                    size=s["cursor_size"], opacity=st["opacity"],
                    tilt=st["tilt"],
                    pressed=st["pressed"], dragging=st["dragging"])

    canvas, *_ = composite(
        img, cw, ch,
        s["bg_type"], s["bg_val"],
        s["padding"], s["inset"],
        s["roundness"], s["shadow"],
        glass=s.get("glass", 0))
    return canvas


def canvas_dims(meta_or_region, s):
    """Resolve final canvas (cw, ch) given meta/region + settings."""
    reg = meta_or_region.get("region") if "region" in meta_or_region else meta_or_region
    sw, sh = reg["width"], reg["height"]
    preset = s["canvas_preset"]
    if preset and preset != "Original":
        return CANVAS_PRESETS[preset]
    p = s["padding"]
    return sw + p*2 + 60, sh + p*2 + 60


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
        self._frame_times = []          # content-time per kept frame
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
    def _write_frame(self, bgra, content_t=None):
        # Decimate to target fps. content_t is when the *pixels* were captured,
        # which may be earlier than _now() because of grab/decode latency. We
        # stamp the bundle with content_t so the processor can align events
        # to the exact frame they belong on.
        t = self._now() if content_t is None else content_t
        if t - self._last_write < (1.0 / self.fps) - 0.001:
            return
        self._last_write = t
        try:
            self._ffmpeg.stdin.write(bgra.tobytes())
            self._frame_times.append(t)
            self._frame_idx += 1
        except (BrokenPipeError, OSError, ValueError):
            self.running = False

    # ── Backend 1: Windows.Graphics.Capture (cursor excluded) ──
    def _start_wgc(self, region):
        # NOTE on draw_border: some Windows 10 builds and early Windows 11
        # builds reject IsBorderRequired=False at session-start time with
        # "Toggling the capture border is not supported". The exception
        # surfaces asynchronously inside the WGC worker thread, so the main
        # thread happily proceeds while no frames are ever written. Passing
        # True keeps the yellow recording indicator visible but it does NOT
        # appear in the captured frames — it is a Windows-drawn overlay.
        cap = WindowsCapture(
            cursor_capture=False,
            draw_border=True,
            monitor_index=1,
            window_name=None,
        )
        l, t, w, h = region["left"], region["top"], region["width"], region["height"]

        @cap.event
        def on_frame_arrived(frame: "Frame", ctrl: "InternalCaptureControl"):
            if not self.running:
                ctrl.stop(); return
            t_arrived = self._now()
            buf = frame.frame_buffer  # HxWx4 BGRA
            if (buf.shape[1], buf.shape[0]) != (w, h):
                buf = buf[t:t+h, l:l+w]
            self._write_frame(buf, content_t=t_arrived)

        @cap.event
        def on_closed():
            # If the session ends while we still think we are recording,
            # WGC died on us. Flip the flag so the rest of the pipeline
            # notices nothing is being captured.
            if self.running:
                self.running = False

        cap.start_free_threaded()
        self._capture = cap

    # ── Backend 2: mss fallback (cursor will be in footage) ────
    def _start_mss(self, region):
        def loop():
            iv = 1.0 / self.fps
            with mss.mss() as sct:
                while self.running:
                    # Stamp content time at the start of the grab — this is
                    # the moment the screen pixels actually represent.
                    t_capture = self._now()
                    t_loop    = time.perf_counter()
                    img = np.asarray(sct.grab(region))  # BGRA
                    self._write_frame(img, content_t=t_capture)
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
        self._frame_times.clear()
        self._frame_idx = 0
        self._last_write = -1.0
        self._region = region
        self._t0 = time.perf_counter()
        self.running = True

        # WGC may import fine but fail at session start on some Windows
        # builds ("Toggling the capture border is not supported"). When
        # that happens we transparently take the mss path so recording
        # still works — at the cost of the OS cursor appearing in the
        # raw footage. self._active_backend reflects what actually ran.
        self._active_backend = "mss"
        if HAS_WGC:
            try:
                self._start_wgc(region)
                self._active_backend = "windows-capture"
            except Exception as e:
                print(f"[ScreenSee] WGC start failed: {e}\n"
                      f"[ScreenSee] Falling back to mss capture.")
                self._capture = None
                self._start_mss(region)
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
                "frame_times": self._frame_times,
                "duration": self._now(),
                "cursor_excluded": self._active_backend == "windows-capture",
                "backend": self._active_backend,
            }, f)
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

        s   = self.s
        fps = meta.get("fps", s["fps"])
        reg = meta.get("region") or s["region"]
        sw, sh = reg["width"], reg["height"]
        cw, ch = canvas_dims(meta, s)

        self.prog(2, "Computing animation track...")
        track = precompute_track(events_raw, meta, s)
        n = len(track) or int(src.get(cv2.CAP_PROP_FRAME_COUNT)) or 1

        self.prog(5, "Opening encoder...")
        cmd = [
            "ffmpeg","-y","-loglevel","error",
            "-f","rawvideo","-vcodec","rawvideo",
            "-s",f"{cw}x{ch}","-pix_fmt","bgr24",
            "-r",str(fps),"-i","pipe:0",
            "-c:v","libx264","-preset","fast",
            "-crf","17","-pix_fmt","yuv420p",
            out_path,
        ]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            src.release()
            self.prog(0, "FFmpeg not found"); return False

        self.prog(8, "Rendering frames...")
        fi = 0
        while fi < n:
            ok, frame = src.read()
            if not ok: break
            canvas = render_frame(frame, track[fi], s, sw, sh, cw, ch)
            bgr = cv2.cvtColor(np.array(canvas), cv2.COLOR_RGBA2BGR)
            try:
                proc.stdin.write(bgr.tobytes())
            except BrokenPipeError:
                break
            if fi % 15 == 0:
                self.prog(8 + int((fi/n) * 87),
                          f"Frame {fi+1}/{n}  ({int(fi/n*100)}%)")
            fi += 1

        src.release()
        proc.stdin.close()
        proc.wait()
        self.prog(100, "Done!")
        return True


# ============================================================
#  UI  —  two-state shell:
#         (1) Welcome:  big record button only
#         (2) Editor:   embedded preview + sidebar after recording
# ============================================================
class App(ctk.CTk):
    BG       = "#0a0a0f"
    PANEL    = "#14141c"
    PANEL_2  = "#1c1c28"
    BORDER   = "#262635"
    MUTED    = "#8b8b9c"
    FG       = "#e6e6f0"
    ACCENT   = "#6366f1"
    ACCENT_H = "#818cf8"
    DANGER   = "#ef4444"
    OK       = "#22c55e"

    SIDEBAR_W = 340

    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1280x820")
        self.minsize(1100, 720)
        self.configure(fg_color=self.BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Recording state
        self.recorder       = Recorder()
        self.recording      = False
        self.bundle_path    = None
        self._rec_start     = 0.0

        # Background selection (not a Var, so trace doesn't fire)
        self.bg_type = "gradient"
        self.bg_val  = GRADIENTS[0]

        # Editor state
        self._preview_src   = None
        self._meta          = None
        self._meta_events   = None
        self._track         = None
        self._track_key     = None
        self._cur_frame     = 0
        self._total         = 1
        self._fps_meta      = 30
        self._playing       = False
        self._programmatic  = False
        self._render_pending = False
        self._tk_img        = None

        # Hidden defaults (controls dropped from UI)
        self._inset       = 0
        self._cursor_tilt = False   # rotation moves the tip off-target
        # Hold ~1 s at peak zoom AFTER the 1 s ease-in, so a click looks
        # like: 1 s zoom in → 1 s held → 1.4 s ease out.
        self._zoom_dur    = 2.0

        # Detect display
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
                         text_color=self.DANGER).pack(pady=40)
            return

        self._init_vars()
        self._welcome_frame = self._build_welcome()
        self._editor_frame  = None
        self._show_welcome()

    # ── shared variables ───────────────────────────────────────
    def _init_vars(self):
        self.fps_var          = ctk.StringVar(value="30")
        self.canvas_var       = ctk.StringVar(value="16:9")
        self.padding_var      = ctk.DoubleVar(value=60)
        self.roundness_var    = ctk.DoubleVar(value=14)
        self.shadow_var       = ctk.DoubleVar(value=70)
        self.cursor_size_var  = ctk.DoubleVar(value=36)
        self.glass_var        = ctk.DoubleVar(value=14)
        self.smooth_var       = ctk.DoubleVar(value=0.0)
        self.ripple_var       = ctk.BooleanVar(value=True)
        self.show_cursor_var  = ctk.BooleanVar(value=True)
        self.autohide_var     = ctk.BooleanVar(value=True)
        self.loop_cursor_var  = ctk.BooleanVar(value=False)
        self.autozoom_var     = ctk.BooleanVar(value=True)
        self.zoomlevel_var    = ctk.DoubleVar(value=2.0)

        for v in (self.canvas_var, self.padding_var, self.roundness_var,
                  self.shadow_var, self.glass_var,
                  self.cursor_size_var, self.smooth_var,
                  self.ripple_var, self.show_cursor_var,
                  self.autohide_var, self.loop_cursor_var,
                  self.autozoom_var, self.zoomlevel_var):
            v.trace_add("write", lambda *a: self._request_render())

    # ── small helpers ──────────────────────────────────────────
    def _label(self, parent, text, size=12, color=None, bold=False):
        return ctk.CTkLabel(parent, text=text,
            font=ctk.CTkFont("Segoe UI", size,
                             weight="bold" if bold else "normal"),
            text_color=color or self.MUTED)

    def _section(self, parent, title):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(fill="x", padx=18, pady=(20, 6))
        self._label(f, title, 10, "#5e5e75", bold=True).pack(side="left")
        ctk.CTkFrame(f, height=1, fg_color=self.BORDER).pack(
            side="left", fill="x", expand=True, padx=(10, 0))

    def _slider(self, parent, label, var, lo, hi, fmt=None):
        if fmt is None:
            fmt = lambda v: str(int(float(v)))
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(8, 0))
        self._label(row, label, 11).pack(side="left")
        val_lbl = self._label(row, fmt(var.get()), 11, self.ACCENT, bold=True)
        val_lbl.pack(side="right")
        sl = ctk.CTkSlider(row, from_=lo, to=hi, variable=var,
                           button_color=self.ACCENT,
                           button_hover_color=self.ACCENT_H,
                           progress_color=self.ACCENT,
                           command=lambda v, l=val_lbl, f=fmt: l.configure(text=f(v)))
        sl.pack(fill="x", padx=(12, 8), pady=(2, 0))

    def _toggle(self, parent, label, var):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(10, 0))
        self._label(row, label, 11, self.FG).pack(side="left")
        ctk.CTkSwitch(row, variable=var, text="",
                      width=40, height=20,
                      button_color=self.ACCENT,
                      button_hover_color=self.ACCENT_H,
                      progress_color=self.ACCENT).pack(side="right")

    # ── state switching ────────────────────────────────────────
    def _show_welcome(self):
        if self._editor_frame:
            self._editor_frame.pack_forget()
        self._welcome_frame.pack(fill="both", expand=True)

    def _show_editor(self):
        self._welcome_frame.pack_forget()
        if not self._editor_frame:
            self._editor_frame = self._build_editor()
        self._editor_frame.pack(fill="both", expand=True)
        self.after(150, self._load_bundle_into_preview)

    # ── WELCOME screen ─────────────────────────────────────────
    def _build_welcome(self):
        wrap = ctk.CTkFrame(self, fg_color=self.BG)
        center = ctk.CTkFrame(wrap, fg_color="transparent")
        center.place(relx=0.5, rely=0.5, anchor="center")

        ctk.CTkLabel(center, text=APP_NAME,
                     font=ctk.CTkFont("Segoe UI", 56, weight="bold"),
                     text_color=self.FG).pack()
        ctk.CTkLabel(center, text="Beautiful screen recordings, automatically",
                     font=ctk.CTkFont("Segoe UI", 15),
                     text_color=self.MUTED).pack(pady=(4, 36))

        self.welcome_btn = ctk.CTkButton(
            center, text="●  Start Recording",
            width=260, height=62,
            font=ctk.CTkFont("Segoe UI", 16, weight="bold"),
            fg_color=self.OK, hover_color="#16a34a",
            corner_radius=12,
            command=self.toggle_recording)
        self.welcome_btn.pack(pady=(0, 22))

        fps_row = ctk.CTkFrame(center, fg_color="transparent")
        fps_row.pack()
        ctk.CTkLabel(fps_row, text="Frame rate",
                     font=ctk.CTkFont("Segoe UI", 11),
                     text_color=self.MUTED).pack(side="left", padx=(0, 12))
        for f in ("30", "60"):
            ctk.CTkRadioButton(
                fps_row, text=f"{f} fps",
                variable=self.fps_var, value=f,
                radiobutton_width=14, radiobutton_height=14,
                fg_color=self.ACCENT, hover_color=self.ACCENT_H,
                font=ctk.CTkFont("Segoe UI", 12),
                text_color=self.FG).pack(side="left", padx=8)

        ctk.CTkLabel(center, text=f"Display: {self.sw} × {self.sh}",
                     font=ctk.CTkFont("Segoe UI", 10),
                     text_color="#5e5e75").pack(pady=(36, 6))

        # Backend chip. Both paths produce cursor-free frames on Windows
        # (WGC with cursor_capture=False, mss via BitBlt which never
        # captures the cursor). WGC is just GPU-accelerated; mss is a
        # fine software fallback.
        chip_frame = ctk.CTkFrame(center, fg_color=self.PANEL_2,
                                   corner_radius=6)
        chip_frame.pack()
        ctk.CTkLabel(
            chip_frame,
            text=("● Capture: WGC  (hardware, cursor excluded)" if HAS_WGC
                  else "● Capture: mss  (software fallback, cursor excluded)"),
            font=ctk.CTkFont("Segoe UI", 10, weight="bold"),
            text_color="#22c55e" if HAS_WGC else "#60a5fa",
        ).pack(padx=12, pady=4)

        self.welcome_status = ctk.CTkLabel(
            center, text="",
            font=ctk.CTkFont("Cascadia Code", 26, weight="bold"),
            text_color=self.ACCENT)
        self.welcome_status.pack(pady=(28, 0))
        return wrap

    # ── EDITOR screen ──────────────────────────────────────────
    def _build_editor(self):
        wrap = ctk.CTkFrame(self, fg_color=self.BG)

        # Top bar
        top = ctk.CTkFrame(wrap, fg_color=self.PANEL, height=58, corner_radius=0)
        top.pack(fill="x")
        top.pack_propagate(False)
        ctk.CTkFrame(wrap, height=1, fg_color=self.BORDER).pack(fill="x")

        ctk.CTkLabel(top, text=APP_NAME,
                     font=ctk.CTkFont("Segoe UI", 18, weight="bold"),
                     text_color=self.FG).pack(side="left", padx=22)

        right = ctk.CTkFrame(top, fg_color="transparent")
        right.pack(side="right", padx=14)
        ctk.CTkButton(right, text="New Recording",
                      width=130, height=34,
                      font=ctk.CTkFont("Segoe UI", 12),
                      fg_color=self.PANEL_2, hover_color=self.BORDER,
                      text_color=self.FG, corner_radius=8,
                      command=self._new_recording).pack(side="left", padx=4)
        ctk.CTkButton(right, text="Export MP4",
                      width=130, height=34,
                      font=ctk.CTkFont("Segoe UI", 12, weight="bold"),
                      fg_color=self.ACCENT, hover_color=self.ACCENT_H,
                      corner_radius=8,
                      command=self.export_mp4).pack(side="left", padx=4)

        # Body: preview (left) + sidebar (right)
        body = ctk.CTkFrame(wrap, fg_color=self.BG)
        body.pack(fill="both", expand=True)

        side = ctk.CTkScrollableFrame(
            body, fg_color=self.PANEL, width=self.SIDEBAR_W, corner_radius=0,
            scrollbar_button_color="#2a2a3e",
            scrollbar_button_hover_color=self.ACCENT)
        side.pack(side="right", fill="y")
        ctk.CTkFrame(body, width=1, fg_color=self.BORDER).pack(side="right", fill="y")
        self._build_sidebar(side)

        prev_area = ctk.CTkFrame(body, fg_color=self.BG)
        prev_area.pack(side="left", fill="both", expand=True)

        self.prev_canvas = ctk.CTkFrame(prev_area, fg_color="#000000",
                                         corner_radius=10)
        self.prev_canvas.pack(fill="both", expand=True, padx=24, pady=(20, 12))
        self.prev_canvas.bind("<Configure>",
                              lambda e: self._request_render(recompute_track=False))
        self.prev_label = ctk.CTkLabel(self.prev_canvas, text="")
        self.prev_label.place(relx=0.5, rely=0.5, anchor="center")

        # Transport bar
        tr = ctk.CTkFrame(prev_area, fg_color=self.PANEL, height=64, corner_radius=14)
        tr.pack(fill="x", padx=24, pady=(0, 6))
        tr.pack_propagate(False)

        self.play_btn = ctk.CTkButton(
            tr, text="▶", width=44, height=44,
            font=ctk.CTkFont("Segoe UI", 16, weight="bold"),
            fg_color=self.ACCENT, hover_color=self.ACCENT_H,
            corner_radius=22, command=self._toggle_play)
        self.play_btn.pack(side="left", padx=12, pady=10)

        self.time_lbl = ctk.CTkLabel(
            tr, text="00:00 / 00:00",
            font=ctk.CTkFont("Cascadia Code", 12),
            text_color=self.MUTED, width=110)
        self.time_lbl.pack(side="left", padx=(0, 8))

        self.scrub_var = ctk.DoubleVar(value=0)
        self.scrub = ctk.CTkSlider(
            tr, from_=0, to=1, variable=self.scrub_var,
            button_color=self.ACCENT, button_hover_color=self.ACCENT_H,
            progress_color=self.ACCENT,
            command=self._on_scrub)
        self.scrub.pack(side="left", fill="x", expand=True, padx=12, pady=10)

        # Export progress
        self.prog_var = ctk.DoubleVar(value=0)
        self.prog_bar = ctk.CTkProgressBar(prev_area, variable=self.prog_var,
                                            progress_color=self.ACCENT,
                                            fg_color=self.PANEL, height=4)
        self.prog_bar.pack(fill="x", padx=24, pady=(8, 4))
        self.prog_var.set(0)

        self.status_var = ctk.StringVar(value="")
        ctk.CTkLabel(prev_area, textvariable=self.status_var,
                     font=ctk.CTkFont("Segoe UI", 10),
                     text_color=self.MUTED).pack(pady=(0, 8))

        return wrap

    # ── Sidebar (pared down to essentials) ────────────────────
    def _build_sidebar(self, parent):
        # CANVAS
        self._section(parent, "CANVAS")
        cp = ctk.CTkFrame(parent, fg_color="transparent")
        cp.pack(fill="x", padx=14, pady=(2, 0))
        self._preset_btns = {}
        for preset in ("Original", "16:9", "1:1", "4:3", "9:16"):
            sel = (preset == self.canvas_var.get())
            b = ctk.CTkButton(cp, text=preset, width=58, height=30,
                              font=ctk.CTkFont("Segoe UI", 11, weight="bold"),
                              fg_color=self.ACCENT if sel else self.PANEL_2,
                              hover_color=self.ACCENT_H,
                              corner_radius=7,
                              command=lambda p=preset: self._set_preset(p))
            b.pack(side="left", padx=2)
            self._preset_btns[preset] = b

        # BACKGROUND
        self._section(parent, "BACKGROUND")
        ctk.CTkLabel(parent, text="Gradients",
                     font=ctk.CTkFont("Segoe UI", 10),
                     text_color="#5e5e75").pack(anchor="w", padx=18, pady=(2, 4))
        gg = ctk.CTkFrame(parent, fg_color="transparent")
        gg.pack(padx=14)
        self._bg_btns = []
        for i, (c1, c2) in enumerate(GRADIENTS):
            btn = tk.Canvas(gg, width=44, height=28,
                             highlightthickness=2,
                             highlightbackground=self.ACCENT if i == 0 else self.BORDER,
                             cursor="hand2", bd=0)
            btn.grid(row=i // 6, column=i % 6, padx=3, pady=3)
            for x in range(44):
                t = x / 44
                r = int(int(c1[1:3], 16) * (1 - t) + int(c2[1:3], 16) * t)
                g = int(int(c1[3:5], 16) * (1 - t) + int(c2[3:5], 16) * t)
                b = int(int(c1[5:7], 16) * (1 - t) + int(c2[5:7], 16) * t)
                btn.create_line(x, 0, x, 28, fill=f"#{r:02x}{g:02x}{b:02x}")
            btn.bind("<Button-1>", lambda e, idx=i, b=btn: self._set_bg_grad(idx, b))
            self._bg_btns.append(btn)

        ctk.CTkLabel(parent, text="Solid colors",
                     font=ctk.CTkFont("Segoe UI", 10),
                     text_color="#5e5e75").pack(anchor="w", padx=18, pady=(12, 4))
        sg = ctk.CTkFrame(parent, fg_color="transparent")
        sg.pack(padx=14, pady=(0, 4))
        for i, col in enumerate(SOLIDS):
            btn = tk.Canvas(sg, width=34, height=24, bg=col, bd=0,
                             highlightthickness=2,
                             highlightbackground=self.BORDER, cursor="hand2")
            btn.grid(row=i // 8, column=i % 8, padx=3, pady=3)
            btn.bind("<Button-1>", lambda e, c=col, b=btn: self._set_bg_solid(c, b))
            self._bg_btns.append(btn)

        # FRAME STYLE
        self._section(parent, "FRAME")
        self._slider(parent, "Padding",   self.padding_var,   0, 160)
        self._slider(parent, "Roundness", self.roundness_var, 0, 40)
        self._slider(parent, "Shadow",    self.shadow_var,    0, 100)
        self._slider(parent, "Glass halo", self.glass_var,    0, 40)

        # CURSOR
        self._section(parent, "CURSOR")
        self._toggle(parent, "Cursor overlay",   self.show_cursor_var)
        self._slider(parent, "Size",             self.cursor_size_var, 16, 64)
        self._slider(parent, "Smoothness",       self.smooth_var, 0.0, 1.0,
                     fmt=lambda v: f"{float(v):.2f}")
        self._toggle(parent, "Auto-hide idle",   self.autohide_var)
        self._toggle(parent, "Click ripples",    self.ripple_var)
        self._toggle(parent, "Loop to start",    self.loop_cursor_var)

        # AUTO ZOOM
        self._section(parent, "AUTO ZOOM")
        self._toggle(parent, "Zoom on click", self.autozoom_var)
        self._slider(parent, "Zoom level", self.zoomlevel_var, 1.2, 3.0,
                     fmt=lambda v: f"{float(v):.1f}×")

        ctk.CTkFrame(parent, fg_color="transparent", height=20).pack()

    # ── Background pickers ─────────────────────────────────────
    def _set_preset(self, p):
        self.canvas_var.set(p)
        for name, b in self._preset_btns.items():
            b.configure(fg_color=self.ACCENT if name == p else self.PANEL_2)

    def _set_bg_grad(self, idx, btn):
        self.bg_type = "gradient"
        self.bg_val  = GRADIENTS[idx]
        for b in self._bg_btns:
            b.configure(highlightbackground=self.BORDER)
        btn.configure(highlightbackground=self.ACCENT)
        self._request_render()

    def _set_bg_solid(self, col, btn):
        self.bg_type = "solid"
        self.bg_val  = col
        for b in self._bg_btns:
            b.configure(highlightbackground=self.BORDER)
        btn.configure(highlightbackground=self.ACCENT)
        self._request_render()

    # ── settings dict (single source of truth for render+export)
    def _settings(self):
        smooth = float(self.smooth_var.get())
        # Smoothness 0 → spring bypassed entirely (exact cursor follow).
        # Above ~0.05 we map onto FocuSee-style spring constants:
        #   0.05 → k≈452 (snappy), 1.0 → k=120 (very gentle).
        # Damping is always 0.92 of critical so the cursor catches up
        # without ringing.
        if smooth < 0.05:
            k = 0.0
            b = 0.0
        else:
            k = 470.0 - smooth * 350.0
            b = 2.0 * math.sqrt(k * 3.0) * 0.92
        return {
            "fps":           int(self.fps_var.get()),
            "region":        {"left": 0, "top": 0,
                              "width": self.sw, "height": self.sh},
            "canvas_preset": self.canvas_var.get(),
            "padding":       int(self.padding_var.get()),
            "inset":         self._inset,
            "roundness":     int(self.roundness_var.get()),
            "shadow":        int(self.shadow_var.get()),
            "glass":         int(self.glass_var.get()),
            "bg_type":       self.bg_type,
            "bg_val":        self.bg_val,
            "cursor_size":   int(self.cursor_size_var.get()),
            "stiffness":     k / 1200.0,    # downstream multiplies back
            "damping":       b / 90.0,
            "cursor_tilt":   self._cursor_tilt,
            "show_cursor":   self.show_cursor_var.get(),
            "click_ripple":  self.ripple_var.get(),
            "auto_hide":     self.autohide_var.get(),
            "loop_cursor":   self.loop_cursor_var.get(),
            "auto_zoom":     self.autozoom_var.get(),
            "zoom_level":    float(self.zoomlevel_var.get()),
            "zoom_dur":      self._zoom_dur,
        }

    # ── Recording ──────────────────────────────────────────────
    def toggle_recording(self):
        if not self.recording:
            self.bundle_path = os.path.join(
                tempfile.gettempdir(),
                f"screensee_{int(time.time())}.screensee")
            self.recording = True
            self._rec_start = time.perf_counter()
            region = {"left": 0, "top": 0,
                      "width": self.sw, "height": self.sh}
            self.recorder = Recorder(fps=int(self.fps_var.get()))
            try:
                self.recorder.start(region, self.bundle_path)
            except RuntimeError as e:
                messagebox.showerror("Recorder", str(e))
                self.recording = False
                return
            self.welcome_btn.configure(text="■  Stop Recording",
                                        fg_color=self.DANGER,
                                        hover_color="#dc2626")
            self.welcome_status.configure(text="00:00.000")
            self._tick()
        else:
            self.recording = False
            self.recorder.stop()
            self.welcome_btn.configure(text="●  Start Recording",
                                        fg_color=self.OK,
                                        hover_color="#16a34a")
            self.welcome_status.configure(text="")

            # Validate that we actually captured something before we try
            # to load the bundle into the editor — otherwise we end up
            # opening a malformed raw.mkv and showing a black preview.
            frame_count = 0
            backend = "mss"
            try:
                with open(os.path.join(self.bundle_path,
                                       "meta.json")) as f:
                    meta = json.load(f)
                frame_count = meta.get("frame_count", 0)
                backend     = meta.get("backend", "mss")
            except Exception:
                pass
            if frame_count < 5:
                messagebox.showerror(
                    "Recording failed",
                    "No frames were captured.\n\n"
                    "Workarounds:\n"
                    "  • Update Windows to the latest build, or\n"
                    "  • pip uninstall windows-capture   (forces the "
                    "mss fallback — cursor will be in the footage but "
                    "recording will work).")
                self.bundle_path = None
                return
            # No dialog when WGC silently fell back to mss — on Windows
            # both paths produce cursor-free frames, so it's not a
            # downgrade as far as the cursor overlay is concerned. WGC
            # is just faster.
            self._show_editor()

    def _tick(self):
        if self.recording:
            e = time.perf_counter() - self._rec_start
            self.welcome_status.configure(
                text=f"{int(e//60):02d}:{int(e%60):02d}.{int((e%1)*1000):03d}")
            self.after(33, self._tick)

    def _new_recording(self):
        self._playing = False
        if self._preview_src:
            try: self._preview_src.release()
            except Exception: pass
            self._preview_src = None
        self.bundle_path = None
        self._meta = None
        self._track = None
        self._track_key = None
        self.welcome_btn.configure(text="●  Start Recording",
                                    fg_color=self.OK)
        self.welcome_status.configure(text="")
        self._show_welcome()

    # ── Preview / playback ────────────────────────────────────
    def _load_bundle_into_preview(self):
        if not self.bundle_path or not os.path.isdir(self.bundle_path):
            return
        if self._preview_src:
            try: self._preview_src.release()
            except Exception: pass
        raw = os.path.join(self.bundle_path, "raw.mkv")
        self._preview_src = cv2.VideoCapture(raw)
        if not self._preview_src.isOpened():
            messagebox.showerror("Preview", f"Failed to open {raw}")
            return
        try:
            with open(os.path.join(self.bundle_path, "meta.json")) as f:
                self._meta = json.load(f)
            with open(os.path.join(self.bundle_path, "events.json")) as f:
                self._meta_events = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            messagebox.showerror("Preview", f"Bad bundle: {e}")
            return
        self._fps_meta = self._meta["fps"]
        self._total = max(1, self._meta.get("frame_count") or 1)
        self._cur_frame = 0
        self.scrub.configure(to=max(1, self._total - 1))
        self._programmatic = True
        self.scrub_var.set(0)
        self._programmatic = False
        self._track_key = None
        self._request_render()

    def _request_render(self, recompute_track=True):
        if recompute_track:
            self._track_key = None
        if self._render_pending:
            return
        self._render_pending = True
        self.after(40, self._do_render)

    def _do_render(self):
        self._render_pending = False
        if (not self._editor_frame or not self._meta
                or self._preview_src is None):
            return
        s = self._settings()
        key = (s["padding"], s["roundness"], s["shadow"],
               s["bg_type"], str(s["bg_val"]),
               s["cursor_size"], s["stiffness"], s["damping"],
               s["click_ripple"], s["auto_zoom"], s["zoom_level"],
               s["zoom_dur"], s["canvas_preset"])
        if key != self._track_key:
            self._track = precompute_track(self._meta_events, self._meta, s)
            self._track_key = key
        self._render_current_frame()

    def _render_current_frame(self):
        if not self._track:
            return
        fi = max(0, min(self._cur_frame, self._total - 1, len(self._track) - 1))
        self._preview_src.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ok, frame = self._preview_src.read()
        if not ok:
            return
        s = self._settings()
        reg = self._meta["region"]
        sw, sh = reg["width"], reg["height"]
        cw, ch = canvas_dims(self._meta, s)

        canvas = render_frame(frame, self._track[fi], s, sw, sh, cw, ch)

        pw = max(160, self.prev_canvas.winfo_width() - 8)
        ph = max(120, self.prev_canvas.winfo_height() - 8)
        asp = cw / ch
        if asp > pw / ph:
            ow = pw; oh = max(1, int(pw / asp))
        else:
            oh = ph; ow = max(1, int(ph * asp))
        small = canvas.resize((ow, oh), Image.LANCZOS)
        self._tk_img = ImageTk.PhotoImage(small)
        self.prev_label.configure(image=self._tk_img, text="")

        cs = fi / self._fps_meta
        ts = self._total / self._fps_meta
        self.time_lbl.configure(
            text=f"{int(cs//60):02d}:{int(cs%60):02d} / "
                 f"{int(ts//60):02d}:{int(ts%60):02d}")

    def _on_scrub(self, v):
        if self._programmatic:
            return
        self._cur_frame = int(float(v))
        self._request_render(recompute_track=False)

    def _toggle_play(self):
        self._playing = not self._playing
        self.play_btn.configure(text="❚❚" if self._playing else "▶")
        if self._playing:
            self._tick_play()

    def _tick_play(self):
        if not self._playing:
            return
        if self._cur_frame >= self._total - 1:
            self._cur_frame = 0
        else:
            self._cur_frame += 1
        self._programmatic = True
        self.scrub_var.set(self._cur_frame)
        self._programmatic = False
        self._render_current_frame()
        # Preview cadence: target fps capped at 20 fps so PIL keeps up.
        delay = max(50, int(1000 / min(self._fps_meta, 20)))
        self.after(delay, self._tick_play)

    # ── Export ─────────────────────────────────────────────────
    def export_mp4(self):
        if not self.bundle_path or not os.path.isdir(self.bundle_path):
            messagebox.showinfo("No Recording", "Record first then export.")
            return
        out = filedialog.asksaveasfilename(
            defaultextension=".mp4",
            filetypes=[("MP4", "*.mp4")],
            initialfile="screensee.mp4")
        if not out:
            return
        s = self._settings()
        bundle = self.bundle_path

        def run():
            p = Processor(s, progress_cb=self._on_progress)
            ok = p.run(bundle, out)
            if ok:
                messagebox.showinfo("Exported!", f"Saved:\n{out}")
            else:
                messagebox.showerror(
                    "Error",
                    "Export failed. Check that FFmpeg is on PATH "
                    "and the bundle is valid.")
        threading.Thread(target=run, daemon=True).start()

    def _on_progress(self, pct, msg):
        if hasattr(self, "prog_var"):
            self.prog_var.set(pct / 100)
            self.status_var.set(msg)

    # ── close ─────────────────────────────────────────────────
    def _on_close(self):
        self._playing = False
        if self._preview_src:
            try: self._preview_src.release()
            except Exception: pass
        if self.recording:
            try: self.recorder.stop()
            except Exception: pass
        self.destroy()

# ============================================================
if __name__ == "__main__":
    app = App()
    app.mainloop()
