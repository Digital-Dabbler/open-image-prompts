#!/usr/bin/env python3
"""Download the video files the dataset references but does not ship.

Every video row in the archive carries two things: the original
`video.twimg.com` URL and the exact local path the gallery requests —
`images/<tweet_id>/video_<index>.mp4`. The published packs ship images and
poster frames only (2,079 clips would add ~8.8 GB), so a fresh clone shows a
poster but cannot play the clip: the player falls back to X's CDN, which is
unreachable from mainland China, expires over time and blocks hotlinked
playback.

This script closes that gap: it pulls each clip from its original source into
the path the database already points at, so the player finds a local file and
never needs the fallback.

    python3 scripts/fetch_videos.py                 # every missing clip
    python3 scripts/fetch_videos.py --dry-run       # print the plan, download nothing
    python3 scripts/fetch_videos.py --limit 20      # small batch first
    python3 scripts/fetch_videos.py --tweet-id 2109571731576750427,2067200831137464362
    python3 scripts/fetch_videos.py --jobs 8 --report /tmp/videos.json

Requires reachable access to video.twimg.com: run it from a network that can
reach X directly (a workstation or VPS outside mainland China, or with a VPN)
or point it at a proxy:

    python3 scripts/fetch_videos.py --proxy http://127.0.0.1:7897
    HTTPS_PROXY=http://127.0.0.1:7897 python3 scripts/fetch_videos.py

The pairing is always row by row — the URL and the destination path come from
the same database row, so a clip can never land in another tweet's directory.
Safe to re-run: valid files are skipped, interrupted downloads resume from the
kept `.part` file, and a JSON report lists whatever still failed.

Writes only under images/** (gitignored), plus the optional report file.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
IMAGES_ROOT = REPOSITORY_ROOT / "images"
CHUNK = 1024 * 1024
# Same rule the frontend applies (web/src/media.js isVideoLikeUrl): a row whose
# `url` is a poster JPEG or empty has no clip to fetch.
VIDEO_URL = re.compile(r"\.(mp4|m4v|mov|webm)(\?|$)", re.IGNORECASE)
# X's video CDN answers browsers, not bare clients, and rejects requests without
# a plausible user agent.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# An MP4 begins with a size field then the `ftyp` box type at offset 4.
MP4_MAGIC_OFFSET = 4
MP4_MAGIC = b"ftyp"
MIN_BYTES = 1024


class Row:
    """One video row: the clip URL and the path it must land on."""

    __slots__ = ("tweet_id", "index", "url", "target", "note")

    def __init__(self, tweet_id: str, index: int, url: str, target: Path, note: str = "") -> None:
        self.tweet_id = tweet_id
        self.index = index
        self.url = url
        self.target = target
        self.note = note


def expected_relative_path(tweet_id: str, index: int) -> str:
    return f"images/{tweet_id}/video_{index}.mp4"


def plan_rows(only_ids: set[str] | None = None) -> tuple[list[Row], int]:
    """Every download-able row, paired with the path the database expects.

    Returns (rows, skipped_rows) — skipped counts rows with no usable source URL
    (an empty `url`, or a poster JPEG older rows kept there).
    """
    sys.path.insert(0, str(REPOSITORY_ROOT))
    from runtime.archive_db import connect_read_only, ensure_working_database, table_exists

    database = ensure_working_database()
    with connect_read_only(database) as connection:
        if not table_exists(connection, "videos"):
            return [], 0
        records = connection.execute(
            "SELECT tweet_id, video_index, url, local_path FROM videos ORDER BY tweet_id, video_index"
        ).fetchall()

    rows: list[Row] = []
    skipped = 0
    for record in records:
        tweet_id = str(record["tweet_id"])
        index = int(record["video_index"] or 1)
        url = (record["url"] or "").strip()
        if only_ids and tweet_id not in only_ids:
            continue
        if not VIDEO_URL.search(url):
            skipped += 1
            continue
        # Trust the row's own local_path so the file lands exactly where the API
        # serves it; fall back to the canonical layout and say so when a row
        # disagrees, rather than silently writing somewhere else.
        declared = (record["local_path"] or "").strip()
        canonical = expected_relative_path(tweet_id, index)
        note = ""
        if declared and declared != canonical:
            note = f"row asked for {declared}; using {canonical}"
        rows.append(Row(tweet_id, index, url, REPOSITORY_ROOT / canonical, note))
    return rows, skipped


def is_valid_clip(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < MIN_BYTES:
        return False
    with path.open("rb") as handle:
        handle.seek(MP4_MAGIC_OFFSET)
        return handle.read(len(MP4_MAGIC)) == MP4_MAGIC


def opener_for(proxy: str | None) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def download(row: Row, opener: urllib.request.OpenerDirector, timeout: float) -> tuple[str, str]:
    """Fetch one clip. Returns (status, detail) with status in ok/skip/failed."""
    if is_valid_clip(row.target):
        return "skip", "already present"
    row.target.parent.mkdir(parents=True, exist_ok=True)
    partial = row.target.with_suffix(row.target.suffix + ".part")

    # Resume an interrupted transfer when the source honors Range requests.
    resume_from = partial.stat().st_size if partial.is_file() else 0
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"
    request = urllib.request.Request(row.url, headers=headers)
    mode = "ab" if resume_from else "wb"
    try:
        with opener.open(request, timeout=timeout) as response, partial.open(mode) as out:
            if resume_from and response.status != 206:
                # Server ignored the range: start over instead of corrupting.
                out.close()
                partial.unlink(missing_ok=True)
                return download(Row(row.tweet_id, row.index, row.url, row.target), opener, timeout)
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                out.write(chunk)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as error:
        size = partial.stat().st_size if partial.is_file() else 0
        return "failed", f"{type(error).__name__}: {error} (kept {size} bytes)"

    if not is_valid_clip(partial):
        partial.unlink(missing_ok=True)
        return "failed", "downloaded file is not an MP4"
    partial.replace(row.target)
    return "ok", f"{row.target.stat().st_size / 1048576:.1f} MB"


def parse_ids(raw: list[str] | None) -> set[str] | None:
    if not raw:
        return None
    ids: set[str] = set()
    for chunk in raw:
        for piece in chunk.replace(" ", "").split(","):
            if piece:
                ids.add(piece)
    return ids or None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tweet-id", action="append", help="only these tweet ids (repeatable or comma separated)")
    parser.add_argument("--limit", type=int, default=0, help="stop after N clips that still need downloading")
    parser.add_argument("--jobs", type=int, default=4, help="parallel downloads (default 4; proxies dislike more)")
    parser.add_argument("--retries", type=int, default=3, help="attempts per clip (default 3)")
    parser.add_argument("--timeout", type=float, default=60.0, help="per-request timeout in seconds")
    parser.add_argument("--proxy", help="proxy for video.twimg.com (default: HTTPS_PROXY from the environment)")
    parser.add_argument("--report", type=Path, help="write a JSON report of failures here")
    parser.add_argument("--dry-run", action="store_true", help="list what would be downloaded and exit")
    args = parser.parse_args()

    only_ids = parse_ids(args.tweet_id)
    rows, skipped = plan_rows(only_ids)
    pending = [row for row in rows if not is_valid_clip(row.target)]
    if args.limit:
        pending = pending[: args.limit]

    print(f"  video rows with a source URL: {len(rows)}")
    if skipped:
        print(f"  rows skipped (no video URL — poster or empty): {skipped}")
    print(f"  already on disk: {len(rows) - len([r for r in rows if not is_valid_clip(r.target)])}")
    print(f"  to download: {len(pending)}")
    for row in pending[:5]:
        print(f"    {row.tweet_id}#{row.index} -> {row.target.relative_to(REPOSITORY_ROOT)}")
        if row.note:
            print(f"      note: {row.note}")
    if len(pending) > 5:
        print(f"    … and {len(pending) - 5} more")

    if args.dry_run or not pending:
        if args.dry_run:
            print("  dry run: nothing downloaded")
        return 0

    opener = opener_for(args.proxy)
    counts = {"ok": 0, "skip": 0, "failed": 0}
    failures: list[dict[str, object]] = []
    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(attempt_row, row, opener, args.timeout, args.retries): row for row in pending}
        for number, future in enumerate(futures, start=1):
            row = futures[future]
            status, detail = future.result()
            counts[status] += 1
            mark = {"ok": "✓", "skip": "=", "failed": "✗"}[status]
            print(f"  [{number}/{len(pending)}] {mark} {row.tweet_id}#{row.index} {detail}")
            if status == "failed":
                failures.append({"tweet_id": row.tweet_id, "index": row.index, "url": row.url, "error": detail})

    elapsed = time.time() - started
    print(
        f"  done in {elapsed:.0f}s — downloaded {counts['ok']}, "
        f"already present {counts['skip']}, failed {counts['failed']}"
    )
    if failures:
        print("  failed clips (re-run the same command to retry just these):")
        for failure in failures:
            print(f"    {failure['tweet_id']}#{failure['index']}  {failure['error']}")
        if args.report:
            args.report.write_text(json.dumps({"failed": failures}, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  report written to {args.report}")
        return 1
    return 0


def attempt_row(row: Row, opener: urllib.request.OpenerDirector, timeout: float, retries: int) -> tuple[str, str]:
    last = ("failed", "no attempt")
    for attempt in range(1, max(1, retries) + 1):
        status, detail = download(row, opener, timeout)
        if status != "failed":
            return status, detail
        last = (status, detail)
        if attempt < retries:
            time.sleep(min(2 ** attempt, 15))
    return last


if __name__ == "__main__":
    raise SystemExit(main())
