#!/usr/bin/env python3
"""Build a single-file PolyTrack 0.6.3 (official + community tracks) from the live web build.

Usage: python3 build_standalone_0_6_3.py [--cache DIR] [output.html]

Downloads the 0.6.3 build from app-polytrack.kodub.com into a cache directory (default:
.cache/polytrack-0.6.3 next to this script; re-runs only fetch what's missing), then inlines
everything. The leaderboard is a live server API and is not embedded.

This is a reupload in the sense of Kodub's Terms of Service (https://www.kodub.com/terms/polytrack,
section 4): keep it non-commercial, keep the in-game "unofficial version" notice and the kodub.com
link untouched, and credit the original game (https://www.crazygames.com/game/polytrack).
`unblocked: true` is the flag those terms prescribe for reuploads.
"""
import base64
import concurrent.futures as cf
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = "https://app-polytrack.kodub.com/0.6.3/"
ROOT = os.path.dirname(os.path.abspath(__file__))

args = sys.argv[1:]
CACHE = os.path.join(ROOT, ".cache", "polytrack-0.6.3")
if "--cache" in args:
    i = args.index("--cache")
    CACHE = os.path.abspath(args[i + 1])
    del args[i : i + 2]
OUT = args[0] if args else os.path.join(ROOT, "polytrack_0.6.3.html")

ASSET_RE = re.compile(r"(?:tracks|audio|models|lib|images)/[A-Za-z0-9_./-]+\.(?:track|mp3|ogg|glb|png|jpg|svg|js|wasm)")
MIME = {"svg": "image/svg+xml", "png": "image/png", "jpg": "image/jpeg"}


def fetch(path, optional=False):
    dst = os.path.join(CACHE, path)
    if os.path.exists(dst):
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "polytrack-standalone-build"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            break
        except urllib.error.HTTPError as e:
            if optional and e.code == 404:
                return
            if e.code == 429 and attempt < 5:  # rate limited: wait as long as the server asks, then retry
                time.sleep(float(e.headers.get("Retry-After") or 5 * (attempt + 1)))
                continue
            raise
    with open(dst, "wb") as f:
        f.write(data)


def read(path, mode="r"):
    with open(os.path.join(CACHE, path), mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()


def b64(data):
    return base64.b64encode(data).decode("ascii")


def replace_once(text, old, new, what):
    if text.count(old) != 1:
        sys.exit(f"patch anchor for {what} found {text.count(old)} times (expected 1); build changed?")
    return text.replace(old, new)


def check_embeddable(text, what):
    low = text.lower()
    if "</script" in low or "<!--" in low:
        sys.exit(f"{what} contains '</script' or '<!--' and can't be inlined safely")


# ---- 1. fetch ---------------------------------------------------------------
for p in ["main.bundle.js", "simulation_worker.bundle.js", "error_screen.bundle.js",
          "lib/polytrack_physics.js", "polytrack_physics.wasm", "forced_square.woff2"]:
    fetch(p)

# Lazy webpack chunks (race, editor, ...): follow i.e(<id>) calls to a fixed point.
chunks, todo = [], set(re.findall(r"\b\w\.e\((\d+)\)", read("main.bundle.js")))
while todo:
    c = todo.pop()
    chunks.append(c)
    fetch(f"{c}.bundle.js")
    todo |= {n for n in re.findall(r"\b\w\.e\((\d+)\)", read(f"{c}.bundle.js")) if n not in chunks}
chunks.sort()

bundle_files = ["main.bundle.js", "simulation_worker.bundle.js"] + [f"{c}.bundle.js" for c in chunks]
asset_paths = set()
for f in bundle_files:
    asset_paths |= set(ASSET_RE.findall(read(f)))
    asset_paths |= {m for m in re.findall(r'\b\w+\.p\+"([^"]+)"', read(f))}
asset_paths |= {"lib/draco/draco_wasm_wrapper.js", "lib/draco/draco_decoder.wasm", "models/garage.glb"}
# Country flags are built as "images/countries/" + code + ".svg", so take the codes from the country map.
_m = re.search(r'\{(af:"Afghanistan",.*?)\}', read("main.bundle.js"))
flags = {f"images/countries/{c}.svg" for c in re.findall(r'(?:^|,)([a-z]{2}):"', _m.group(1))} if _m else set()
asset_paths |= flags
# Skip things that are code we handle separately, or that the bundle only names as fallbacks.
asset_paths -= {"lib/polytrack_physics.js"}
asset_paths = sorted(p for p in asset_paths if not p.endswith((".ttf", ".woff")) or p.endswith(".woff2"))

# Be gentle with the host: few connections, and the many small flag files go one at a time.
with cf.ThreadPoolExecutor(3) as ex:
    list(ex.map(fetch, [p for p in asset_paths if p not in flags]))
for p in sorted(flags):
    fetch(p, optional=True)
asset_paths = [p for p in asset_paths if os.path.exists(os.path.join(CACHE, p))]
print(f"{len(chunks)} chunks, {len(asset_paths)} assets")

# ---- 2. rewrite the webpack bundles -----------------------------------------
IMG_PATH = re.compile(r"(?<![\w/.-])images/[A-Za-z0-9_]+\.(?:svg|png|jpg)")
runtime_assets = set(asset_paths)


def data_uri(p):
    return f"data:{MIME[p.rsplit('.', 1)[1]]};base64,{b64(read(p, 'rb'))}"


def asset_module(m):
    # Webpack asset modules (`n.p + "path"`): blob URL from the runtime map.
    p = m.group(1)
    if p in runtime_assets:
        return f'window.__U("{p}")'
    return m.group(0)  # e.g. the .ttf/.woff font fallbacks, which aren't shipped


def rewrite(src):
    src = re.sub(r'\b\w+\.p\+"([^"]+)"', asset_module, src)
    # Exact string literals of images -> runtime map as well.
    # (` src="..."` is an HTML attribute inside a template string, not a JS literal; those fall through below.)
    src = re.sub(r'(?<!\ssrc=)"(images/[A-Za-z0-9_]+\.(?:svg|png|jpg))"',
                 lambda m: f'window.__U("{m.group(1)}")' if m.group(1) in runtime_assets else m.group(0), src)
    # Whatever is left sits inside HTML template strings, where nothing can intercept the load: data URIs.
    return re.sub(r'(?<!__U\(")' + IMG_PATH.pattern, lambda m: data_uri(m.group(0)) if m.group(0) in asset_paths else m.group(0), src)


main = rewrite(read("main.bundle.js"))
main = replace_once(main, 'new Worker("simulation_worker.bundle.js")', "new Worker(window.__W())", "sim worker construction")
# Webpack's automatic publicPath finds nothing in an inline script and throws; every asset URL goes
# through the runtime map anyway, so any value will do.
main = replace_once(main, 'throw new Error("Automatic publicPath is not supported in this browser")', 'e="./"', "publicPath detection")
# The community version tabs get their banner through a CSS custom property built at runtime.
main = replace_once(
    main,
    '"--image-url: url(\'images/community_tracks_"+n+".jpg\');"',
    '"--image-url: url(\'"+window.__U("images/community_tracks_"+n+".jpg")+"\');"',
    "community tab banner",
)
chunk_src = {c: rewrite(read(f"{c}.bundle.js")) for c in chunks}
error_screen = rewrite(read("error_screen.bundle.js"))
for name, text in [("main", main), ("error_screen", error_screen), *[(f"chunk {c}", t) for c, t in chunk_src.items()]]:
    check_embeddable(text, name)

# ---- 3. physics worker ------------------------------------------------------
worker = read("simulation_worker.bundle.js")
physics = read("lib/polytrack_physics.js")
# (the call sits inside a comma expression, so replace it with a no-op rather than deleting it)
worker = replace_once(worker, 'importScripts("lib/polytrack_physics.js")', "0", "physics import")
worker = replace_once(worker, "PolyTrackPhysics()", "PolyTrackPhysics({wasmBinary:__WB})", "physics init")
# Emscripten glue is sloppy-mode global code, so it goes at the top level of the worker script.
worker = physics + "\n;\n" + worker
check_embeddable(worker, "worker")
wasm_b64 = b64(read("polytrack_physics.wasm", "rb"))

# ---- 4. runtime asset map ---------------------------------------------------
assets_json = "{" + ",".join(f'"{p}":"{b64(read(p, "rb"))}"' for p in sorted(runtime_assets) if p in asset_paths) + "}"

PRELUDE = r"""
(function () {
  var A = JSON.parse(document.getElementById('__assets').textContent);
  var MIME = {ogg: 'audio/ogg', mp3: 'audio/mpeg', glb: 'model/gltf-binary', json: 'application/json',
              track: 'text/plain', png: 'image/png', jpg: 'image/jpeg', svg: 'image/svg+xml',
              js: 'text/javascript', wasm: 'application/wasm', woff2: 'font/woff2'};
  var cache = {};
  function bytes(s) {
    var bin = atob(s), n = bin.length, u = new Uint8Array(n);
    for (var i = 0; i < n; i++) u[i] = bin.charCodeAt(i);
    return u;
  }
  // Blob URL for an embedded asset path; unknown paths pass through unchanged.
  window.__U = function (key) {
    if (!Object.prototype.hasOwnProperty.call(A, key)) return key;
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
  // <img>.src = "images/..." / "tracks/.../thumbnails/..." set from code.
  var d = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src');
  Object.defineProperty(HTMLImageElement.prototype, 'src', {
    configurable: true, enumerable: d.enumerable, get: d.get,
    set: function (v) { var k = lookup(v); d.set.call(this, k ? window.__U(k) : v); }
  });
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

icon_path = "images/icons/icon.svg"
icon_tag = ""
if icon_path in asset_paths:
    icon_tag = f'<link rel="icon" href="data:image/svg+xml;base64,{b64(read(icon_path, "rb"))}">'

# Lazy chunks are registered up front so webpack treats them as already loaded.
chunk_scripts = "\n".join(f"<script>{t}</script>" for t in chunk_src.values())

html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>PolyTrack</title>
{icon_tag}
<meta name="viewport" content="width=device-width,initial-scale=1,minimum-scale=1,maximum-scale=1,viewport-fit=cover,user-scalable=no">
</head>
<body>
<canvas id="screen"></canvas>
<div id="ui"></div>
<div id="transition-layer"></div>
<script>window.polytrackModConfiguration = {{ unblocked: true }};</script>
<script id="__assets" type="application/json">{assets_json}</script>
<script id="__wasm" type="text/plain">{wasm_b64}</script>
<script id="__worker" type="text/plain">{worker}</script>
<script>{PRELUDE}</script>
<script>{error_screen}</script>
{chunk_scripts}
<script type="module">{main}</script>
</body>
</html>
"""

with open(OUT, "w", encoding="utf-8") as f:
    f.write(html)
print(f"wrote {OUT} ({os.path.getsize(OUT) / 1e6:.1f} MB)")
