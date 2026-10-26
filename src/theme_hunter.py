"""The 'Hunter Red & Black Neon' look for The Dominator (display only - nothing here touches trading or any number on the page).

apply(html)       -> the same page with the old grey-blue palette swapped for the hunter palette (deep black canvas, charcoal panels with fine crimson borders, neon red accents,
                     neon emerald for UP / winning, neon hunter red for DOWN / losing) - colours only, every word and number stays.
page_css()        -> a little extra CSS for the main dashboard: tabular numbers, crimson panel borders, the red glow on the critical cards. The layout is the ORIGINAL one (owner's choice 2026-10-25).
Switch it all off with the host variable DASH_THEME=classic (the old look comes back on the next restart)."""
import os
import re

ON = os.environ.get("DASH_THEME", "hunter").lower() != "classic"

# colours swapped everywhere (page, inline styles, SVG): old hex (lower case) -> new hex
UI = {
    "#0d1117": "#070709", "#0f1419": "#0a0a0c", "#010409": "#050506",
    "#161b22": "#111216", "#1b222b": "#16171d", "#21262d": "#16171d", "#1c2128": "#16171d", "#1f2a3a": "#2a1018",
    "#30363d": "#3d1420", "#3b4350": "#3d1420", "#4a5360": "#4a1b28", "#484f58": "#5a2230",
    "#6e7681": "#7a808c", "#8b949e": "#8a919e", "#9da7b3": "#9aa1ad", "#a6b0bb": "#9aa1ad",
    "#c9d1d9": "#e8eaf0", "#e6edf3": "#f5f7fa",
    # up / winning -> neon emerald
    "#3fb950": "#00e676", "#56d364": "#00e676", "#2ea043": "#00c853", "#7ee787": "#00ff88", "#238636": "#0b7a43",
    "#0f2a1a": "#04170f", "#0d2818": "#04170f", "#1f2a1f": "#04170f", "#128266": "#0b7a43",
    # down / losing -> neon hunter red
    "#f85149": "#ff1e44", "#ff7b72": "#ff3b5c", "#da3633": "#e0143c", "#b62324": "#b3122f",
    "#2a1212": "#2a0a10", "#2d0d0d": "#2a0a10", "#2a1215": "#2a0a10", "#2a1f1f": "#2a0a10", "#6a1a1a": "#5c1020",
}
# blue accents: only inside <style> blocks (so the blue / red / green moving-average lines on the charts keep the colours the words describe)
CSS_ONLY = {"#58a6ff": "#ff3355", "#1f6feb": "#c4002f", "#39d0d8": "#ff3355", "#388bfd": "#e0143c", "#4493f8": "#ff3355"}

_RX_UI = re.compile("(" + "|".join(re.escape(k) for k in sorted(UI, key=len, reverse=True)) + ")(?![0-9a-fA-F])", re.I)
_RX_CSS = re.compile("(" + "|".join(re.escape(k) for k in CSS_ONLY) + ")(?![0-9a-fA-F])", re.I)
_RX_STYLE = re.compile(r"<style[^>]*>.*?</style>", re.S | re.I)


def apply(html):
    """Swap the palette in a page / fragment / JSON string. Returns it unchanged when the classic look is on."""
    if not ON or not html:
        return html
    out = _RX_UI.sub(lambda m: UI[m.group(1).lower()], html)
    return _RX_STYLE.sub(lambda m: _RX_CSS.sub(lambda k: CSS_ONLY[k.group(1).lower()], m.group(0)), out)


def apply_bytes(b):
    if not ON or not b:
        return b
    try:
        return apply(b.decode("utf-8")).encode("utf-8")
    except Exception:
        return b


PAGE_CSS = """
:root{--hx-red:#ff003f;--hx-red2:#e50914;--hx-up:#00e676;--hx-down:#ff1e44;--hx-line:rgba(220,20,60,.25);--hx-glow:0 0 15px rgba(255,0,63,.15)}
html,body{background:#070709 !important;color:#f5f7fa}
body{font-family:Inter,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;font-variant-numeric:tabular-nums;font-feature-settings:'tnum' 1}
h1{letter-spacing:.5px}
.card{border-color:var(--hx-line) !important;box-shadow:0 0 0 1px rgba(255,0,63,.04),0 0 22px rgba(255,0,63,.06)}
.tug-end,.flip-risk-bar,.hr-warn,.tug-end.up,.tug-end.down{box-shadow:var(--hx-glow)}
.tug-end.down,.flip-risk-bar.down,.hr-warn{box-shadow:0 0 15px rgba(255,0,63,.28)}
.view-bar button,.size-mode-btn{border-color:var(--hx-line) !important}
.size-mode-active{border-color:#ff003f !important;box-shadow:0 0 10px rgba(255,0,63,.35)}
details>summary{letter-spacing:.2px}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-track{background:#070709}::-webkit-scrollbar-thumb{background:#3d1420;border-radius:6px}::-webkit-scrollbar-thumb:hover{background:#ff003f}
"""


def page_css():
    return ("<style id=\"hunter-theme\">" + PAGE_CSS + "</style>") if ON else ""
