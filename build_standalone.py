#!/usr/bin/env python3
"""Bundle the game into one self-contained HTML file (polytrack.html).

Usage: python3 build_standalone.py [output.html]

Run it from anywhere; paths are resolved relative to this script.
"""
import base64
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "polytrack.html")

# Assets served at runtime through blob URLs (fetch / XHR / big images).
RUNTIME_DIRS = ["audio", "models", "tracks"]
RUNTIME_FILES = ["forced_square.json", "images/clouds.jpg", "images/smoke.png"]

IMG_MIME = {"svg": "image/svg+xml", "png": "image/png", "jpg": "image/jpeg"}


def read(path, mode="r"):
    with open(os.path.join(ROOT, path), mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()


def b64(data):
    return base64.b64encode(data).decode("ascii")


def replace_once(text, old, new, what):
    if text.count(old) != 1:
        sys.exit(f"patch anchor for {what} found {text.count(old)} times (expected 1); bundle changed?")
    return text.replace(old, new)


def check_embeddable(text, what):
    low = text.lower()
    if "</script" in low or "<!--" in low:
        sys.exit(f"{what} contains '</script' or '<!--' and can't be inlined safely")


# ---- main bundle ------------------------------------------------------------
main = read("main.bundle.js")

main = replace_once(
    main,
    'new Worker("simulation_worker.bundle.js")',
    "new Worker(window.__W())",
    "worker construction",
)

font = b64(read("a82f15d48dbc61b6edeb.woff2", "rb"))
main = replace_once(
    main,
    'n.p+"a82f15d48dbc61b6edeb.woff2"',
    f'"data:font/woff2;base64,{font}"',
    "woff2 font",
)

# Small images: inline as data URIs (they appear inside HTML template strings too).
def inline_image(m):
    path = m.group(0)
    if path in RUNTIME_FILES:
        return path
    ext = path.rsplit(".", 1)[1]
    return f"data:{IMG_MIME[ext]};base64,{b64(read(path, 'rb'))}"


main = re.sub(r"images/[A-Za-z0-9_]+\.(?:svg|png|jpg)", inline_image, main)
# Big images: resolve through the runtime blob-URL map, only where used as a plain string literal.
main = re.sub(r'"(images/(?:clouds\.jpg|smoke\.png))"', r'window.__U("\1")', main)
# Anything still referring to the big images (the preload list) goes through __U too, caught above.
check_embeddable(main, "main.bundle.js")

# ---- worker -----------------------------------------------------------------
worker = read("simulation_worker.bundle.js")
ammo = read("lib/ammo.wasm.js")
# Ammo is sloppy-mode code that relies on `this` being the global, so it goes in front of the
# worker source (top level of the worker script) rather than inside the bundle's IIFE.
worker = replace_once(worker, 'importScripts("lib/ammo.wasm.js");', "", "ammo import")
worker = ammo + "\n;\n" + worker
worker = replace_once(worker, "Ammo()", "Ammo({wasmBinary:__WB})", "Ammo init")
check_embeddable(worker, "worker")
wasm_b64 = b64(read("lib/ammo.wasm.wasm", "rb"))

# ---- runtime asset map ------------------------------------------------------
assets = {}
for d in RUNTIME_DIRS:
    for name in sorted(os.listdir(os.path.join(ROOT, d))):
        assets[f"{d}/{name}"] = b64(read(f"{d}/{name}", "rb"))
for p in RUNTIME_FILES:
    assets[p] = b64(read(p, "rb"))
assets_json = "{" + ",".join(f'"{k}":"{v}"' for k, v in assets.items()) + "}"

PRELUDE = r"""
(function () {
  var A = JSON.parse(document.getElementById('__assets').textContent);
  var MIME = {flac: 'audio/flac', mp3: 'audio/mpeg', glb: 'model/gltf-binary', json: 'application/json',
              track: 'text/plain', png: 'image/png', jpg: 'image/jpeg'};
  var cache = {};
  function bytes(s) {
    var bin = atob(s), n = bin.length, u = new Uint8Array(n);
    for (var i = 0; i < n; i++) u[i] = bin.charCodeAt(i);
    return u;
  }
  // Blob URL for an embedded asset path; unknown paths pass through unchanged.
  window.__U = function (key) {
    if (!(key in A)) return key;
    return cache[key] || (cache[key] = URL.createObjectURL(
      new Blob([bytes(A[key])], {type: MIME[key.split('.').pop()] || 'application/octet-stream'})));
  };
  function lookup(url) {
    url = String(url).split(/[?#]/)[0];
    for (var k in A) if (url === k || url.slice(-k.length - 1) === '/' + k) return k;
    return null;
  }
  var realFetch = window.fetch;
  window.fetch = function (input, init) {
    var k = lookup(input && input.url ? input.url : input);
    return k ? realFetch.call(this, window.__U(k)) : realFetch.call(this, input, init);
  };
  var realOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    var k = lookup(url);
    if (k) arguments[1] = window.__U(k);
    return realOpen.apply(this, arguments);
  };
  // Physics worker: built from the embedded source, with the wasm bytes handed in directly.
  var workerUrl;
  window.__W = function () {
    if (!workerUrl) {
      var pre = 'const __WB=Uint8Array.from(atob(' + JSON.stringify(document.getElementById('__wasm').textContent.trim()) +
                '),function(c){return c.charCodeAt(0)});\n';
      workerUrl = URL.createObjectURL(new Blob([pre, document.getElementById('__worker').textContent],
                                               {type: 'text/javascript'}));
    }
    return workerUrl;
  };
})();
"""

icon = b64(read("images/icon.svg", "rb"))

html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>PolyTrack</title>
<link rel="icon" href="data:image/svg+xml;base64,{icon}">
<meta name="viewport" content="width=device-width, initial-scale=1, minimum-scale=1, maximum-scale=1, user-scalable=no">
</head>
<body>
<canvas id="screen"></canvas>
<div id="ui"></div>
<div id="transition-layer"></div>
<script id="__assets" type="application/json">{assets_json}</script>
<script id="__wasm" type="text/plain">{wasm_b64}</script>
<script id="__worker" type="text/plain">{worker}</script>
<script>{PRELUDE}</script>
<script type="module">{main}</script>
</body>
</html>
"""

with open(OUT, "w", encoding="utf-8") as f:
    f.write(html)
print(f"wrote {OUT} ({os.path.getsize(OUT) / 1e6:.1f} MB)")
