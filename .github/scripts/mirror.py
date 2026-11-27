#!/usr/bin/env python3
"""Mirror the published AI Game Dev desktop builds from downloads.ai-game.dev into this repo's Releases.

downloads.ai-game.dev (Cloudflare R2) stays the source of truth. This script only copies what the
update feeds already list, byte for byte, and refuses anything whose size or sha512 does not match
the feed. The Cloudflare Worker in front of downloads.ai-game.dev redirects clients in regions with
a slow path to Cloudflare to these assets, and falls back to R2 when an asset is not mirrored yet.

Asset naming: GitHub turns spaces in an asset name into dots, so every asset is uploaded as
`name.replace(" ", ".")` under the tag `v<version>`. The Worker applies the same transform.

Update feeds: once every file that all three feeds list for one version is in its release, that
release also gets `latest.yml`, `latest-mac.yml` and `latest-linux.yml`: the R2 feeds with each
`url`/`path` rewritten to the asset name (sha512 and size unchanged). The release the official feeds
name is kept marked latest once it carries all three feeds (until then the newest one that does), so `https://github.com/<repo>/releases/latest/download` works
as an electron-updater generic feed base: `<base>/latest.yml` and `<base>/<asset>`.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
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
DOWNLOAD_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 30


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


def rewrite_feed(text: str) -> str:
    """The feed with every `url:`/`path:` value that names one of its files replaced by the asset
    name; every other byte is kept. The result must parse to the same version, sizes and sha512s."""
    version, files = parse_feed(text)
    urls = {f["url"] for f in files}

    def sub(m: re.Match) -> str:
        key, value = m.group(2), m.group(4)
        if value not in urls:
            raise ValueError(f"feed {key} {value!r} is not one of its files")
        return f"{m.group(1)}{m.group(3)}{asset_name(value)}{m.group(3)}{m.group(5)}"

    out = re.sub(r"(?m)^(\s*(?:-\s+)?(url|path):[ \t]*)(['\"]?)(.*?)\3([ \t]*\r?)$", sub, text)
    new_version, new_files = parse_feed(out)
    expected = [{**f, "url": asset_name(f["url"])} for f in files]
    if new_version != version or new_files != expected:
        raise ValueError("rewritten feed does not describe the same files")
    return out


def release_assets(tag: str) -> dict[str, int] | None:
    """Uploaded assets by name -> size. An asset left behind by an interrupted upload (state other
    than "uploaded") is deleted here so the next upload can replace it; otherwise it would block
    that file forever."""
    out = gh("release", "view", tag, "--repo", REPO, "--json", "assets", check=False)
    if not out.strip():
        return None
    assets = {}
    for a in json.loads(out)["assets"]:
        if a.get("state", "uploaded") != "uploaded":
            gh("release", "delete-asset", tag, a["name"], "--repo", REPO, "--yes")
            print(f"deleted incomplete asset {tag}/{a['name']} (state {a.get('state')})")
            continue
        assets[a["name"]] = a["size"]
    return assets


def download(src: str, path: str, size: int, sha512: str) -> None:
    """One whole download to path; a cut connection can end the body early without an error, so
    the bytes are always checked here."""
    h = hashlib.sha512()
    n = 0
    with urllib.request.urlopen(urllib.request.Request(src, headers=UA), timeout=600) as r, open(path, "wb") as out:
        while chunk := r.read(1 << 20):
            h.update(chunk)
            n += len(chunk)
            out.write(chunk)
    digest = base64.b64encode(h.digest()).decode()
    if n != size or digest != sha512:
        raise RuntimeError(f"{src}: got {n} bytes sha512 {digest}, feed says {size} / {sha512}")


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
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                download(src, path, size, f["sha512"])
                break
            except (OSError, http.client.HTTPException, RuntimeError) as error:
                if attempt == DOWNLOAD_ATTEMPTS:
                    raise
                print(f"retrying {name} (attempt {attempt} of {DOWNLOAD_ATTEMPTS}): {error}")
                time.sleep(RETRY_DELAY_SECONDS)
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
        # While no release is marked latest explicitly, GitHub treats the newest one as latest even
        # with --latest=false: pin the complete one again right away, or `releases/latest/download`
        # would 404 for the whole time this release's files take to copy.
        mark_latest(list_releases())
        existing = {}
    return existing


def sha256(content: bytes) -> str:
    """In the form of GitHub's asset `digest`."""
    return "sha256:" + hashlib.sha256(content).hexdigest()


def asset_digests(tag: str) -> dict[str, str]:
    out = gh("release", "view", tag, "--repo", REPO, "--json", "assets")
    return {a["name"]: a.get("digest") for a in json.loads(out)["assets"] if a.get("state") == "uploaded"}


def publish_feeds(tag: str, feeds: dict[str, bytes]) -> list[str]:
    """Upload each rewritten feed the release does not already hold byte for byte (by GitHub's
    sha256 digest), then check the digests again. Only called once every file of the release is
    mirrored. Returns the feeds uploaded."""
    have = asset_digests(tag)
    todo = {feed: content for feed, content in feeds.items() if have.get(feed) != sha256(content)}
    if not todo:
        return []
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for feed, content in todo.items():
            paths.append(os.path.join(d, feed))
            with open(paths[-1], "wb") as out:
                out.write(content)
        gh("release", "upload", tag, *paths, "--repo", REPO, "--clobber")
    after = asset_digests(tag)
    bad = [feed for feed, content in todo.items() if after.get(feed) != sha256(content)]
    if bad:
        raise RuntimeError(f"uploaded {tag} feeds {bad} but the release holds other bytes")
    print(f"published feeds {tag}: {', '.join(todo)}")
    return list(todo)


def version_key(tag: str) -> tuple[int, ...] | None:
    m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag)
    return tuple(map(int, m.groups())) if m else None


def list_releases() -> list[dict]:
    return json.loads(gh("api", f"repos/{REPO}/releases?per_page=100"))


def mark_latest(rels: list[dict], official: str | None = None) -> str | None:
    """Keep the release the official feeds name marked latest once it carries all three feeds, else
    the newest release that does (GitHub may pick another one on its own, e.g. after the latest
    release is deleted or a new one is created). Returns the tag it marked, if any."""
    complete = [r["tag_name"] for r in rels
                if not r["draft"] and not r["prerelease"] and version_key(r["tag_name"])
                and set(FEEDS) <= {a["name"] for a in r["assets"] if a["state"] == "uploaded"}]
    if not complete:
        return None
    want = official if official in complete else max(complete, key=version_key)
    current = json.loads(gh("api", f"repos/{REPO}/releases/latest", check=False) or "{}").get("tag_name")
    if current == want:
        return None
    gh("release", "edit", want, "--repo", REPO, "--latest")
    print(f"marked {want} latest (was {current})")
    return want


def prune(rels: list[dict]) -> list[str]:
    removed = []
    for r in sorted(rels, key=lambda r: r["created_at"], reverse=True)[KEEP_VERSIONS:]:
        gh("release", "delete", r["tag_name"], "--repo", REPO, "--yes", "--cleanup-tag")
        removed.append(r["tag_name"])
    return removed


def main() -> int:
    changed: list[str] = []
    feeds: dict[str, tuple[str, str]] = {}
    for feed in FEEDS:
        text = fetch(f"{ORIGIN}/{feed}").decode()
        version, files = parse_feed(text)
        tag = f"v{version}"
        existing = ensure_release(tag, version)
        for f in files:
            if mirror_file(tag, f, existing):
                changed.append(f"{tag}/{asset_name(f['url'])}")
        feeds[feed] = (tag, text)
    # Every file of every feed is mirrored and verified at this point (mirror_file raises
    # otherwise). The feeds go up only when all three name the same release, so a release that
    # carries feeds holds every platform's files; while a release is still being published on R2,
    # the previous one stays latest.
    # A feed the rewrite refuses fails the pass (red run) but never stops the mirroring above or
    # the housekeeping below. Replacing a feed that changed (--clobber) deletes the old asset first,
    # a short 404 on that one file; R2 refuses new bytes under an existing name, so it is rare.
    tags = {tag for tag, _ in feeds.values()}
    official = next(iter(tags)) if len(tags) == 1 else None
    feed_error = False
    if official:
        try:
            rewritten = {feed: rewrite_feed(text).encode() for feed, (_, text) in feeds.items()}
        except ValueError as error:
            feed_error = True
            print(f"::error::feeds of {official} not published: {error}")
        else:
            changed += [f"{official}/{feed}" for feed in publish_feeds(official, rewritten)]
    else:
        print(f"feeds name different releases {sorted(tags)}; feed files wait")
    rels = list_releases()
    removed = prune(rels)
    marked = mark_latest([r for r in rels if r["tag_name"] not in removed], official)
    if marked:
        changed.append(f"{marked} latest")
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
    return 1 if feed_error else 0


if __name__ == "__main__":
    sys.exit(main())
