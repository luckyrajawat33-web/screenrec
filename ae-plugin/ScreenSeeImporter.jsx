/*
 * ScreenSee Importer for After Effects
 *
 * Imports a .screensee bundle (raw.mkv + events.json + meta.json) from
 * screensee.py and builds an editable composition with cursor shapes
 * baked from Cursor_Sprite.json (Bodymovin/Lottie export of the 5 cursor
 * sprites — no SVG dependency anymore).
 *
 * Target structure
 * ─────────────────
 * MAIN COMP "ScreenSee Edit"  (1920x1080 by default)
 *   ├── Recording Matte       (shape, rounded rect — fixed size, depends only on Padding/Roundness)
 *   ├── Recording             (precomp instance, alpha matte = Recording Matte;
 *   │                          Scale + Position driven by Auto Zoom / Zoom Level / Zoom Position)
 *   ├── Glass Halo            (soft white plate under Recording)
 *   ├── Background            (solid + Ramp gradient + Gaussian Blur)
 *   └── Controls              (null, guide, disabled — holds every knob)
 *
 * RECORDING PRECOMP "Recording"  (source-resolution canvas, e.g. 2560x1440)
 *   ├── Arrow Cursor          (shape, opacity expr: Style Index === 0)
 *   ├── textcursor            (shape, opacity expr: Style Index === 1)
 *   ├── Pointinghand Cursor   (shape, opacity expr: Style Index === 2)
 *   ├── Openhand Cursor       (shape, opacity expr: Style Index === 3)
 *   ├── Closedhand Cursor     (shape, opacity expr: Style Index === 4)
 *   ├── Cursor Position Null  (parent for the 5 cursors; Position from Screen Pos + smoothing)
 *   ├── Style Driver          (null with Slider Control "Style Index" — hold-keyed from CURSOR_<style> events)
 *   ├── Screen Pos            (null with Point Control "Screen Pos" — linear-keyed from MOVE events)
 *   └── Screen Recording Footage  (raw.mkv)
 *
 * All Controls live in the MAIN comp. Expressions inside the Recording
 * precomp reach across via `comp(MASTER_COMP_NAME).layer("Controls")`.
 */

(function () {
    if (parseFloat(app.version) < 13) {
        alert("ScreenSee Importer requires After Effects CC 2014 or newer.");
        return;
    }

    // ── Master comp name; baked into cross-comp expressions ──────
    var MASTER_COMP_NAME    = "ScreenSee Edit";
    var RECORDING_COMP_NAME = "Recording";

    var SCRIPT_FILE = new File($.fileName);
    var SCRIPT_DIR  = SCRIPT_FILE.parent;
    var CURSOR_JSON = new File(SCRIPT_DIR.fsName + "/Cursor_Sprite.json");

    if (!CURSOR_JSON.exists) {
        alert("Missing Cursor_Sprite.json next to the script:\n" +
              CURSOR_JSON.fsName);
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

    var meta, events, cursorJson;
    try {
        meta       = JSON.parse(readText(metaPath));
        events     = JSON.parse(readText(eventsPath));
        cursorJson = JSON.parse(readText(CURSOR_JSON));
    } catch (e) {
        alert("Failed to parse JSON: " + e.toString());
        return;
    }

    var sw  = meta.region.width;
    var sh  = meta.region.height;
    var lft = meta.region.left || 0;
    var topY = meta.region.top || 0;
    var fps = meta.fps;
    var frameCount = meta.frame_count || 0;
    var bundleDur  = (frameCount > 0 ? frameCount / fps : 30);

    // ── Style mapping. Style Index 0 is reserved for "no cursor"; the
    //    five sprites occupy 1–5. Cursor_Sprite.json's embedded opacity
    //    expressions still use 0-based indices (0–4), so the importer
    //    shifts them by +1 when wiring up the AE opacity expressions.
    var STYLE_TO_INDEX = {
        "arrow":      1,
        "text":       2,
        "pointer":    3,
        "openhand":   4,
        "closedhand": 5
    };

    // ── Lottie → AE shape-layer builder ─────────────────────────
    // Walks Cursor_Sprite.json shape items and reconstructs them as native
    // AE shape paths so the file is self-contained (no Bodymovin runtime).
    function makeShape(lottieSh) {
        var k = lottieSh.ks.k;
        var sh = new Shape();
        sh.vertices    = k.v;
        sh.inTangents  = k.i;
        sh.outTangents = k.o;
        sh.closed      = !!k.c;
        return sh;
    }

    function addShapeItem(parentContents, item) {
        var t = item.ty;
        if (t === "gr") {
            var grp = parentContents.addProperty("ADBE Vector Group");
            grp.name = item.nm || "Group";
            var inner = grp.property("ADBE Vectors Group");
            for (var i = 0; i < item.it.length; i++) {
                var sub = item.it[i];
                if (sub.ty === "tr") {
                    // The "tr" sibling configures the group's own transform.
                    var grpTr = grp.property("ADBE Vector Transform Group");
                    if (sub.p && grpTr.property("Position"))
                        grpTr.property("Position").setValue([sub.p.k[0], sub.p.k[1]]);
                    if (sub.a && grpTr.property("Anchor Point"))
                        grpTr.property("Anchor Point").setValue([sub.a.k[0], sub.a.k[1]]);
                    if (sub.s && grpTr.property("Scale"))
                        grpTr.property("Scale").setValue([sub.s.k[0], sub.s.k[1]]);
                    if (sub.r && grpTr.property("Rotation") && typeof sub.r.k === "number")
                        grpTr.property("Rotation").setValue(sub.r.k);
                    if (sub.o && grpTr.property("Opacity") && typeof sub.o.k === "number")
                        grpTr.property("Opacity").setValue(sub.o.k);
                } else {
                    addShapeItem(inner, sub);
                }
            }
            return grp;
        } else if (t === "sh") {
            var pathProp = parentContents.addProperty("ADBE Vector Shape - Group");
            pathProp.name = item.nm || "Path";
            pathProp.property("Path").setValue(makeShape(item));
            return pathProp;
        } else if (t === "fl") {
            var fill = parentContents.addProperty("ADBE Vector Graphic - Fill");
            if (item.c && item.c.k) {
                var fc = item.c.k;
                fill.property("Color").setValue([fc[0], fc[1], fc[2]]);
            }
            if (item.o && typeof item.o.k === "number") {
                fill.property("Opacity").setValue(item.o.k);
            }
            return fill;
        } else if (t === "st") {
            var stroke = parentContents.addProperty("ADBE Vector Graphic - Stroke");
            if (item.c && item.c.k) {
                var sc = item.c.k;
                stroke.property("Color").setValue([sc[0], sc[1], sc[2]]);
            }
            if (item.w && typeof item.w.k === "number") {
                stroke.property("Stroke Width").setValue(item.w.k);
            }
            return stroke;
        }
        return null;
    }

    function buildCursorLayer(comp, lottieLayer, opacityExpr, parentLayer) {
        var shapeLyr = comp.layers.addShape();
        shapeLyr.name = lottieLayer.nm;
        var ks = lottieLayer.ks;
        if (ks.a && ks.a.k)
            shapeLyr.transform.anchorPoint.setValue([ks.a.k[0], ks.a.k[1]]);
        // Scale = Lottie's intrinsic scale × (Cursor Size / 500). The
        // Lottie file ships sprites at 1343.7% so the artwork fills its
        // 500-unit canvas; dividing the slider by 500 makes "Cursor
        // Size = 500" identical to the sprite's natural render size.
        var lx = (ks.s && ks.s.k) ? ks.s.k[0] : 100;
        var ly = (ks.s && ks.s.k) ? ks.s.k[1] : 100;
        shapeLyr.transform.scale.expression =
            'var sz = thisComp.layer("Style Driver").effect("Cursor Size")("Slider");\n' +
            'var k = sz / 500;\n' +
            '[' + lx + ' * k, ' + ly + ' * k];';
        if (ks.r && typeof ks.r.k === "number")
            shapeLyr.transform.rotation.setValue(ks.r.k);
        shapeLyr.transform.opacity.expression = opacityExpr;

        var contents = shapeLyr.property("ADBE Root Vectors Group");
        for (var si = 0; si < lottieLayer.shapes.length; si++) {
            addShapeItem(contents, lottieLayer.shapes[si]);
        }

        // Wire the cursor to the Cursor Position Null by expression
        // rather than parenting. Parenting works in the UI but the
        // scripted version is flaky across AE versions (returns
        // "Object is invalid" on .parent assignment when the source
        // layer was added in the same script run). An expression on
        // Position avoids the issue entirely AND survives the user
        // re-parenting later if they prefer.
        if (parentLayer) {
            shapeLyr.transform.position.expression =
                'thisComp.layer("' + parentLayer.name + '").transform.position;';
        } else {
            shapeLyr.transform.position.setValue([0, 0]);
        }
        return shapeLyr;
    }

    // ── Expression strings (master-comp name baked in) ──────────
    function masterCtrl(effectName, prop) {
        return 'comp("' + MASTER_COMP_NAME + '").layer("Controls").effect("' +
               effectName + '")("' + prop + '")';
    }
    function thisCompCtrl(effectName, prop) {
        return 'thisComp.layer("Controls").effect("' + effectName + '")("' +
               prop + '")';
    }

    app.beginUndoGroup("Import ScreenSee bundle");
    try {
        var proj = app.project;
        if (!proj) {
            app.newProject();
            proj = app.project;
        }

        // ── Project folders ─────────────────────────────────────
        var rootFolder    = proj.items.addFolder("ScreenSee – " + bundle.name);
        var sourcesFolder = proj.items.addFolder("Sources");
        sourcesFolder.parentFolder = rootFolder;

        // ── Raw footage ─────────────────────────────────────────
        var rawItem = proj.importFile(new ImportOptions(rawPath));
        rawItem.parentFolder = sourcesFolder;
        var duration = rawItem.duration;
        if (!duration || duration <= 0) duration = bundleDur;

        // ════════════════════════════════════════════════════════
        // RECORDING PRECOMP  (sized to the *encoded* footage, which can
        // differ from meta.json's region by a pixel or two when ffmpeg
        // rounds to even dimensions — using the real footage size keeps
        // the cursor overlay pixel-aligned with the screen capture).
        // ════════════════════════════════════════════════════════
        var footW = rawItem.width  || sw;
        var footH = rawItem.height || sh;
        // MOVE events are in capture-region pixels; scale them into the
        // footage's pixel space so the cursor lands on the right pixel
        // even if the two resolutions differ.
        var coordSX = footW / sw;
        var coordSY = footH / sh;
        var recComp = proj.items.addComp(
            RECORDING_COMP_NAME, footW, footH, 1.0, duration, fps);
        recComp.parentFolder = rootFolder;

        // ── Footage layer (bottom) ──────────────────────────────
        var footage = recComp.layers.add(rawItem);
        footage.name = "Screen Recording Footage";
        footage.transform.position.setValue([footW / 2, footH / 2]);

        // ── Screen Pos null with Point Control, keyed from MOVE events
        var screenPosLayer = recComp.layers.addNull(duration);
        screenPosLayer.name = "Screen Pos";
        screenPosLayer.guideLayer = true;
        screenPosLayer.enabled = false;
        var screenPosEff = screenPosLayer.Effects.addProperty("ADBE Point Control");
        screenPosEff.name = "Screen Pos";
        var screenPosProp = screenPosEff.property(1);

        var pTimes = [], pVals = [];
        var lastX = footW / 2, lastY = footH / 2;
        for (var mi = 0; mi < events.length; mi++) {
            var em = events[mi];
            if (em.type === "MOVE") {
                lastX = (em.x - lft) * coordSX;
                lastY = (em.y - topY) * coordSY;
                pTimes.push(em.t);
                pVals.push([lastX, lastY]);
            }
        }
        if (pTimes.length === 0) {
            pTimes = [0];
            pVals  = [[footW / 2, footH / 2]];
        }
        screenPosProp.setValuesAtTimes(pTimes, pVals);

        // ── Style Driver null. Holds Style Index (hold-keyed from
        //    CURSOR_<style> events) plus Cursor Size and Cursor
        //    Smoothness — these belong with the cursor sprites, not in
        //    the main comp's Controls null.
        var styleLayer = recComp.layers.addNull(duration);
        styleLayer.name = "Style Driver";
        styleLayer.guideLayer = true;
        styleLayer.enabled = false;
        // Add all three sliders FIRST, then re-acquire fresh property
        // references before doing anything (keyframe writes especially)
        // — keeping a Property handle across subsequent addProperty()
        // calls invalidates it in some AE versions and surfaces as
        // "ReferenceError: Object is invalid" on the first setValue.
        styleLayer.Effects.addProperty("ADBE Slider Control").name = "Style Index";
        styleLayer.Effects.addProperty("ADBE Slider Control").name = "Cursor Size";
        styleLayer.Effects.addProperty("ADBE Slider Control").name = "Cursor Smoothness";
        styleLayer.Effects.property("Cursor Size").property(1).setValue(200);
        styleLayer.Effects.property("Cursor Smoothness").property(1).setValue(0);
        var styleProp = styleLayer.Effects.property("Style Index").property(1);

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
            // No CURSOR_<style> samples (e.g. non-Windows recording) —
            // fall back to the plain arrow (index 1) so something shows.
            styleTimes = [0];
            styleVals  = [1];
        }
        styleProp.setValuesAtTimes(styleTimes, styleVals);
        for (var ki = 1; ki <= styleProp.numKeys; ki++) {
            styleProp.setInterpolationTypeAtKey(
                ki,
                KeyframeInterpolationType.HOLD,
                KeyframeInterpolationType.HOLD);
        }
        // Snap the slider to whole numbers. Keyframe values are already
        // integers; this also forces any value the user types/drags to
        // the nearest level (0 = hidden, 1–5 = the five sprites), so the
        // Effect Controls panel never shows 0.5 / 2.8 etc.
        styleProp.expression = "Math.round(value);";

        // ── Cursor Position Null (parent for the cursor shapes) ──
        //    Position, Size, and Smoothness all read from Style Driver
        //    in this same precomp — no cross-comp reference needed.
        var cursorNull = recComp.layers.addNull(duration);
        cursorNull.name = "Cursor Position Null";
        cursorNull.transform.anchorPoint.setValue([0, 0]);
        cursorNull.transform.position.expression =
            'var smo = thisComp.layer("Style Driver").effect("Cursor Smoothness")("Slider");\n' +
            'var sp  = thisComp.layer("Screen Pos");\n' +
            'var raw = sp.effect("Screen Pos")("Point");\n' +
            'var p   = raw;\n' +
            'if (smo > 0.01) {\n' +
            '    var win = 0.1 + smo * 0.5;\n' +
            '    var sm  = sp.effect("Screen Pos")("Point").smooth(win, 5);\n' +
            '    p = [raw[0]*(1-smo) + sm[0]*smo, raw[1]*(1-smo) + sm[1]*smo];\n' +
            '}\n' +
            'p;';
        // Scale is left at default; the cursors expression-link their
        // Position to this null but compute Scale from Cursor Size on
        // Style Driver directly. This avoids a chained dependency and
        // keeps the null purely as a position anchor.

        // ── 5 cursor shape layers from Cursor_Sprite.json ────────
        // Lottie file lists them top-to-bottom in this order:
        //   Closedhand (idx 4), Openhand (idx 3), Pointinghand (idx 2),
        //   textcursor (idx 1), Arrow (idx 0).
        // We add the lowest-index last so Arrow ends up at the top of the
        // layer stack (visually first in the timeline).
        var cursorLayers = [];
        // First, gather Lottie layers by index from STYLE_TO_INDEX so we
        // don't rely on the file's array order.
        var byIndex = {};
        for (var li = 0; li < cursorJson.layers.length; li++) {
            var L = cursorJson.layers[li];
            // The bodymovin opacity expression encodes the index, e.g.
            // "...Math.round(s) === 4 ? 100 : 0...". Parse it back so the
            // mapping survives reorderings of the file.
            var ox = L.ks && L.ks.o && L.ks.o.x ? L.ks.o.x : "";
            // Pull the index out of the embedded Bodymovin opacity expr
            // (e.g. "Math.round(s) === 4 ? 100 : 0"). RegExp() avoids the
            // ExtendScript parser confusing "/===" with the /= operator.
            var m = ox.match(new RegExp("===\\s*(\\d+)"));
            var idx = m ? parseInt(m[1], 10) : null;
            if (idx !== null && idx >= 0 && idx <= 4) {
                byIndex[idx] = L;
            }
        }
        // Add in reverse so the arrow ends up on top. The JSON encodes
        // 0-based indices (0–4); Style Index uses 1–5 with 0 = hidden,
        // so the opacity expression checks idx + 1.
        for (var idx = 4; idx >= 0; idx--) {
            var lLayer = byIndex[idx];
            if (!lLayer) continue;
            var aeIdx = idx + 1;
            var opacityExpr =
                'var s = thisComp.layer("Style Driver").effect("Style Index")("Slider");\n' +
                'Math.round(s) === ' + aeIdx + ' ? 100 : 0;';
            var aeLyr = buildCursorLayer(recComp, lLayer, opacityExpr, cursorNull);
            cursorLayers.push(aeLyr);
        }

        // ════════════════════════════════════════════════════════
        // MAIN COMP
        // ════════════════════════════════════════════════════════
        var canvasW = 1920;
        var canvasH = 1080;
        var masterComp = proj.items.addComp(
            MASTER_COMP_NAME, canvasW, canvasH, 1.0, duration, fps);
        masterComp.parentFolder = rootFolder;
        masterComp.bgColor = [0.06, 0.06, 0.10];

        // ── Controls null (will be at bottom; build first) ──────
        var ctrl = masterComp.layers.addNull();
        ctrl.name = "Controls";
        ctrl.guideLayer = true;
        ctrl.enabled = false;
        var fx = ctrl.Effects;

        function addSlider(name, val) {
            var e = fx.addProperty("ADBE Slider Control");
            e.name = name;
            e.property(1).setValue(val);
        }
        function addPoint(name, xy) {
            var e = fx.addProperty("ADBE Point Control");
            e.name = name;
            e.property(1).setValue(xy);
        }
        function addColor(name, rgb) {
            var e = fx.addProperty("ADBE Color Control");
            e.name = name;
            e.property(1).setValue(rgb);
        }
        function addCheck(name, on) {
            var e = fx.addProperty("ADBE Checkbox Control");
            e.name = name;
            e.property(1).setValue(on ? 1 : 0);
        }

        addSlider("Padding",         60);
        addSlider("Roundness",       14);
        addSlider("Shadow",          70);
        addSlider("Glass Halo",      14);
        addSlider("Zoom Level",      1.0);
        addPoint ("Zoom Position",   [0.5, 0.5]);
        addColor ("BG Color A", [0.10, 0.16, 0.36]);
        addColor ("BG Color B", [0.55, 0.32, 0.85]);
        // Keep Zoom Level inside 1.0–2.0 at the slider itself, so the
        // Effect Controls panel reflects the clamp the moment the user
        // overshoots (the Recording scale expression also clamps, but
        // this gives immediate visual feedback).
        fx.property("Zoom Level").property(1).expression =
            "Math.max(1, Math.min(2, value));";

        // Shared expression fragment: computes the recording's *fit*
        // size — what it would render at with Zoom Level 1 — leaving
        // `fit`, `srcW`, `srcH` in scope. The matte, the shadow shape,
        // and the glass halo all derive their size from this so they
        // stay locked to one another and never react to zoom.
        var FIT_SIZE_EXPR =
            'var pad = ' + thisCompCtrl("Padding", "Slider") + ';\n' +
            'var rec = thisComp.layer("Recording");\n' +
            'var srcW = rec.source.width;\n' +
            'var srcH = rec.source.height;\n' +
            'var frameW = thisComp.width  - pad*2;\n' +
            'var frameH = thisComp.height - pad*2;\n' +
            'var fit = Math.min(frameW / srcW, frameH / srcH);\n';

        // ── Background (solid + Ramp + blur) ────────────────────
        var bg = masterComp.layers.addSolid(
            [0, 0, 0], "Background", canvasW, canvasH, 1.0, duration);
        var ramp = bg.Effects.addProperty("ADBE Ramp");
        ramp.property("Start of Ramp").setValue([canvasW / 2, 0]);
        ramp.property("End of Ramp").setValue([canvasW / 2, canvasH]);
        ramp.property("Start Color").expression =
            thisCompCtrl("BG Color A", "Color") + ';';
        ramp.property("End Color").expression =
            thisCompCtrl("BG Color B", "Color") + ';';

        // ── Glass Halo (soft white plate under the recording) ────
        // Size = recording FIT size (zoom-independent) + halo on every
        // edge; position is the fixed comp centre. The halo never
        // reacts to Zoom Level or Zoom Position — only the footage
        // precomp zooms.
        var glass = masterComp.layers.addShape();
        glass.name = "Glass Halo";
        var glassRoot = glass.property("ADBE Root Vectors Group");
        var glassGrp  = glassRoot.addProperty("ADBE Vector Group");
        glassGrp.name = "Halo";
        var glassContents = glassGrp.property("ADBE Vectors Group");
        var glassRect = glassContents.addProperty("ADBE Vector Shape - Rect");
        glassRect.property("Size").expression =
            FIT_SIZE_EXPR +
            'var halo = ' + thisCompCtrl("Glass Halo", "Slider") + ';\n' +
            '[srcW * fit + halo*2, srcH * fit + halo*2];';
        glassRect.property("Roundness").expression =
            'var r    = ' + thisCompCtrl("Roundness", "Slider") + ';\n' +
            'var halo = ' + thisCompCtrl("Glass Halo", "Slider") + ';\n' +
            'r + halo;';
        var glassFill = glassContents.addProperty("ADBE Vector Graphic - Fill");
        glassFill.property("Color").setValue([1, 1, 1]);
        glassFill.property("Opacity").setValue(14);
        glass.transform.position.setValue([canvasW / 2, canvasH / 2]);
        glass.transform.opacity.expression =
            'var halo = ' + thisCompCtrl("Glass Halo", "Slider") + ';\n' +
            'halo > 0 ? 100 : 0;';

        // ── Recording Shadow (cast behind the recording) ─────────
        // A rounded rectangle the exact size of the matte. The Drop
        // Shadow effect runs in Shadow-Only mode with Distance 0, so
        // the shadow sits dead-centre behind the recording and only
        // its blurred edge peeks out past the matte. The Shadow slider
        // drives Softness alone — a real, visible shadow that the
        // alpha matte can't clip (because this layer isn't matted).
        var shadowLyr = masterComp.layers.addShape();
        shadowLyr.name = "Recording Shadow";
        var shRoot = shadowLyr.property("ADBE Root Vectors Group");
        var shGrp  = shRoot.addProperty("ADBE Vector Group");
        shGrp.name = "Shadow Plate";
        var shContents = shGrp.property("ADBE Vectors Group");
        var shRect = shContents.addProperty("ADBE Vector Shape - Rect");
        shRect.property("Size").expression =
            FIT_SIZE_EXPR + '[srcW * fit, srcH * fit];';
        shRect.property("Roundness").expression =
            thisCompCtrl("Roundness", "Slider") + ';';
        var shFill = shContents.addProperty("ADBE Vector Graphic - Fill");
        shFill.property("Color").setValue([0, 0, 0]);
        shadowLyr.transform.position.setValue([canvasW / 2, canvasH / 2]);
        var sds = shadowLyr.Effects.addProperty("ADBE Drop Shadow");
        sds.property("Shadow Color").setValue([0, 0, 0]);
        sds.property("Opacity").setValue(190);
        sds.property("Direction").setValue(0);
        sds.property("Distance").setValue(0);
        sds.property("Softness").expression =
            thisCompCtrl("Shadow", "Slider") + ';';
        // "Shadow Only" so the black plate itself never renders — only
        // its shadow does.
        sds.property("Shadow Only").setValue(1);

        // ── Recording (precomp instance) ─────────────────────────
        var rec = masterComp.layers.add(recComp);
        rec.name = "Recording";
        // Scale: fit inside the matte using Math.min so the source is
        // letterboxed instead of cover-cropped. Zoom Level is a plain
        // multiplier — at 1.0 the recording fits exactly, above 1.0 it
        // zooms in and the matte clips the overflow.
        // Zoom Level is clamped to 1.0–2.0 here so the slider can't push
        // the recording past a useful range no matter what value the
        // user types or drags in.
        rec.transform.scale.expression =
            'var pad = ' + thisCompCtrl("Padding", "Slider") + ';\n' +
            'var z   = Math.max(1, Math.min(2, ' + thisCompCtrl("Zoom Level", "Slider") + '));\n' +
            'var srcW   = thisLayer.source.width;\n' +
            'var srcH   = thisLayer.source.height;\n' +
            'var frameW = thisComp.width  - pad*2;\n' +
            'var frameH = thisComp.height - pad*2;\n' +
            'var fit    = Math.min(frameW / srcW, frameH / srcH) * 100;\n' +
            '[fit * z, fit * z];';
        // Position: pan range is the overflow of the zoomed recording
        // past the matte edge — max(0, scaledSize - frame). Zoom
        // Position is clamped to 0–1, so the recording edge can never
        // pull inside the matte: 0 = source edge flush to matte edge,
        // 0.5 = centred, 1 = opposite edge flush. Anything outside that
        // range is clipped, so a stray 1.4 or -0.3 just sticks at the
        // boundary instead of exposing empty matte.
        rec.transform.position.expression =
            'var pad = ' + thisCompCtrl("Padding", "Slider") + ';\n' +
            'var zp  = ' + thisCompCtrl("Zoom Position", "Point") + ';\n' +
            'var s   = thisLayer.transform.scale[0] / 100;\n' +
            'var srcW   = thisLayer.source.width;\n' +
            'var srcH   = thisLayer.source.height;\n' +
            'var frameW = thisComp.width  - pad*2;\n' +
            'var frameH = thisComp.height - pad*2;\n' +
            'var panW = Math.max(0, srcW * s - frameW);\n' +
            'var panH = Math.max(0, srcH * s - frameH);\n' +
            'var zx = Math.max(0, Math.min(1, zp[0]));\n' +
            'var zy = Math.max(0, Math.min(1, zp[1]));\n' +
            '[thisComp.width/2  - (zx - 0.5) * panW,\n' +
            ' thisComp.height/2 - (zy - 0.5) * panH];';
        // No Drop Shadow on the Recording layer itself — the alpha
        // matte would clip it to nothing. The dedicated "Recording
        // Shadow" layer below handles the shadow instead.

        // ── Recording Matte ──────────────────────────────────────
        // Size = the recording's FIT size (zoom-independent), so the
        // matte is exactly the recording precomp's footprint with
        // rounded corners. It never reacts to Zoom Level / Zoom
        // Position; only the footage inside zooms and the matte clips.
        var matte = masterComp.layers.addShape();
        matte.name = "Recording Matte";
        var matteRoot = matte.property("ADBE Root Vectors Group");
        var matteGrp  = matteRoot.addProperty("ADBE Vector Group");
        matteGrp.name = "Rectangle 1";
        var matteContents = matteGrp.property("ADBE Vectors Group");
        var matteRect = matteContents.addProperty("ADBE Vector Shape - Rect");
        matteRect.property("Size").expression =
            FIT_SIZE_EXPR + '[srcW * fit, srcH * fit];';
        matteRect.property("Roundness").expression =
            thisCompCtrl("Roundness", "Slider") + ';';
        var matteFill = matteContents.addProperty("ADBE Vector Graphic - Fill");
        matteFill.property("Color").setValue([1, 1, 1]);
        matte.transform.position.expression =
            '[thisComp.width/2, thisComp.height/2];';

        // The matte must sit immediately above Recording for the alpha
        // matte target. AE auto-stacks new layers on top.
        rec.trackMatteType = TrackMatteType.ALPHA;

        masterComp.openInViewer();
        alert("Imported " + bundle.name + "\n" +
              "  duration: " + duration.toFixed(2) + " s\n" +
              "  cursor samples: " + styleTimes.length + "\n" +
              "  move events: " + pTimes.length + "\n\n" +
              "Main comp 'Controls': Padding, Roundness, Shadow, Glass Halo,\n" +
              "Zoom Level (1.0 = no zoom), Zoom Position ([0.5, 0.5] = centred),\n" +
              "BG Color A / B.\n" +
              "Recording precomp 'Style Driver': Style Index, Cursor Size, Cursor Smoothness.");
    } catch (err) {
        var detail = "Import failed: " + err.toString();
        if (typeof err.line !== "undefined" && err.line !== null) {
            detail += "\nLine: " + err.line;
        }
        if (err.source) {
            // Show the offending source line; ExtendScript fills this in
            // for syntax / reference errors.
            var lines = String(err.source).split("\n");
            var li = (typeof err.line === "number") ? err.line - 1 : -1;
            if (li >= 0 && li < lines.length) {
                detail += "\n  >> " + lines[li].replace(/^\s+|\s+$/g, "");
            }
        }
        alert(detail);
    }
    app.endUndoGroup();
})();
