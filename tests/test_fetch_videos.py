#!/usr/bin/env python3
"""Tests for scripts/fetch_videos.py — offline, no X and no dataset required."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

spec = importlib.util.spec_from_file_location("fetch_videos", REPOSITORY_ROOT / "scripts" / "fetch_videos.py")
fetch_videos = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_videos)

# A byte-identical stand-in for a clip: size box, `ftyp` at offset 4, then filler.
FAKE_MP4 = (2000).to_bytes(4, "big") + b"ftypisom" + b"\0" * 2000


class FixtureHandler(BaseHTTPRequestHandler):
    """Serves /ok.mp4 (200), /missing.mp4 (404) and /poster.jpg (an image)."""

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if self.path == "/ok.mp4":
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(FAKE_MP4)))
            self.end_headers()
            self.wfile.write(FAKE_MP4)
            return
        if self.path == "/poster.jpg":
            body = b"\xff\xd8\xff" + b"\0" * 1500
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, *args) -> None:  # keep the test output clean
        return


class FetchVideosTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def row(self, tweet_id: str, index: int, url: str) -> fetch_videos.Row:
        target = self.root / fetch_videos.expected_relative_path(tweet_id, index)
        return fetch_videos.Row(tweet_id, index, url, target)

    def test_canonical_path_is_the_one_the_gallery_requests(self) -> None:
        # The whole point of the tool: the clip must land on exactly the path the
        # database (and therefore the API and the <video> element) points at.
        self.assertEqual(
            fetch_videos.expected_relative_path("2109571731576750427", 1),
            "images/2109571731576750427/video_1.mp4",
        )
        self.assertEqual(
            fetch_videos.expected_relative_path("2109571731576750427", 2),
            "images/2109571731576750427/video_2.mp4",
        )

    def test_download_writes_a_valid_clip_and_reruns_skip_it(self) -> None:
        row = self.row("1001", 1, f"{self.base}/ok.mp4")
        status, detail = fetch_videos.download(row, fetch_videos.opener_for(None), 15.0)
        self.assertEqual(status, "ok", detail)
        self.assertTrue(row.target.is_file())
        self.assertTrue(fetch_videos.is_valid_clip(row.target))
        self.assertEqual(row.target.read_bytes(), FAKE_MP4)
        # Idempotent: a second pass never re-downloads.
        status, detail = fetch_videos.download(row, fetch_videos.opener_for(None), 15.0)
        self.assertEqual((status, detail), ("skip", "already present"))

    def test_rows_never_mix_up_their_destination(self) -> None:
        # Two clips fetched in one run: each one lands under its own tweet id.
        first = self.row("1001", 1, f"{self.base}/ok.mp4")
        second = self.row("1002", 1, f"{self.base}/ok.mp4")
        opener = fetch_videos.opener_for(None)
        self.assertEqual(fetch_videos.download(first, opener, 15.0)[0], "ok")
        self.assertEqual(fetch_videos.download(second, opener, 15.0)[0], "ok")
        self.assertEqual(first.target.parent.name, "1001")
        self.assertEqual(second.target.parent.name, "1002")
        self.assertEqual(first.target.read_bytes(), second.target.read_bytes())

    def test_a_missing_source_fails_without_leaving_a_file(self) -> None:
        row = self.row("1003", 1, f"{self.base}/missing.mp4")
        status, detail = fetch_videos.download(row, fetch_videos.opener_for(None), 15.0)
        self.assertEqual(status, "failed")
        self.assertIn("HTTPError", detail)
        self.assertFalse(row.target.exists())

    def test_a_non_video_response_is_rejected(self) -> None:
        # A poster JPEG or an error page must never be saved as a clip.
        row = self.row("1004", 1, f"{self.base}/poster.jpg")
        status, detail = fetch_videos.download(row, fetch_videos.opener_for(None), 15.0)
        self.assertEqual(status, "failed", detail)
        self.assertFalse(row.target.exists())

    def test_plan_pairs_each_row_with_its_own_path(self) -> None:
        with sqlite3.connect(self.root / "prompts.db") as connection:
            connection.execute(
                "CREATE TABLE videos (tweet_id TEXT, video_index INTEGER, url TEXT, local_path TEXT)"
            )
            connection.executemany(
                "INSERT INTO videos VALUES (?,?,?,?)",
                [
                    ("2001", 1, "https://video.twimg.com/amplify_video/1/vid/a.mp4?tag=29",
                     "images/2001/video_1.mp4"),
                    ("2002", 1, "https://pbs.twimg.com/media/poster.jpg", "images/2002/video_1.mp4"),
                    ("2003", 1, "", ""),
                    # A row whose stored path disagrees still lands canonically.
                    ("2004", 2, "https://video.twimg.com/amplify_video/4/vid/d.mp4",
                     "images/2004/other.mp4"),
                ],
            )

        fake = type(sys)("runtime.archive_db")

        def fake_connect(path):
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row  # what connect_read_only provides
            return connection

        fake.connect_read_only = fake_connect
        fake.ensure_working_database = lambda: self.root / "prompts.db"
        fake.table_exists = lambda connection, name: bool(
            connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()
        )
        runtime = type(sys)("runtime")
        runtime.archive_db = fake
        sys.modules["runtime"] = runtime
        sys.modules["runtime.archive_db"] = fake
        self.addCleanup(sys.modules.pop, "runtime.archive_db", None)
        self.addCleanup(sys.modules.pop, "runtime", None)

        original_root = fetch_videos.REPOSITORY_ROOT
        fetch_videos.REPOSITORY_ROOT = self.root
        self.addCleanup(setattr, fetch_videos, "REPOSITORY_ROOT", original_root)

        rows, skipped = fetch_videos.plan_rows()
        self.assertEqual(skipped, 2, "poster JPEG and empty url rows are not downloadable")
        self.assertEqual([row.tweet_id for row in rows], ["2001", "2004"])
        # Each row keeps its own source URL — never the neighbouring row's.
        self.assertIn("amplify_video/1/", rows[0].url)
        self.assertIn("amplify_video/4/", rows[1].url)
        self.assertEqual(rows[0].target.relative_to(self.root).as_posix(), "images/2001/video_1.mp4")
        # 2004 asked for images/2004/other.mp4 but the canonical path wins, loudly.
        self.assertEqual(rows[1].target.relative_to(self.root).as_posix(), "images/2004/video_2.mp4")
        self.assertIn("images/2004/other.mp4", rows[1].note)


if __name__ == "__main__":
    unittest.main()
