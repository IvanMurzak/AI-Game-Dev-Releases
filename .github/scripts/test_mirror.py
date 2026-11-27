"""Tests for mirror.py: python3 -m unittest discover -s .github/scripts"""
import json
import os
import unittest
from unittest import mock

os.environ.setdefault("GITHUB_REPOSITORY", "owner/mirror")
import mirror  # noqa: E402

WIN = """version: 0.32.8
files:
  - url: AI Game Dev Setup 0.32.8.exe
    sha512: irC42g==
    size: 462300800
path: AI Game Dev Setup 0.32.8.exe
sha512: irC42g==
releaseDate: '2026-11-26T10:58:23.411Z'
"""

LINUX = """version: 0.32.8
files:
  - url: AI Game Dev-0.32.8-x86_64.AppImage
    sha512: OFBJ==
    size: 746828791
    blockMapSize: 775179
  - url: 'AI Game Dev-0.32.8-amd64.deb'
    sha512: uYK2==
    size: 589458596
path: AI Game Dev-0.32.8-x86_64.AppImage
sha512: OFBJ==
releaseDate: '2026-11-26T10:47:15.254Z'
"""


class RewriteFeed(unittest.TestCase):
    def test_rewrites_url_and_path_only(self):
        self.assertEqual(mirror.rewrite_feed(WIN), WIN.replace("AI Game Dev Setup 0.32.8.exe", "AI.Game.Dev.Setup.0.32.8.exe"))

    def test_keeps_quotes_sizes_and_blockmap(self):
        out = mirror.rewrite_feed(LINUX)
        self.assertIn("  - url: 'AI.Game.Dev-0.32.8-amd64.deb'\n", out)
        self.assertIn("path: AI.Game.Dev-0.32.8-x86_64.AppImage\n", out)
        self.assertIn("    blockMapSize: 775179\n", out)
        self.assertNotIn("AI Game Dev", out)
        self.assertEqual(len(out), len(LINUX))  # only spaces became dots

    def test_crlf_kept(self):
        crlf = WIN.replace("\n", "\r\n")
        self.assertEqual(mirror.rewrite_feed(crlf), mirror.rewrite_feed(WIN).replace("\n", "\r\n"))

    def test_path_outside_files_refused(self):
        with self.assertRaises(ValueError):
            mirror.rewrite_feed(WIN.replace("path: AI Game Dev Setup", "path: Other Setup"))


def release(tag, assets, draft=False, prerelease=False):
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease,
            "assets": [{"name": n, "state": "uploaded"} for n in assets]}


class MarkLatest(unittest.TestCase):
    def run_with(self, rels, latest):
        calls = []

        def gh(*args, check=True):
            calls.append(args)
            if args[:2] == ("api", "repos/owner/mirror/releases?per_page=100"):
                return json.dumps(rels)
            if args[:2] == ("api", "repos/owner/mirror/releases/latest"):
                return json.dumps({"tag_name": latest}) if latest else '{"message":"Not Found"}'
            return ""

        with mock.patch.object(mirror, "gh", gh):
            marked = mirror.mark_latest()
        edits = [c for c in calls if c[:2] == ("release", "edit")]
        return marked, edits

    def test_newest_complete_release_by_version(self):
        rels = [release("v0.33.0", ["AI.Game.Dev.Setup.0.33.0.exe"]),  # still publishing: no feeds
                release("v0.32.10", mirror.FEEDS), release("v0.32.9", mirror.FEEDS)]
        marked, edits = self.run_with(rels, "v0.33.0")
        self.assertEqual(marked, "v0.32.10")
        self.assertEqual(edits, [("release", "edit", "v0.32.10", "--repo", "owner/mirror", "--latest")])

    def test_already_latest_is_left_alone(self):
        self.assertEqual(self.run_with([release("v0.32.8", mirror.FEEDS)], "v0.32.8"), (None, []))

    def test_no_complete_release_changes_nothing(self):
        rels = [release("v0.32.8", ["latest.yml"]), release("v0.32.9", mirror.FEEDS, draft=True)]
        self.assertEqual(self.run_with(rels, None), (None, []))


class Main(unittest.TestCase):
    def run_main(self, feeds):
        published = []
        with mock.patch.object(mirror, "fetch", lambda url: feeds[url.rsplit("/", 1)[1]].encode()), \
             mock.patch.object(mirror, "ensure_release", lambda tag, version: {}), \
             mock.patch.object(mirror, "mirror_file", lambda tag, f, existing: False), \
             mock.patch.object(mirror, "publish_feed", lambda tag, feed, content: published.append((tag, feed, content)) or True), \
             mock.patch.object(mirror, "prune", lambda: []), \
             mock.patch.object(mirror, "mark_latest", lambda: None), \
             mock.patch("builtins.open", mock.mock_open(read_data="{}")), \
             mock.patch.object(mirror.os, "replace"):
            mirror.main()
        return published

    def test_feeds_published_when_all_name_one_release(self):
        published = self.run_main({"latest.yml": WIN, "latest-mac.yml": WIN, "latest-linux.yml": LINUX})
        self.assertEqual([(t, f) for t, f, _ in published], [("v0.32.8", f) for f in mirror.FEEDS])
        self.assertEqual(published[2][2], mirror.rewrite_feed(LINUX).encode())

    def test_feeds_wait_while_releases_differ(self):
        newer = WIN.replace("0.32.8", "0.32.9")
        self.assertEqual(self.run_main({"latest.yml": newer, "latest-mac.yml": WIN, "latest-linux.yml": LINUX}), [])


if __name__ == "__main__":
    unittest.main()
