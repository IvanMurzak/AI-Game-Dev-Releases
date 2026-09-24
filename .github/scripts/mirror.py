#!/usr/bin/env python3
"""Mirror the published AI Game Dev desktop builds from downloads.ai-game.dev into this repo's Releases.

downloads.ai-game.dev (Cloudflare R2) stays the source of truth. This script only copies what the
update feeds already list, byte for byte, and refuses anything whose size or sha512 does not match
the feed. The Cloudflare Worker in front of downloads.ai-game.dev redirects clients in regions with
a slow path to Cloudflare to these assets, and falls back to R2 when an asset is not mirrored yet.

Asset naming: GitHub turns spaces in an asset name into dots, so every asset is uploaded as
`name.replace(" ", ".")` under the tag `v<version>`. The Worker applies the same transform.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

ORIGIN = "https://downloads.ai-game.dev"
FEEDS = ["latest.yml", "latest-mac.yml", "latest-linux.yml"]
KEEP_VERSIONS = int(os.environ.get("KEEP_VERSIONS", "5"))
KEEPALIVE_DAYS = 30
REPO = os.environ["GITHUB_REPOSITORY"]
UA = {"User-Agent": "ai-game-dev-release-mirror"}


def gh(*args: str, check: bool = True) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed ({r.returncode}): {r.stderr.strip()}")
    return r.stdout


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return r.read()


def parse_feed(text: str) -> tuple[str, list[dict]]:
    """Minimal parser for electron-builder feeds: `version:` plus the `files:` list entries."""
    version = None
    files: list[dict] = []
    cur: dict | None = None
    in_files = False
    for line in text.splitlines():
        if re.match(r"^version:\s*", line):
            version = line.split(":", 1)[1].strip().strip("'\"")
        elif re.match(r"^files:\s*$", line):
            in_files = True
        elif in_files and re.match(r"^\s+-\s+url:", line):
            cur = {"url": line.split("url:", 1)[1].strip().strip("'\"")}
            files.append(cur)
        elif in_files and cur is not None and re.match(r"^\s+\w+:", line):
            k, v = line.strip().split(":", 1)
            cur[k] = v.strip().strip("'\"")
        elif not line.startswith(" "):
            in_files = False
            cur = None
    if not version or not files:
        raise ValueError("feed has no version or no files")
    for f in files:
        if "sha512" not in f or "size" not in f:
            raise ValueError(f"feed entry {f.get('url')} lacks sha512/size")
    return version, files


def asset_name(url: str) -> str:
    return url.replace(" ", ".")


def release_assets(tag: str) -> dict[str, int] | None:
    out = gh("release", "view", tag, "--repo", REPO, "--json", "assets", check=False)
    if not out.strip():
        return None
    return {a["name"]: a["size"] for a in json.loads(out)["assets"]}


def mirror_file(tag: str, f: dict, existing: dict[str, int]) -> bool:
    name = asset_name(f["url"])
    size = int(f["size"])
    if existing.get(name) == size:
        return False
    if name in existing:
        raise RuntimeError(f"{tag}/{name} exists with size {existing[name]}, feed says {size}; refusing to replace")
    src = f"{ORIGIN}/{urllib.parse.quote(f['url'])}"
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, name)
        h = hashlib.sha512()
        n = 0
        with urllib.request.urlopen(urllib.request.Request(src, headers=UA), timeout=600) as r, open(path, "wb") as out:
            while chunk := r.read(1 << 20):
                h.update(chunk)
                n += len(chunk)
                out.write(chunk)
        digest = base64.b64encode(h.digest()).decode()
        if n != size or digest != f["sha512"]:
            raise RuntimeError(f"{src}: got {n} bytes sha512 {digest}, feed says {size} / {f['sha512']}")
        gh("release", "upload", tag, path, "--repo", REPO)
    after = release_assets(tag) or {}
    if after.get(name) != size:
        raise RuntimeError(f"uploaded {name} but release shows {sorted(after)}")
    print(f"mirrored {tag}/{name} ({size} bytes)")
    return True


def ensure_release(tag: str, version: str) -> dict[str, int]:
    existing = release_assets(tag)
    if existing is None:
        gh("release", "create", tag, "--repo", REPO, "--title", f"AI Game Dev {version}",
           "--notes", f"Mirror of the AI Game Dev {version} desktop builds. Download: https://ai-game.dev/download",
           "--latest=false")
        existing = {}
    return existing


def prune() -> list[str]:
    out = gh("release", "list", "--repo", REPO, "--limit", "200", "--json", "tagName,createdAt")
    rels = sorted(json.loads(out), key=lambda r: r["createdAt"], reverse=True)
    removed = []
    for r in rels[KEEP_VERSIONS:]:
        gh("release", "delete", r["tagName"], "--repo", REPO, "--yes", "--cleanup-tag")
        removed.append(r["tagName"])
    return removed


def main() -> int:
    changed: list[str] = []
    for feed in FEEDS:
        version, files = parse_feed(fetch(f"{ORIGIN}/{feed}").decode())
        tag = f"v{version}"
        existing = ensure_release(tag, version)
        for f in files:
            if mirror_file(tag, f, existing):
                changed.append(f"{tag}/{asset_name(f['url'])}")
    removed = prune()
    state_path = "mirrored.json"
    try:
        state = json.load(open(state_path))
    except (OSError, ValueError):
        state = {}
    stale = time.time() - state.get("committed_at", 0) > KEEPALIVE_DAYS * 86400
    if changed or removed or stale:
        # A commit keeps the scheduled workflow alive: GitHub disables schedules in public repos
        # after 60 days without repository activity.
        state = {"committed_at": int(time.time()), "last_added": changed, "last_removed": removed}
        with open(state_path + ".tmp", "w") as out:
            json.dump(state, out, indent=2)
            out.write("\n")
        os.replace(state_path + ".tmp", state_path)
        print(f"STATE_CHANGED=1 added={len(changed)} removed={len(removed)} keepalive={stale}")
        with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as o:
            o.write("state_changed=true\n")
    else:
        print("nothing to mirror")
    return 0


if __name__ == "__main__":
    sys.exit(main())
