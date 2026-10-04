#!/usr/bin/env python3
"""Tests for the pre-rendered SEO pages.

Runs against a synthetic dataset, so the suite needs neither the 400 MB archive
nor the image packs. Covers the rules that keep the published page tree honest:
bilingual output, canonical/noindex decisions, media URLs, sitemap contents, the
index cap, incremental rewrites and pruning.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPOSITORY_ROOT / "scripts" / "gen_seo_pages.py"

SCHEMA = """
CREATE TABLE prompts (
  tweet_id TEXT PRIMARY KEY, author TEXT NOT NULL, tool TEXT, prompt_text TEXT NOT NULL,
  created_at TEXT, tweet_url TEXT, collected_at TEXT NOT NULL
);
CREATE TABLE images (
  id INTEGER PRIMARY KEY, tweet_id TEXT NOT NULL, image_index INTEGER NOT NULL,
  url TEXT NOT NULL, local_path TEXT NOT NULL
);
CREATE TABLE videos (
  id INTEGER PRIMARY KEY, tweet_id TEXT NOT NULL, video_index INTEGER NOT NULL,
  url TEXT, local_path TEXT NOT NULL, poster_path TEXT
);
CREATE TABLE prompt_translations (
  tweet_id TEXT NOT NULL, locale TEXT NOT NULL, translated_text TEXT NOT NULL,
  translation_version TEXT NOT NULL
);
CREATE TABLE label_dimensions (id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, name TEXT NOT NULL);
CREATE TABLE labels (id INTEGER PRIMARY KEY, dimension_id INTEGER NOT NULL, key TEXT NOT NULL, name TEXT NOT NULL);
CREATE TABLE prompt_labels (
  tweet_id TEXT NOT NULL, label_id INTEGER NOT NULL, confidence REAL, taxonomy_version TEXT NOT NULL
);
CREATE TABLE taxonomy_labels (
  taxonomy_version TEXT, dimension_key TEXT, key TEXT, label_id INTEGER,
  display_en TEXT, display_zh TEXT, aliases_en_json TEXT, aliases_zh_json TEXT
);
"""

LONG_PROMPT = (
    "A cinematic portrait of a lighthouse keeper at dusk, shot on 85mm film, "
    "soft rim light, wet stone textures, volumetric fog, muted teal palette, "
    "shallow depth of field, Kodak Portra grain, and a calm, resolute expression. "
) * 6


def build_dataset(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO label_dimensions VALUES (1,'visual_style','Visual style')"
    )
    connection.execute(
        "INSERT INTO labels VALUES (1,1,'cinematic','cinematic')"
    )
    connection.execute(
        "INSERT INTO taxonomy_labels VALUES ('oip-visual-v2','visual_style','cinematic',1,"
        "'cinematic','电影感','[]','[]')"
    )
    prompts = [
        # indexable: long prompt, media, translation
        ("1001", "alice", "Nano Banana", LONG_PROMPT, "2026-09-01T10:00:00Z"),
        ("1002", "alice", "Nano Banana", LONG_PROMPT, "2026-09-02T10:00:00Z"),
        ("1003", "bob", "Nano Banana", LONG_PROMPT, "2026-09-03T10:00:00Z"),
        # not indexable: prompt too short
        ("1004", "bob", "Nano Banana", "short prompt", "2026-09-04T10:00:00Z"),
        # not indexable: no media
        ("1005", "carol", "Midjourney", LONG_PROMPT, "2026-09-05T10:00:00Z"),
    ]
    for tweet_id, author, tool, prompt, created in prompts:
        connection.execute(
            "INSERT INTO prompts VALUES (?,?,?,?,?,?,?)",
            (
                tweet_id,
                author,
                tool,
                prompt,
                created,
                f"https://x.com/{author}/status/{tweet_id}",
                created,
            ),
        )
        connection.execute(
            "INSERT INTO prompt_translations VALUES (?,?,?,?)",
            (tweet_id, "zh-Hans", f"中文译文 {tweet_id}", "oip-visual-v2"),
        )
        connection.execute(
            "INSERT INTO prompt_labels VALUES (?,?,?,?)", (tweet_id, 1, 1.0, "oip-visual-v2")
        )
    connection.execute(
        "INSERT INTO images VALUES (1,'1001',1,'https://pbs.twimg.com/media/a.jpg','images/1001/1.jpg')"
    )
    connection.execute(
        "INSERT INTO images VALUES (2,'1002',1,'https://pbs.twimg.com/media/b.jpg','images/1002/1.jpg')"
    )
    connection.execute(
        "INSERT INTO videos VALUES (1,'1003',1,'https://video.twimg.com/v.mp4',"
        "'images/1003/video_1.mp4','images/1003/video_1.jpg')"
    )
    connection.commit()
    connection.close()


class SeoPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="oip-seo-test-"))
        cls.database = cls.tmp / "prompts.db"
        build_dataset(cls.database)
        cls.out = cls.tmp / "pages"
        cls.run_generator()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def run_generator(cls, *extra: str, out: Path | None = None, database: Path | None = None,
                      lower_thresholds: bool = True) -> subprocess.CompletedProcess:
        environment = dict(os.environ)
        if lower_thresholds:
            # The shipped thresholds assume a 20k-record corpus; the fixture is
            # tiny, so hubs would never appear. Lower them for the fixture only.
            environment.update(
                {
                    "OIP_SEO_MIN_TOOL_COUNT": "2",
                    "OIP_SEO_MIN_TAG_COUNT": "2",
                    "OIP_SEO_MIN_AUTHOR_COUNT": "2",
                }
            )
        return subprocess.run(
            [
                sys.executable,
                str(GENERATOR),
                "--db",
                str(database or cls.database),
                "--out",
                str(out or cls.out),
                "--min-chars",
                "400",
                *extra,
            ],
            capture_output=True,
            text=True,
            cwd=REPOSITORY_ROOT,
            env=environment,
        )

    def read(self, relative: str) -> str:
        return (self.out / relative).read_text(encoding="utf-8")

    def test_generator_succeeds(self) -> None:
        self.assertIn("written", self.run_generator().stdout)

    def test_pages_carry_the_analytics_tag(self) -> None:
        # Organic landings arrive on the pre-rendered pages, never on the SPA
        # shell, so the measurement tag has to be baked into every page.
        page = self.read("p/1001/index.html")
        self.assertIn("googletagmanager.com/gtag/js?id=G-", page)
        self.assertIn("gtag('config','G-", page)

    def test_detail_page_carries_both_languages_and_provenance(self) -> None:
        page = self.read("p/1001/index.html")
        self.assertIn("lang=\"en\"", page)          # byte-for-byte original
        self.assertIn("中文译文 1001", page)         # reading translation
        self.assertIn("https://x.com/alice/status/1001", page)
        self.assertIn("rel=\"canonical\" href=\"https://openimages.relakkesyang.org/p/1001/\"", page)

    def test_detail_page_structured_data_parses(self) -> None:
        page = self.read("p/1001/index.html")
        block = re.search(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)
        self.assertIsNotNone(block)
        graph = json.loads(block.group(1))["@graph"]
        types = [node["@type"] for node in graph]
        self.assertIn("CreativeWork", types)
        self.assertIn("BreadcrumbList", types)
        work = next(node for node in graph if node["@type"] == "CreativeWork")
        self.assertEqual(work["isBasedOn"], "https://x.com/alice/status/1001")
        self.assertEqual(work["inLanguage"], "en")
        self.assertTrue(work["image"]["contentUrl"].startswith("https://"))

    def test_video_page_exposes_video_object(self) -> None:
        page = self.read("p/1003/index.html")
        self.assertIn("<video ", page)
        graph = json.loads(
            re.search(r'<script type="application/ld\+json">(.*?)</script>', page, re.S).group(1)
        )["@graph"]
        work = next(node for node in graph if node["@type"] == "CreativeWork")
        self.assertEqual(work["video"]["@type"], "VideoObject")
        self.assertTrue(work["video"]["contentUrl"].endswith("/images/1003/video_1.mp4"))
        self.assertTrue(work["video"]["thumbnailUrl"].endswith("/images/1003/video_1.jpg"))

    def test_local_media_paths_are_not_double_prefixed(self) -> None:
        for relative in ("p/1001/index.html", "p/1003/index.html"):
            self.assertNotIn("/images/images/", self.read(relative))

    def test_thin_records_are_noindex(self) -> None:
        self.assertIn('content="noindex, follow"', self.read("p/1004/index.html"))
        self.assertNotIn("noindex", self.read("p/1001/index.html"))

    def test_sitemap_lists_only_indexable_pages(self) -> None:
        sitemap = self.read("sitemap-1.xml")
        self.assertIn("/p/1001/", sitemap)
        self.assertNotIn("/p/1004/", sitemap)
        self.assertNotIn("/p/1005/", sitemap)
        self.assertIn("<sitemap>", self.read("sitemap.xml"))

    def test_index_limit_caps_indexable_pages(self) -> None:
        limited = self.tmp / "pages-limited"
        result = self.run_generator("--index-limit", "1", out=limited)
        self.assertIn("1 indexable", result.stdout)
        sitemap = (limited / "sitemap-1.xml").read_text(encoding="utf-8")
        # Hubs are indexable on their own; the cap applies to prompt pages.
        self.assertEqual(sitemap.count("<loc>https://openimages.relakkesyang.org/p/"), 1)
        shutil.rmtree(limited, ignore_errors=True)

    def test_hub_pages_exist_for_qualified_dimensions_only(self) -> None:
        self.assertTrue((self.out / "tool" / "nano-banana" / "index.html").is_file())
        self.assertFalse((self.out / "tool" / "midjourney" / "index.html").exists())
        self.assertTrue((self.out / "tag" / "visual-style" / "cinematic" / "index.html").is_file())

    def test_second_run_is_incremental_and_prunes_deleted_records(self) -> None:
        # Isolated fixture: this test deletes a record on purpose.
        sandbox = self.tmp / "prune"
        sandbox.mkdir()
        database = sandbox / "prompts.db"
        shutil.copyfile(self.database, database)
        out = sandbox / "pages"
        self.assertIn("written", self.run_generator(out=out, database=database).stdout)
        self.assertIn("0 written", self.run_generator(out=out, database=database).stdout)
        self.assertTrue((out / "p" / "1001" / "index.html").is_file())
        connection = sqlite3.connect(database)
        connection.execute("DELETE FROM prompts WHERE tweet_id='1001'")
        connection.commit()
        connection.close()
        third = self.run_generator(out=out, database=database)
        self.assertIn("removed", third.stdout)
        self.assertFalse((out / "p" / "1001" / "index.html").exists())
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
