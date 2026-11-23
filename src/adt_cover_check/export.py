"""Write the static site (index.html + covers.json) for GitHub Pages.

Usage: uv run adt-export [out_dir]  (default ./site)
"""
import json
import shutil
import sys
from pathlib import Path

from .paths import DB_PATH, INDEX_HTML
from .server import load_covers


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "site")
    if not DB_PATH.exists():
        sys.exit(f"{DB_PATH} not found; run `uv run adt-sync` first")
    data = load_covers()
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(INDEX_HTML, out / "index.html")
    (out / "covers.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (out / ".nojekyll").touch()
    print(f"Wrote {len(data['covers'])} covers and {len(data['people'])} people to {out}/")


if __name__ == "__main__":
    main()
