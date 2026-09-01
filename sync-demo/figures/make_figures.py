"""Render the design-document sequence diagrams to equal-size PNGs.

Each ``*.mmd`` in this directory is rendered onto an identical white canvas
so the figures are the *same size* - the two assessed modules
(``change_detection.py`` and ``single_file_transfer.py``) get one sequence
diagram each, matched for the design document
(``docs/<branch>/architecture_report_<branch>_full.md``).

Run:  .venv/bin/python figures/make_figures.py

Dependencies (host-side, not needed to run the demo itself):
  - playwright:            pip install playwright
  - a Chromium binary:     playwright install chromium   (or set CHROMIUM)
  - mermaid.min.js:        npm install mermaid            (or set MERMAID_JS)

CHROMIUM may point at an existing Chrome/Chromium binary (it is passed
straight to ``executable_path``), which avoids the playwright browser
download.

The canvas is fixed, so every figure comes out at the same pixel
dimensions regardless of how tall or wide the diagram itself is; each
diagram is scaled to fit and centred, and the viewBox is taken from the
*actual* rendered extent (``getBBox``) so nothing on the leftmost lifeline
clips.
"""

from __future__ import annotations

import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

CANVAS_W, CANVAS_H = 1000, 720          # identical for every figure
PAD_W, PAD_H = CANVAS_W - 70, CANVAS_H - 70
SCALE = 2                               # device pixels per CSS pixel


def _find_mermaid() -> str:
    """Return the path to mermaid.min.js.

    Search $MERMAID_JS first, then a local node_modules.
    """
    candidates = [
        os.environ.get("MERMAID_JS"),
        os.path.join(HERE, "node_modules/mermaid/dist/mermaid.min.js"),
        os.path.join(HERE, "..", "node_modules/mermaid/dist/mermaid.min.js"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    sys.exit("mermaid.min.js not found. `npm install mermaid` here, or set "
             "MERMAID_JS to its path.")


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>
  html,body{{margin:0;padding:0;background:#ffffff;}}
  #canvas{{width:{W}px;height:{H}px;background:#ffffff;
    display:flex;align-items:center;justify-content:center;overflow:hidden;}}
  #canvas svg{{max-width:{PW}px;max-height:{PH}px;}}
</style>
<script>{MERMAID}</script>
</head><body>
<div id="canvas"><div id="target"></div></div>
<script type="text/plain" id="src">{DIAGRAM}</script>
<script>
  const MARGIN = 18;
  mermaid.initialize({{ startOnLoad:false, theme:"neutral", securityLevel:"loose",
    themeVariables:{{ fontSize:"14px" }} }});
  mermaid.render("g", document.getElementById("src").textContent).then(({{svg}}) => {{
    const t = document.getElementById("target"); t.innerHTML = svg;
    const el = t.querySelector("svg");
    const bb = el.getBBox();            // true drawn extent, incl. overflow
    const x = bb.x - MARGIN, y = bb.y - MARGIN,
          w = bb.width + 2*MARGIN, h = bb.height + 2*MARGIN;
    el.setAttribute("viewBox", [x,y,w,h].join(" "));
    el.setAttribute("width", w); el.setAttribute("height", h);
    el.style.maxWidth = "{PW}px"; el.style.maxHeight = "{PH}px";
    requestAnimationFrame(() => requestAnimationFrame(() => window.__ready = true));
  }}).catch(e => {{ window.__err = String(e); }});
</script>
</body></html>"""


def main() -> None:
    """Render every ``*.mmd`` next to this script to an equal-size PNG."""
    from playwright.sync_api import sync_playwright

    mermaid = open(_find_mermaid(), encoding="utf-8").read()
    sources = sorted(glob.glob(os.path.join(HERE, "*.mmd")))
    if not sources:
        sys.exit("no *.mmd sources next to this script")

    chromium = os.environ.get("CHROMIUM")
    with sync_playwright() as p:
        launch = {"args": ["--no-sandbox"]}
        if chromium:
            launch["executable_path"] = chromium
        browser = p.chromium.launch(**launch)
        page = browser.new_page(viewport={"width": CANVAS_W, "height": CANVAS_H},
                                device_scale_factor=SCALE)
        for src in sources:
            name = os.path.splitext(os.path.basename(src))[0]
            diagram = open(src, encoding="utf-8").read()
            html = os.path.join(HERE, f".render_{name}.html")
            with open(html, "w", encoding="utf-8") as fh:
                fh.write(PAGE.format(W=CANVAS_W, H=CANVAS_H, PW=PAD_W, PH=PAD_H,
                                     MERMAID=mermaid, DIAGRAM=diagram))
            page.goto("file://" + html)
            try:
                page.wait_for_function("window.__ready === true", timeout=20000)
            except Exception:
                sys.exit(f"{name}: render failed: "
                         + str(page.evaluate("window.__err")))
            page.wait_for_timeout(200)
            out = os.path.join(HERE, f"{name}.png")
            page.query_selector("#canvas").screenshot(path=out)
            os.remove(html)
            print("wrote", os.path.relpath(out, os.path.join(HERE, "..")))
        browser.close()


if __name__ == "__main__":
    main()
