#!/usr/bin/env python3
from __future__ import annotations
import argparse
import configparser
import hashlib
from pathlib import Path
import py_compile
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "terrain_viewer"
BLOCKED_SUFFIXES = {".pyc", ".pyo", ".pyd", ".dll", ".so", ".dylib", ".exe"}
REQUIRED = ["name", "qgisMinimumVersion", "description", "about", "version", "author", "email", "repository"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--github-user", required=True)
    ap.add_argument("--repo", default="qgis-terrain-viewer")
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    user = args.github_user.strip()
    repo = args.repo.strip()
    if not user or not repo:
        raise SystemExit("GitHub user and repo name must be non-empty.")

    meta_path = PLUGIN / "metadata.txt"
    text = meta_path.read_text(encoding="utf-8")
    base = f"https://github.com/{user}/{repo}"
    # Keep repository URLs synchronized with the requested GitHub owner/repository.
    import re
    text = re.sub(r"homepage=https://github\.com/[^/]+/[^\n#]+(?:#readme)?", f"homepage={base}#readme", text)
    text = re.sub(r"repository=https://github\.com/[^/]+/[^\n]+", f"repository={base}", text)
    text = re.sub(r"tracker=https://github\.com/[^/]+/[^\n]+/issues", f"tracker={base}/issues", text)
    meta_path.write_text(text, encoding="utf-8", newline="\n")

    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(meta_path, encoding="utf-8")
    g = cfg["general"]
    missing = [k for k in REQUIRED if not g.get(k, "").strip()]
    if missing:
        raise SystemExit("Missing metadata: " + ", ".join(missing))
    for key in ("homepage", "repository", "tracker"):
        if not g.get(key, "").startswith("https://"):
            raise SystemExit(f"{key} must be an https URL")

    blocked = []
    for p in PLUGIN.rglob("*"):
        if p.is_file() and (p.suffix.lower() in BLOCKED_SUFFIXES or "__pycache__" in p.parts):
            blocked.append(str(p.relative_to(PLUGIN)))
    if blocked:
        raise SystemExit("Blocked compiled/binary files: " + ", ".join(blocked))

    compile_dir = ROOT / ".release_compile_check"
    if compile_dir.exists():
        shutil.rmtree(compile_dir)
    compile_dir.mkdir()
    try:
        for p in PLUGIN.rglob("*.py"):
            cfile = compile_dir / (p.name + ".pyc")
            py_compile.compile(str(p), cfile=str(cfile), doraise=True)
    finally:
        shutil.rmtree(compile_dir, ignore_errors=True)

    version = g.get("version", "1.0.1")
    out = Path(args.output) if args.output else ROOT / f"Terrain_Viewer_QGIS_{version}.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in sorted(PLUGIN.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix.lower() not in BLOCKED_SUFFIXES:
                zf.write(p, Path("terrain_viewer") / p.relative_to(PLUGIN))

    size_mb = out.stat().st_size / (1024 * 1024)
    if size_mb > 20:
        out.unlink(missing_ok=True)
        raise SystemExit(f"Package is {size_mb:.2f} MB, over the conservative 20 MB QGIS repository limit.")
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"Built: {out}")
    print(f"Size: {size_mb:.3f} MB")
    print(f"SHA-256: {digest}")

if __name__ == "__main__":
    main()
