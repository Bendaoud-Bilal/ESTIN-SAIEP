"""Inject the ESTIN splash animation into JupyterLab's index.html.

Usage:
    python patch_lab.py [/opt/conda/share/jupyter/lab/static/index.html]

The script is idempotent and also copies the ESTIN logo next to index.html so
the page can load it as ``static/logo-estin.png``.
"""
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

CSS = """
<style>
#jupyterlab-splash { display: none !important; }
#estin-splash {
  position: fixed; inset: 0; z-index: 99999;
  display: flex; align-items: center; justify-content: center;
  background: radial-gradient(circle at 50% 42%, #445d75 0%, #34495e 45%, #22313f 100%);
  animation: estin-splash-fade 3.6s ease forwards;
  pointer-events: none;
}
#estin-splash .estin-splash-inner { position: relative; text-align: center; }
#estin-splash img {
  width: 230px; max-width: 48vw; display: block; margin: 0 auto;
  animation: estin-splash-pulse 1.15s ease-in-out infinite alternate;
  filter: drop-shadow(0 0 18px rgba(229,76,63,.55));
}
#estin-splash .estin-splash-text {
  margin-top: 18px; color: #fff; font: 600 22px/1.2 sans-serif;
  letter-spacing: .28em; animation: estin-splash-glow 1.15s ease-in-out infinite alternate;
}
#estin-splash .estin-splash-ring {
  position: absolute; left: 50%; top: 38%; width: 300px; height: 300px;
  margin: -150px 0 0 -150px; border: 2px solid rgba(229,76,63,.45);
  border-radius: 50%; animation: estin-splash-ring 1.8s ease-out infinite;
}
@keyframes estin-splash-pulse {
  from { transform: scale(.94); opacity: .82; }
  to   { transform: scale(1.06); opacity: 1; }
}
@keyframes estin-splash-glow {
  from { opacity: .55; }
  to   { opacity: 1; text-shadow: 0 0 18px rgba(229,76,63,.7); }
}
@keyframes estin-splash-ring {
  from { transform: scale(.72); opacity: .8; }
  to   { transform: scale(1.18); opacity: 0; }
}
@keyframes estin-splash-fade {
  0%, 72% { opacity: 1; visibility: visible; }
  100% { opacity: 0; visibility: hidden; }
}
</style>
"""

SPLASH = """
<div id="estin-splash" role="status" aria-label="ESTIN SAIEP">
  <div class="estin-splash-inner">
    <div class="estin-splash-ring"></div>
    <img src="static/logo-estin.png" alt="ESTIN">
    <div class="estin-splash-text">SAIEP</div>
  </div>
</div>
"""


def patch(index_path):
    index = Path(index_path)
    if not index.exists():
        raise SystemExit(f'JupyterLab index not found: {index}')
    logo = index.parent / 'logo-estin.png'
    src = REPO / 'share' / 'jupyterhub' / 'static' / 'img' / 'logo-estin.png'
    if src.exists():
        shutil.copyfile(src, logo)
    text = index.read_text()
    if 'id="estin-splash"' not in text:
        text = text.replace('</head>', CSS + '</head>', 1)
        text = text.replace('</body>', SPLASH + '</body>', 1)
        index.write_text(text)
    return index


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else '/opt/conda/share/jupyter/lab/static/index.html'
    print(patch(path))
