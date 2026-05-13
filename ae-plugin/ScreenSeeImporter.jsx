/*
 * ScreenSee Importer for After Effects
 *
 * Imports a .screensee bundle (raw.mkv + events.json + meta.json) produced
 * by screensee.py and builds a fully editable composition:
 *
 *   - Master comp "ScreenSee Edit" sized 1920x1080 by default
 *   - "Controls" null layer with sliders for every adjustable setting
 *     (padding, roundness, shadow, glass, bg colors + blur, cursor size,
 *     auto-hide, click ripples toggle, zoom, ...)
 *   - "Recording" footage layer with expression-driven scale, rounded-
 *     corner alpha matte, and drop shadow.
 *   - "Cursor" precomp containing the 5 SVG sprites; opacity is driven by
 *     a "Style Index" slider that holds CURSOR_<style> sample times so the
 *     overlay swaps between arrow / text / pointer / openhand / closedhand
 *     to match the live OS cursor at capture time.
 *   - Position keyframes from MOVE events, mapped from screen coords to
 *     canvas coords through the Recording layer's live scale + position.
 *   - One ripple shape layer per left-click, fading out over 0.45 s.
 *
 * Run from File > Scripts > Run Script File... and pick the .screensee
 * folder when prompted.
 */

(function () {
    if (parseFloat(app.version) < 13) {
        alert("ScreenSee Importer requires After Effects CC 2014 or newer.");
        return;
    }

    var SCRIPT_FILE = new File($.fileName);
    var SCRIPT_DIR  = SCRIPT_FILE.parent;
    var CURSORS_DIR = new Folder(SCRIPT_DIR.fsName + "/cursors");

    if (!CURSORS_DIR.exists) {
        alert("Could not find cursors/ next to the script. Expected:\n" +
              CURSORS_DIR.fsName);
        return;
    }

    var bundle = Folder.selectDialog(
        "Select a .screensee bundle (the folder containing raw.mkv).");
    if (!bundle) return;

    var rawPath    = new File(bundle.fsName + "/raw.mkv");
    var eventsPath = new File(bundle.fsName + "/events.json");
    var metaPath   = new File(bundle.fsName + "/meta.json");
    if (!rawPath.exists || !eventsPath.exists || !metaPath.exists) {
        alert("Selected folder is not a .screensee bundle.\n" +
              "Expected raw.mkv, events.json, meta.json inside:\n" +
              bundle.fsName);
        return;
    }

    function readText(f) {
        f.encoding = "UTF-8";
        f.open("r");
        var t = f.read();
        f.close();
        return t;
    }

    var meta, events;
    try {
        meta   = JSON.parse(readText(metaPath));
        events = JSON.parse(readText(eventsPath));
    } catch (e) {
        alert("Failed to parse bundle JSON: " + e.toString());
        return;
    }

    var sw  = meta.region.width;
    var sh  = meta.region.height;
    var lft = meta.region.left || 0;
    var top = meta.region.top  || 0;
    var fps = meta.fps;
    var frameCount = meta.frame_count || 0;
    var bundleDur  = (frameCount > 0 ? frameCount / fps : 30);

    // Cursor sprite mapping. Order matters — index is what the Style
    // Index slider holds.
    var CURSOR_FILES = [
        "cursor.svg",        // 0 arrow
        "textcursor.svg",    // 1 text
        "pointinghand.svg",  // 2 pointer
        "openhand.svg",      // 3 openhand
        "closedhand.svg"     // 4 closedhand
    ];
    var STYLE_TO_INDEX = {
        "arrow": 0, "text": 1, "pointer": 2,
        "openhand": 3, "closedhand": 4
    };
    // SVG hotspot (normalised 0..1) — sprite anchor point so the visible
    // tip lands on the recorded screen coordinate.
    var CURSOR_TIPS = [
        [0.30, 0.18], // arrow
        [0.50, 0.50], // text
        [0.45, 0.06], // pointer
        [0.50, 0.30], // openhand
        [0.50, 0.40]  // closedhand
    ];

    app.beginUndoGroup("Import ScreenSee bundle");
    try {
        var proj = app.project;
        if (!proj) {
            app.newProject();
            proj = app.project;
        }

        // ── Project folders ───────────────────────────────────────
        var rootFolder    = proj.items.addFolder("ScreenSee – " + bundle.name);
        var sourcesFolder = proj.items.addFolder("Sources");
        sourcesFolder.parentFolder = rootFolder;
        var cursorsFolder = proj.items.addFolder("Cursors");
        cursorsFolder.parentFolder = rootFolder;

        // ── Raw footage ───────────────────────────────────────────
        var rawItem = proj.importFile(new ImportOptions(rawPath));
        rawItem.parentFolder = sourcesFolder;
        var duration = rawItem.duration;
        if (!duration || duration <= 0) duration = bundleDur;

        // ── Cursor SVG imports ────────────────────────────────────
        var cursorItems = [];
        for (var i = 0; i < CURSOR_FILES.length; i++) {
            var f = new File(CURSORS_DIR.fsName + "/" + CURSOR_FILES[i]);
            if (!f.exists) {
                alert("Missing cursor sprite: " + f.fsName);
                throw new Error("missing cursor file");
            }
            var ci = proj.importFile(new ImportOptions(f));
            ci.parentFolder = cursorsFolder;
            // AE imports SVGs as continuously-rasterized footage; flag the
            // layer for it later.
            cursorItems.push(ci);
        }

        // ── Cursor sprite precomp ─────────────────────────────────
        // 128x128 canvas centred on the cursor hotspot. Each frame, the
        // Style Index slider picks which of the 5 sprites is visible.
        var SPRITE_BOX = 128;
        var cursorComp = proj.items.addComp(
            "Cursor Sprite", SPRITE_BOX, SPRITE_BOX, 1.0, duration, fps);
        cursorComp.parentFolder = rootFolder;

        var driver = cursorComp.layers.addNull();
        driver.name = "Style Driver";
        driver.guideLayer = true;
        var styleEffect = driver.Effects.addProperty("ADBE Slider Control");
        styleEffect.name = "Style Index";
        var styleSlider = styleEffect.property(1);

        // Build hold-interpolated keyframes from CURSOR_<style> events.
        var styleTimes = [];
        var styleVals  = [];
        for (var ei = 0; ei < events.length; ei++) {
            var ev = events[ei];
            if (ev.type && ev.type.indexOf("CURSOR_") === 0) {
                var nm = ev.type.substring("CURSOR_".length).toLowerCase();
                if (STYLE_TO_INDEX.hasOwnProperty(nm)) {
                    styleTimes.push(ev.t);
                    styleVals.push(STYLE_TO_INDEX[nm]);
                }
            }
        }
        if (styleTimes.length === 0) {
            // No samples (e.g. recorded on non-Windows) — pin to arrow.
            styleTimes = [0];
            styleVals  = [0];
        }
        styleSlider.setValuesAtTimes(styleTimes, styleVals);
        for (var ki = 1; ki <= styleSlider.numKeys; ki++) {
            styleSlider.setInterpolationTypeAtKey(
                ki,
                KeyframeInterpolationType.HOLD,
                KeyframeInterpolationType.HOLD);
        }

        // Add the 5 cursor sprite layers on top of the driver.
        for (var sIdx = 0; sIdx < cursorItems.length; sIdx++) {
            var cl = cursorComp.layers.add(cursorItems[sIdx]);
            cl.name = CURSOR_FILES[sIdx];
            // Continuously rasterise so SVG stays crisp at any scale.
            try { cl.collapseTransformation = true; } catch (e1) {}
            // Anchor on the hotspot so position == on-screen tip.
            var tip = CURSOR_TIPS[sIdx];
            var sourceW = cl.source.width  || 64;
            var sourceH = cl.source.height || 64;
            cl.anchorPoint.setValue([sourceW * tip[0], sourceH * tip[1]]);
            cl.position.setValue([SPRITE_BOX / 2, SPRITE_BOX / 2]);
            cl.opacity.expression =
                'var s = thisComp.layer("Style Driver")' +
                '.effect("Style Index")("Slider");\n' +
                'Math.round(s) === ' + sIdx + ' ? 100 : 0;';
        }

        // ── Master comp ───────────────────────────────────────────
        var canvasW = 1920;
        var canvasH = 1080;
        var masterComp = proj.items.addComp(
            "ScreenSee Edit", canvasW, canvasH, 1.0, duration, fps);
        masterComp.parentFolder = rootFolder;
        masterComp.bgColor = [0.06, 0.06, 0.10];

        // ── Controls null ─────────────────────────────────────────
        var ctrl = masterComp.layers.addNull();
        ctrl.name = "Controls";
        ctrl.guideLayer = true;
        ctrl.enabled = false;
        var fx = ctrl.Effects;

        function addSlider(name, val) {
            var e = fx.addProperty("ADBE Slider Control");
            e.name = name;
            e.property(1).setValue(val);
            return e;
        }
        function addColor(name, rgb) {
            var e = fx.addProperty("ADBE Color Control");
            e.name = name;
            e.property(1).setValue(rgb);
            return e;
        }
        function addCheck(name, on) {
            var e = fx.addProperty("ADBE Checkbox Control");
            e.name = name;
            e.property(1).setValue(on ? 1 : 0);
            return e;
        }

        addSlider("Padding",         60);
        addSlider("Roundness",       14);
        addSlider("Shadow",          70);
        addSlider("Glass Halo",      14);
        addSlider("Background Blur",  0);
        addColor ("BG Color A", [0.10, 0.16, 0.36]);
        addColor ("BG Color B", [0.55, 0.32, 0.85]);
        addSlider("Cursor Size",     36);
        addSlider("Cursor Smoothness", 0);
        addCheck ("Auto-hide Cursor", true);
        addCheck ("Click Ripples",    true);
        addCheck ("Auto Zoom",        false);
        addSlider("Zoom Level",      2.0);

        // ── Background (gradient + blur) ──────────────────────────
        var bg = masterComp.layers.addSolid(
            [0, 0, 0], "Background", canvasW, canvasH, 1.0, duration);
        var ramp = bg.Effects.addProperty("ADBE Ramp");
        ramp.property("Start of Ramp").setValue([canvasW / 2, 0]);
        ramp.property("End of Ramp").setValue([canvasW / 2, canvasH]);
        ramp.property("Start Color").expression =
            'thisComp.layer("Controls").effect("BG Color A")("Color")';
        ramp.property("End Color").expression =
            'thisComp.layer("Controls").effect("BG Color B")("Color")';
        var bgBlur = bg.Effects.addProperty("ADBE Gaussian Blur 2");
        bgBlur.property(1).expression =
            'thisComp.layer("Controls").effect("Background Blur")("Slider")';

        // ── Glass halo (under recording) ──────────────────────────
        // Soft white plate behind the recording, scale follows recording.
        var glass = masterComp.layers.addShape();
        glass.name = "Glass Halo";
        var glassRoot = glass.property("ADBE Root Vectors Group");
        var glassGrp  = glassRoot.addProperty("ADBE Vector Group");
        glassGrp.name = "Halo";
        var glassContents = glassGrp.property("ADBE Vectors Group");
        var glassRect = glassContents.addProperty("ADBE Vector Shape - Rect");
        glassRect.property("Size").expression =
            'var pad = thisComp.layer("Controls").effect("Padding")("Slider");\n' +
            'var halo = thisComp.layer("Controls").effect("Glass Halo")("Slider");\n' +
            'var aw = thisComp.width  - pad*2;\n' +
            'var ah = thisComp.height - pad*2;\n' +
            'var s  = Math.min(aw / ' + sw + ', ah / ' + sh + ');\n' +
            '[' + sw + ' * s + halo*2, ' + sh + ' * s + halo*2];';
        glassRect.property("Roundness").expression =
            'var r = thisComp.layer("Controls").effect("Roundness")("Slider");\n' +
            'var halo = thisComp.layer("Controls").effect("Glass Halo")("Slider");\n' +
            'r + halo / 2;';
        var glassFill = glassContents.addProperty("ADBE Vector Graphic - Fill");
        glassFill.property("Color").setValue([1, 1, 1]);
        glassFill.property("Opacity").setValue(14);
        glass.position.setValue([canvasW / 2, canvasH / 2]);
        glass.opacity.expression =
            'thisComp.layer("Controls").effect("Glass Halo")("Slider") > 0 ? 100 : 0;';
        var glassBlur = glass.Effects.addProperty("ADBE Gaussian Blur 2");
        glassBlur.property(1).expression =
            'thisComp.layer("Controls").effect("Glass Halo")("Slider") / 2;';

        // ── Recording layer ───────────────────────────────────────
        var rec = masterComp.layers.add(rawItem);
        rec.name = "Recording";
        rec.position.setValue([canvasW / 2, canvasH / 2]);
        rec.scale.expression =
            'var pad = thisComp.layer("Controls").effect("Padding")("Slider");\n' +
            'var aw = thisComp.width  - pad*2;\n' +
            'var ah = thisComp.height - pad*2;\n' +
            'var s  = Math.min(aw / ' + sw + ', ah / ' + sh + ') * 100;\n' +
            'var zoomOn = thisComp.layer("Controls").effect("Auto Zoom")("Checkbox");\n' +
            'var z      = thisComp.layer("Controls").effect("Zoom Level")("Slider");\n' +
            'zoomOn > 0.5 ? [s*z, s*z] : [s, s];';

        var ds = rec.Effects.addProperty("ADBE Drop Shadow");
        ds.property("Opacity").expression =
            'Math.min(255, thisComp.layer("Controls").effect("Shadow")("Slider") * 2.5);';
        ds.property("Distance").expression =
            'thisComp.layer("Controls").effect("Shadow")("Slider") / 8;';
        ds.property("Softness").expression =
            'thisComp.layer("Controls").effect("Shadow")("Slider") / 3;';
        ds.property("Direction").setValue(180);

        // ── Rounded corner mask (alpha matte for Recording) ──────
        var mask = masterComp.layers.addShape();
        mask.name = "Recording Mask";
        var maskRoot = mask.property("ADBE Root Vectors Group");
        var maskGrp  = maskRoot.addProperty("ADBE Vector Group");
        maskGrp.name = "Mask";
        var maskContents = maskGrp.property("ADBE Vectors Group");
        var maskRect = maskContents.addProperty("ADBE Vector Shape - Rect");
        maskRect.property("Size").expression =
            'var rec = thisComp.layer("Recording");\n' +
            'var s = rec.transform.scale[0] / 100;\n' +
            '[' + sw + ' * s, ' + sh + ' * s];';
        maskRect.property("Roundness").expression =
            'thisComp.layer("Controls").effect("Roundness")("Slider");';
        var maskFill = maskContents.addProperty("ADBE Vector Graphic - Fill");
        maskFill.property("Color").setValue([1, 1, 1]);
        mask.position.expression =
            'var rec = thisComp.layer("Recording");\n' +
            'rec.transform.position;';
        // Move mask immediately above Recording so trkmat can target it.
        mask.moveBefore(rec);
        rec.trackMatteType = TrackMatteType.ALPHA;

        // ── Cursor layer ─────────────────────────────────────────
        var cursor = masterComp.layers.add(cursorComp);
        cursor.name = "Cursor";
        cursor.collapseTransformation = true;
        cursor.scale.expression =
            'var sz = thisComp.layer("Controls").effect("Cursor Size")("Slider");\n' +
            'var s  = sz / 36 * 100;\n' +
            '[s, s];';

        // Bake MOVE events as Point Control keyframes (in screen coords).
        var pos = cursor.Effects.addProperty("ADBE Point Control");
        pos.name = "Screen Pos";
        var posProp = pos.property(1);
        var pTimes = [], pVals = [];
        var lastX = sw / 2, lastY = sh / 2;
        for (var mi = 0; mi < events.length; mi++) {
            var em = events[mi];
            if (em.type === "MOVE") {
                lastX = em.x - lft;
                lastY = em.y - top;
                pTimes.push(em.t);
                pVals.push([lastX, lastY]);
            }
        }
        if (pTimes.length === 0) {
            pTimes = [0];
            pVals  = [[sw / 2, sh / 2]];
        }
        posProp.setValuesAtTimes(pTimes, pVals);

        // Cursor canvas position = recording top-left + screen pos * scale.
        // Smoothness slider linearly interpolates between the raw value and
        // a smoothMove() over a 0.1..0.6 s window.
        cursor.position.expression =
            'var rec = thisComp.layer("Recording");\n' +
            'var s   = rec.transform.scale[0] / 100;\n' +
            'var rp  = rec.transform.position;\n' +
            'var rw  = ' + sw + ' * s;\n' +
            'var rh  = ' + sh + ' * s;\n' +
            'var smo = thisComp.layer("Controls").effect("Cursor Smoothness")("Slider");\n' +
            'var raw = effect("Screen Pos")("Point");\n' +
            'var p   = raw;\n' +
            'if (smo > 0.01) {\n' +
            '  var win = 0.1 + smo * 0.5;\n' +
            '  var sm  = effect("Screen Pos")("Point").smooth(win, 5);\n' +
            '  p = [raw[0]*(1-smo) + sm[0]*smo, raw[1]*(1-smo) + sm[1]*smo];\n' +
            '}\n' +
            '[rp[0] - rw/2 + p[0]*s, rp[1] - rh/2 + p[1]*s];';

        // Auto-hide: drop opacity when the cursor is idle (~0 px/s).
        cursor.opacity.expression =
            'var auto = thisComp.layer("Controls").effect("Auto-hide Cursor")("Checkbox");\n' +
            'if (auto < 0.5) {\n' +
            '  100;\n' +
            '} else {\n' +
            '  var dt = 0.25;\n' +
            '  var p1 = effect("Screen Pos")("Point");\n' +
            '  var p0 = effect("Screen Pos").param("Point").valueAtTime(time - dt);\n' +
            '  var spd = length(p1, p0) / dt;\n' +
            '  spd > 1 ? 100 : 0;\n' +
            '}';

        // ── Click ripples ────────────────────────────────────────
        // One shape layer per left click; opacity + scale animate from
        // an in-point set to the click time.
        var clicks = [];
        for (var ci3 = 0; ci3 < events.length; ci3++) {
            if (events[ci3].type === "CLICK_L") {
                clicks.push(events[ci3]);
            }
        }
        var ripFolder = null;
        for (var ck = 0; ck < clicks.length; ck++) {
            var cev = clicks[ck];
            var sx  = cev.x - lft;
            var sy  = cev.y - top;
            var rip = masterComp.layers.addShape();
            rip.name = "Ripple " + (ck + 1);
            try { rip.startTime = cev.t; } catch (e2) {}
            rip.inPoint  = cev.t;
            rip.outPoint = cev.t + 0.45;

            var rRoot = rip.property("ADBE Root Vectors Group");
            var rGrp  = rRoot.addProperty("ADBE Vector Group");
            rGrp.name = "Ripple";
            var rContents = rGrp.property("ADBE Vectors Group");
            var ell = rContents.addProperty("ADBE Vector Shape - Ellipse");
            ell.property("Size").expression =
                'var t = time - thisLayer.inPoint;\n' +
                'var p = Math.min(1, Math.max(0, t / 0.45));\n' +
                'var ease = 1 - Math.pow(1 - p, 2);\n' +
                'var r = ease * 110;\n' +
                '[r, r];';
            var stroke = rContents.addProperty("ADBE Vector Graphic - Stroke");
            stroke.property("Color").setValue([0.39, 0.59, 1.0]);
            stroke.property("Stroke Width").setValue(2);

            // Position the ripple at the click point in canvas space.
            rip.position.expression =
                'var rec = thisComp.layer("Recording");\n' +
                'var s   = rec.transform.scale[0] / 100;\n' +
                'var rp  = rec.transform.position;\n' +
                'var rw  = ' + sw + ' * s;\n' +
                'var rh  = ' + sh + ' * s;\n' +
                '[rp[0] - rw/2 + (' + sx + ') * s, rp[1] - rh/2 + (' + sy + ') * s];';

            rip.opacity.expression =
                'var on = thisComp.layer("Controls").effect("Click Ripples")("Checkbox");\n' +
                'if (on < 0.5) 0\n' +
                'else {\n' +
                '  var t = time - thisLayer.inPoint;\n' +
                '  var p = Math.min(1, Math.max(0, t / 0.45));\n' +
                '  (1 - p) * 100;\n' +
                '}';
        }

        // ── Layer order (bottom-up): bg, glass, mask, recording, ripples, cursor ──
        cursor.moveToBeginning();

        // ── Open & report ─────────────────────────────────────────
        masterComp.openInViewer();
        alert("Imported " + bundle.name + "\n" +
              "  duration: " + duration.toFixed(2) + " s\n" +
              "  cursor samples: " + styleTimes.length + "\n" +
              "  move events: " + pTimes.length + "\n" +
              "  clicks: " + clicks.length + "\n\n" +
              "Tweak the 'Controls' null layer's effects to adjust look.");
    } catch (err) {
        alert("Import failed: " + err.toString() +
              (err.stack ? ("\n\n" + err.stack) : ""));
    }
    app.endUndoGroup();
})();
