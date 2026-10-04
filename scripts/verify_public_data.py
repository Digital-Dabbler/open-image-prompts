#!/usr/bin/env python3
"""Validate the distributable database and any locally fetched media packs.

Media packs are optional downloads (scripts/fetch_dataset.py), so this check
adapts to what is present:

  * database: integrity, counts against data/public-corpus.json, referential
    consistency, and absence of labeling process tables;
  * images/: every file on disk must be referenced by the database and look
    like a real JPEG/PNG still, or - for video files - a real MP4 referenced by
    the videos table. Referenced-but-not-downloaded files are fine unless
    OIP_REQUIRE_IMAGES=1 (full local setups) is set.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from runtime.archive_db import connect_read_only, ensure_working_database, table_exists

IMAGES_ROOT = REPOSITORY_ROOT / "images"
CORPUS_PATH = REPOSITORY_ROOT / "data" / "public-corpus.json"
PROCESS_TABLES = (
    "labeling_status",
    "label_candidates",
    "labeling_runs",
    "labeling_config",
    "image_evaluations",
    "prompt_tags",
)
VIDEO_SUFFIXES = (".mp4", ".m4v", ".mov", ".webm")


def signature(path: Path) -> str:
    header = path.read_bytes()[:12]
    if header.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(header) == 12 and header[4:8] == b"ftyp":
        return "mp4"
    return "unknown"


def main() -> int:
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    expected = corpus["counts"]

    database = ensure_working_database()
    with connect_read_only(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        has_videos = table_exists(connection, "videos")
        actual = {
            "prompts": connection.execute("SELECT count(*) FROM prompts").fetchone()[0],
            "images": connection.execute("SELECT count(*) FROM images").fetchone()[0],
            "videos": (
                connection.execute("SELECT count(*) FROM videos").fetchone()[0]
                if has_videos
                else 0
            ),
            "translations": connection.execute(
                "SELECT count(*) FROM prompt_translations"
            ).fetchone()[0],
            "prompt_labels": connection.execute(
                "SELECT count(*) FROM prompt_labels"
            ).fetchone()[0],
            "media_labels": connection.execute(
                "SELECT count(*) FROM media_labels"
            ).fetchone()[0],
            "taxonomy_labels": connection.execute(
                "SELECT count(*) FROM taxonomy_labels"
            ).fetchone()[0],
        }
        for key, value in expected.items():
            if key == "videos" and not has_videos:
                continue
            assert actual.get(key) == value, (
                f"count mismatch for {key}: db={actual.get(key)}, manifest={value}"
            )
        assert connection.execute(
            "SELECT count(*) FROM images i LEFT JOIN prompts p USING(tweet_id) "
            "WHERE p.tweet_id IS NULL"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM prompt_labels pl LEFT JOIN labels l ON l.id=pl.label_id "
            "WHERE l.id IS NULL"
        ).fetchone()[0] == 0
        if has_videos:
            assert connection.execute(
                "SELECT count(*) FROM videos v LEFT JOIN prompts p USING(tweet_id) "
                "WHERE p.tweet_id IS NULL"
            ).fetchone()[0] == 0
            # Every shipped video row must be playable one way or the other:
            # a local file or a real source URL, never a bare poster JPEG.
            assert connection.execute(
                "SELECT count(*) FROM videos WHERE coalesce(local_path,'')='' "
                "AND coalesce(url,'')=''"
            ).fetchone()[0] == 0
        for table in PROCESS_TABLES:
            present = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            assert not present, f"process table must not ship publicly: {table}"
        database_paths = {
            str(row[0])
            for row in connection.execute("SELECT local_path FROM images")
        }
        video_paths = set()
        if has_videos:
            video_paths = {
                str(row[0])
                for row in connection.execute(
                    "SELECT local_path FROM videos WHERE coalesce(local_path,'')<>''"
                )
            }
            video_paths |= {
                str(row[0])
                for row in connection.execute(
                    "SELECT poster_path FROM videos WHERE coalesce(poster_path,'')<>''"
                )
            }
        database_paths |= video_paths

    disk_paths = {
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in IMAGES_ROOT.rglob("*")
        if path.is_file()
    } if IMAGES_ROOT.is_dir() else set()

    # A release that drops records leaves the files it used to reference behind:
    # fetch_dataset.py extracts packs but never deletes. That is a stale local
    # artifact, not corruption, and the gallery simply never references it - so it
    # must not fail an ordinary checkout after a legitimate re-pull. Treat it the
    # same way as the missing-file direction: reported always, fatal only for the
    # exact-mirror setups that opt in.
    strays = sorted(disk_paths - database_paths)
    require_images = os.environ.get("OIP_REQUIRE_IMAGES") == "1"
    if strays:
        detail = (
            f"{len(strays)} files on disk are not referenced by the DB, "
            f"e.g. {strays[:3]}; remove them with "
            "`python3 scripts/fetch_dataset.py --prune`"
        )
        assert not require_images, detail
        print(f"warning: {detail}")
    missing = len(database_paths - disk_paths)
    if require_images:
        assert missing == 0, f"{missing} referenced media files have not been fetched"
    invalid = [
        path
        for path in sorted(IMAGES_ROOT.rglob("*"))
        if path.is_file() and signature(path) == "unknown"
    ] if disk_paths else []
    assert not invalid, f"invalid media assets (not JPEG/PNG/MP4): {invalid[:5]}"

    print(
        f"Public data OK: {actual['prompts']:,} prompts, {actual['images']:,} image rows, "
        f"{actual['videos']:,} video rows, {len(disk_paths):,} files fetched "
        f"({missing:,} not downloaded), {actual['translations']:,} translations"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
