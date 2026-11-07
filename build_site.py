#!/usr/bin/env python3
"""Assemble the website folder from the latest reports (one page per account)."""
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
cfg = json.loads((HERE / "config.json").read_text())
site = HERE / "site"
shutil.rmtree(site, ignore_errors=True)
site.mkdir()
rep = HERE / cfg.get("report_dir", "reports")
accounts = [a["name"] for a in cfg.get("accounts") or [] if a.get("name")]
for i, name in enumerate(accounts or [""]):
    src = rep / name if name else rep
    dest = site / name if name else site
    dest.mkdir(parents=True, exist_ok=True)
    for f in src.glob("report_*.html"):
        shutil.copy(f, dest / f.name)
    for f in ("trades.csv",):
        if (src / f).exists():
            shutil.copy(src / f, dest / f)
    if (src / "latest.html").exists():
        shutil.copy(src / "latest.html", dest / "index.html")
        if i == 0 and name:
            shutil.copy(src / "latest.html", site / "index.html")
for f in ("backtest.html", "dials.html"):
    if (HERE / "published" / f).exists():
        shutil.copy(HERE / "published" / f, site / f)
print("site built:", sorted(str(p.relative_to(site)) for p in site.rglob("index.html")))
print("publish=" + ("true" if cfg.get("publish_site", True) else "false"))
