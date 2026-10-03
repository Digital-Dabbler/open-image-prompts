#!/usr/bin/env python3
"""Pre-render crawlable pages for the archive: one page per prompt plus hubs.

The gallery itself is a JavaScript app, so a crawler that reaches it gets an empty
shell. This script turns the published dataset into plain HTML that needs no
JavaScript at all:

    p/<tweet_id>/index.html          one page per prompt, both languages on it
    tool/<slug>/index.html           tool hubs (paginated above PAGE_SIZE)
    u/<author>/index.html            author hubs
    tag/<dimension>/<slug>/          taxonomy hubs
    archive/<YYYY-MM>/               monthly archives
    about/, takedown/                trust pages
    robots.txt, sitemap*.xml         crawl entry points

Design notes (decided 2026-10-03 after six parallel research passes):

* **One URL per prompt, both languages on the page.** The prompt is one work; the
  Chinese reading translation is an auxiliary field, exactly as
  ``AGENTS.md`` requires. Splitting zh/en into separate URLs would double the
  corpus for two near-identical documents and force a hreflang triangle on a
  brand-new domain.
* **Static, generated in the deploy pipeline.** The production API is a stdlib
  ``http.server`` behind a four-slot gate on a 2-core box; rendering per request
  would crush it. Pages are written by ``npm run build`` (see
  ``scripts/build_seo_pages.mjs``) and served straight off disk by Caddy.
* **Incremental and self-pruning.** A manifest of ``path -> sha256`` means a
  re-run rewrites only what changed (seconds instead of minutes) and deletes
  pages whose record disappeared.
* **Only vetted records are indexable.** Everything is generated (users and
  internal linking benefit), but ``noindex`` and an absent sitemap entry keep
  thin records out of the index until they are reviewed.

Usage:

    python3 scripts/gen_seo_pages.py --out /var/www/oip-pages
    python3 scripts/gen_seo_pages.py --out /tmp/pages --limit 50 --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

REPO_ROOT = Path(__file__).resolve().parents[1]
SITE = os.getenv("OIP_SEO_SITE", "https://openimages.relakkesyang.org").rstrip("/")
TAXONOMY = os.getenv("OIP_SEO_TAXONOMY", "oip-visual-v2")

# Media bases. Videos are archived on this host; the 35k stills are not (owned by
# X's CDN) until they move to object storage, so a prefix can be configured later
# without touching the templates. `OIP_SEO_IMAGE_BASE` empty means "use each
# record's original source URL".
IMAGE_BASE = os.getenv("OIP_SEO_IMAGE_BASE", "").rstrip("/")
VIDEO_BASE = os.getenv("OIP_SEO_VIDEO_BASE", SITE).rstrip("/")

PAGE_SIZE = 48
SITEMAP_CHUNK = 45000
# Indexability gates: a page earns an index entry only when it carries a real
# prompt, at least one archived medium, and a Chinese reading translation. The
# cap keeps the first release to a reviewed pilot instead of 20k pages at once.
MIN_PROMPT_CHARS = int(os.getenv("OIP_SEO_MIN_PROMPT_CHARS", "400"))
MIN_TAG_COUNT = int(os.getenv("OIP_SEO_MIN_TAG_COUNT", "10"))
MIN_TOOL_COUNT = int(os.getenv("OIP_SEO_MIN_TOOL_COUNT", "20"))
MIN_AUTHOR_COUNT = int(os.getenv("OIP_SEO_MIN_AUTHOR_COUNT", "5"))
INDEX_LIMIT = int(os.getenv("OIP_SEO_INDEX_LIMIT", "4000"))
MANIFEST_NAME = ".seo-manifest.json"

TRUST_PAGES = {
    "about": (
        "About this archive",
        "How Open Image Prompts collects, verifies and publishes AI prompts.",
    ),
    "takedown": (
        "Creator opt-out and takedown",
        "How a creator asks for their prompt or media to be removed.",
    ),
}

CSS = """
:root{--bg:#09090b;--panel:#111113;--ink:#f4f3ee;--muted:#a1a1aa;--line:#27272a;--brass:#e2a75b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans SC",sans-serif}
main{max-width:960px;margin:0 auto;padding:28px 20px 72px}
a{color:var(--brass);text-decoration:none}a:hover{text-decoration:underline}
nav.crumbs{font-size:13px;color:var(--muted);margin-bottom:22px}
nav.crumbs a{color:var(--muted)}
h1{font-size:clamp(22px,3.6vw,34px);line-height:1.3;margin:0 0 10px}
.meta{color:var(--muted);font-size:14px;margin:0 0 22px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:14px 0 0}
.chip{border:1px solid var(--line);border-radius:999px;padding:3px 10px;font-size:12px;color:var(--muted)}
figure{margin:0 0 18px}
img,video{max-width:100%;height:auto;border-radius:12px;display:block;background:var(--panel)}
.prompt{white-space:pre-wrap;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13.5px;line-height:1.6}
h2{font-size:18px;margin:34px 0 10px}
h3{font-size:15px;margin:22px 0 8px;color:var(--muted);font-weight:600}
ul.grid{list-style:none;padding:0;display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(220px,1fr))}
ul.grid a{display:block;color:var(--ink)}
ul.grid .t{font-size:13.5px;line-height:1.5;margin:8px 0 4px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
ul.grid .a{font-size:12px;color:var(--muted);font-family:ui-monospace,monospace}
ul.grid img{aspect-ratio:4/3;object-fit:cover;width:100%}
.pager{display:flex;gap:14px;margin:30px 0 0;color:var(--muted);font-size:14px}
footer{margin-top:56px;color:var(--muted);font-size:13px;border-top:1px solid var(--line);padding-top:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin:0 0 14px}
.count{color:var(--brass);font-family:ui-monospace,monospace}
"""


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def clamp(text: object, limit: int) -> str:
    collapsed = re.sub(r"\s+", " ", "" if text is None else str(text)).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip(" ,;:，。；：") + "…"


def slugify(value: object, limit: int = 60) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return cleaned[:limit] or "item"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def month_of(created_at: str | None, collected_at: str | None) -> str:
    for candidate in (created_at, collected_at):
        if candidate and len(str(candidate)) >= 7:
            match = re.match(r"(\d{4})-(\d{2})", str(candidate))
            if match:
                return f"{match.group(1)}-{match.group(2)}"
    return "unknown"


def js_number(value: object) -> float | None:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def json_ld(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


class Dataset:
    """Everything the templates need, loaded in bounded batches."""

    def __init__(self, connection: sqlite3.Connection, dims: dict, chunk: int = 400):
        self.connection = connection
        self.dims = dims
        self.chunk = chunk
        self.tool_of: dict[str, str] = {}
        self.author_of: dict[str, str] = {}
        self.created_of: dict[str, str] = {}
        self.collected_of: dict[str, str] = {}
        self.by_tool: dict[str, list[str]] = {}
        self.indexable_tools: dict[str, int] = {}
        self.author_counts: dict[str, int] = {}
        self.indexable_authors: dict[str, int] = {}
        self.tag_counts: dict[tuple[str, str, str], int] = {}
        self.tag_labels: dict[tuple[str, str], tuple[str, str]] = {}
        self.reader = None

    # -- bulk indexes -----------------------------------------------------
    def load_indexes(self) -> None:
        for tweet_id, author, tool, created_at, collected_at in self.connection.execute(
            "SELECT tweet_id,author,tool,created_at,collected_at FROM prompts "
            "ORDER BY CAST(tweet_id AS INTEGER) DESC"
        ):
            key = str(tweet_id)
            tool_value = (tool or "Unknown").strip() or "Unknown"
            self.tool_of[key] = tool_value
            self.author_of[key] = str(author or "")
            self.created_of[key] = created_at or ""
            self.collected_of[key] = collected_at or ""
            self.by_tool.setdefault(tool_value, []).append(key)

        for tool, count in self.connection.execute(
            "SELECT COALESCE(NULLIF(TRIM(tool),''),'Unknown') AS tool, COUNT(*) FROM prompts GROUP BY 1"
        ):
            if count >= MIN_TOOL_COUNT:
                self.indexable_tools[str(tool)] = count

        for author, count in self.connection.execute(
            "SELECT author, COUNT(*) FROM prompts "
            "WHERE author IS NOT NULL AND author<>'' GROUP BY author"
        ):
            name = str(author)
            self.author_counts[name] = count
            if count >= MIN_AUTHOR_COUNT:
                self.indexable_authors[name] = count

        self._load_tag_indexes()

    def _load_tag_indexes(self) -> None:
        try:
            rows = self.connection.execute(
                """
                SELECT ld.key AS dimension, l.key AS tag, l.name AS label,
                       tl.display_zh AS label_zh, COUNT(DISTINCT pl.tweet_id) AS count
                FROM prompt_labels pl
                JOIN labels l ON l.id = pl.label_id
                JOIN label_dimensions ld ON ld.id = l.dimension_id
                LEFT JOIN taxonomy_labels tl
                       ON tl.label_id = l.id AND tl.taxonomy_version = ?
                WHERE pl.taxonomy_version = ?
                GROUP BY ld.key, l.key
                """,
                (TAXONOMY, TAXONOMY),
            ).fetchall()
        except sqlite3.Error:
            return
        for dimension, tag, label, label_zh, count in rows:
            key = (str(dimension), str(tag))
            self.tag_labels[key] = (str(label or tag), str(label_zh or label or tag))
            if count >= MIN_TAG_COUNT:
                self.tag_counts[(str(dimension), str(tag), str(label or tag))] = count

        self.tags_of: dict[str, list[tuple[str, str]]] = {}
        try:
            for tweet_id, dimension, tag in self.connection.execute(
                """
                SELECT pl.tweet_id, ld.key, l.key
                FROM prompt_labels pl
                JOIN labels l ON l.id = pl.label_id
                JOIN label_dimensions ld ON ld.id = l.dimension_id
                WHERE pl.taxonomy_version = ?
                """,
                (TAXONOMY,),
            ):
                self.tags_of.setdefault(str(tweet_id), []).append((str(dimension), str(tag)))
        except sqlite3.Error:
            self.tags_of = {}
        for key in self.tags_of:
            self.tags_of[key] = self.tags_of[key][:6]

    # -- per-batch fetch --------------------------------------------------
    def fetch_batch(self, tweet_ids: list[str]) -> dict[str, dict]:
        placeholders = ",".join("?" for _ in tweet_ids)
        records: dict[str, dict] = {}
        rows = self.connection.execute(
            f"""
            SELECT tweet_id, author, tool, prompt_text, created_at, tweet_url, collected_at
            FROM prompts WHERE tweet_id IN ({placeholders})
            """,
            tweet_ids,
        ).fetchall()
        for row in rows:
            tweet_id = str(row["tweet_id"])
            records[tweet_id] = {
                "tweet_id": tweet_id,
                "author": row["author"] or "",
                "tool": (row["tool"] or "Unknown").strip() or "Unknown",
                "prompt_text": row["prompt_text"] or "",
                "created_at": row["created_at"] or "",
                "tweet_url": row["tweet_url"] or "",
                "collected_at": row["collected_at"] or "",
                "images": [],
                "videos": [],
                "translation": "",
                "tags": self.tags_of.get(tweet_id, []),
            }

        for row in self.connection.execute(
            f"SELECT tweet_id, image_index, url, local_path FROM images "
            f"WHERE tweet_id IN ({placeholders}) ORDER BY image_index",
            tweet_ids,
        ):
            record = records.get(str(row["tweet_id"]))
            if record is not None:
                record["images"].append(
                    {
                        "index": row["image_index"],
                        "url": row["url"] or "",
                        "local": row["local_path"] or "",
                    }
                )

        try:
            for row in self.connection.execute(
                f"SELECT tweet_id, video_index, url, local_path, poster_path FROM videos "
                f"WHERE tweet_id IN ({placeholders}) ORDER BY video_index",
                tweet_ids,
            ):
                record = records.get(str(row["tweet_id"]))
                if record is not None:
                    record["videos"].append(
                        {
                            "index": row["video_index"],
                            "url": row["url"] or "",
                            "local": row["local_path"] or "",
                            "poster": row["poster_path"] or "",
                        }
                    )
        except sqlite3.Error:
            pass

        try:
            for row in self.connection.execute(
                f"SELECT tweet_id, translated_text FROM prompt_translations "
                f"WHERE locale='zh-Hans' AND translation_version=? AND tweet_id IN ({placeholders})",
                [TAXONOMY, *tweet_ids],
            ):
                record = records.get(str(row["tweet_id"]))
                if record is not None:
                    record["translation"] = row["translated_text"] or ""
        except sqlite3.Error:
            pass
        return records

    # -- derived ----------------------------------------------------------
    def ratio(self, record: dict) -> tuple[int, int] | None:
        entry = self.dims.get(record["tweet_id"])
        if not entry:
            return None
        pair = entry[0] if isinstance(entry[0], (list, tuple)) else entry
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            return None
        width, height = js_number(pair[0]), js_number(pair[1])
        if not width or not height:
            return None
        return int(width), int(height)

    def related(self, record: dict, limit: int = 6) -> list[str]:
        siblings = self.by_tool.get(record["tool"], [])
        if not siblings:
            return []
        try:
            position = siblings.index(record["tweet_id"])
        except ValueError:
            position = 0
        picks: list[str] = []
        step = 1
        while len(picks) < limit and step < len(siblings):
            for candidate in (position + step, position - step):
                if 0 <= candidate < len(siblings):
                    tweet_id = siblings[candidate]
                    if tweet_id != record["tweet_id"] and tweet_id not in picks:
                        picks.append(tweet_id)
                if len(picks) >= limit:
                    break
            step += 1
        return picks


def indexable(record: dict, min_chars: int = MIN_PROMPT_CHARS) -> bool:
    """Vetted enough to be indexed: real prompt, real media, Chinese reading."""
    if len(record["prompt_text"].strip()) < min_chars:
        return False
    if not record["images"] and not record["videos"]:
        return False
    if not record["translation"].strip():
        return False
    return True


def image_src(record: dict, image: dict) -> str:
    local = image.get("local") or ""
    if IMAGE_BASE and local:
        return f"{IMAGE_BASE}/{local.lstrip('/')}"
    return image.get("url") or (f"{SITE}/{local.lstrip('/')}" if local else "")


def video_src(video: dict) -> str:
    local = (video.get("local") or "").lstrip("/")
    return f"{VIDEO_BASE}/{local}" if local else (video.get("url") or "")


def video_poster(video: dict) -> str:
    local = (video.get("poster") or "").lstrip("/")
    return f"{VIDEO_BASE}/{local}" if local else ""


def heading_for(record: dict) -> str:
    source = record["translation"] or record["prompt_text"]
    return clamp(source.split("\n")[0], 68)


def layout(*, lang: str, title: str, description: str, canonical: str, body: str,
           structured: str, robots: str = "") -> str:
    robots_meta = f'<meta name="robots" content="{esc(robots)}">' if robots else ""
    return f"""<!doctype html>
<html lang="{esc(lang)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
{robots_meta}
<link rel="canonical" href="{esc(canonical)}">
<meta property="og:type" content="article">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{esc(canonical)}">
<style>{CSS}</style>
<script type="application/ld+json">{structured}</script>
</head>
<body>
<main>
{body}
<footer>
<p>Source prompts are reproduced byte-for-byte with attribution to the original creator.
Every record links back to its X post. <a href="/about/">How this archive works</a> ·
<a href="/takedown/">Creator opt-out / takedown</a></p>
<p>Open Image Prompts · open dataset, open source (<a href="https://github.com/NanmiCoder/open-image-prompts" rel="noopener">GitHub</a>)</p>
</footer>
</main>
</body>
</html>
"""


def detail_structured(record: dict, canonical: str, title: str) -> str:
    tweet_id = record["tweet_id"]
    images = []
    for image in record["images"]:
        src = image_src(record, image)
        if not src:
            continue
        node = {"@type": "ImageObject", "contentUrl": src, "url": src}
        if image.get("url"):
            node["sameAs"] = image["url"]
        images.append(node)
    videos = []
    for video in record["videos"]:
        src = video_src(video)
        if not src:
            continue
        node = {
            "@type": "VideoObject",
            "name": title,
            "description": clamp(record["translation"] or record["prompt_text"], 200),
            "contentUrl": src,
            "uploadDate": (record["created_at"] or "")[:19] or None,
        }
        poster = video_poster(video)
        if poster:
            node["thumbnailUrl"] = poster
        videos.append({key: value for key, value in node.items() if value})
    work = {
        "@type": "CreativeWork",
        "name": title,
        "text": record["prompt_text"],
        "inLanguage": "en",
        "headline": heading_for(record),
        "datePublished": (record["created_at"] or "")[:10] or None,
        "dateModified": (record["collected_at"] or "")[:10] or None,
        "author": {"@type": "Person", "name": record["author"], "url": record["tweet_url"]},
        "creator": {"@type": "Person", "name": record["author"]},
        "creditText": f"@{record['author']} on X",
        "isBasedOn": record["tweet_url"],
        "about": record["tool"],
        "keywords": ", ".join(f"{dimension}:{tag}" for dimension, tag in record["tags"]),
        "isAccessibleForFree": True,
        "mainEntityOfPage": canonical,
        "url": canonical,
    }
    if images:
        work["image"] = images if len(images) > 1 else images[0]
    if videos:
        work["video"] = videos if len(videos) > 1 else videos[0]
    graph = [
        {key: value for key, value in work.items() if value not in (None, "", [])},
        {
            "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "Prompts", "item": f"{SITE}/"},
                {
                    "@type": "ListItem",
                    "position": 2,
                    "name": record["tool"],
                    "item": f"{SITE}/tool/{slugify(record['tool'])}/",
                },
                {"@type": "ListItem", "position": 3, "name": title},
            ],
        },
    ]
    return json_ld({"@context": "https://schema.org", "@graph": graph})


def render_detail(record: dict, dataset: Dataset, index_ok: bool) -> str:
    tweet_id = record["tweet_id"]
    tool = record["tool"]
    author = record["author"]
    path = f"/p/{tweet_id}/"
    canonical = f"{SITE}{path}"
    zh = record["translation"]
    title_base = clamp(zh or record["prompt_text"], 46)
    title = f"{title_base}｜{tool} 提示词 — Open Image Prompts"
    description = clamp(
        f"{clamp(zh or record['prompt_text'], 120)}（{tool}，作者 @{author}，含原文与中文译文）",
        155,
    )

    media: list[str] = []
    for image in record["images"][:4]:
        src = image_src(record, image)
        if not src:
            continue
        ratio = dataset.ratio(record)
        size = f' width="{ratio[0]}" height="{ratio[1]}"' if ratio else ""
        loading = "eager" if not media else "lazy"
        priority = ' fetchpriority="high"' if not media else ""
        media.append(
            f'<figure><img src="{esc(src)}" alt="{esc(clamp(zh or record["prompt_text"], 90))}" '
            f'loading="{loading}"{priority}{size}></figure>'
        )
    for video in record["videos"][:2]:
        src = video_src(video)
        if not src:
            continue
        poster = video_poster(video)
        poster_attr = f' poster="{esc(poster)}"' if poster else ""
        media.append(
            f'<figure><video controls preload="metadata" playsinline{poster_attr} src="{esc(src)}"></video></figure>'
        )

    chips = "".join(
        f'<a class="chip" href="/tag/{esc(slugify(dimension))}/{esc(slugify(tag))}/">{esc(dimension)} · {esc(tag)}</a>'
        for dimension, tag in record["tags"]
    )

    related_items = []
    for related_id in dataset.related(record):
        related_record = dataset.fetch_batch([related_id]).get(related_id)
        if not related_record:
            continue
        related_items.append(
            f'<li><a href="/p/{esc(related_id)}/">'
            f'<span class="t">{esc(clamp(related_record["translation"] or related_record["prompt_text"], 80))}</span>'
            f'<span class="a">@{esc(related_record["author"])}</span></a></li>'
        )
    related_html = f"<h2>同工具的更多提示词</h2><ul class=\"grid\">{''.join(related_items)}</ul>" if related_items else ""

    prompt_original = esc(record["prompt_text"])
    body = f"""<nav class="crumbs"><a href="/">提示词库</a> › <a href="/tool/{esc(slugify(tool))}/">{esc(tool)}</a> › <a href="/u/{esc(slugify(author))}/">@{esc(author)}</a></nav>
<h1>{esc(heading_for(record))}</h1>
<p class="meta">{esc(tool)} · <a href="/u/{esc(slugify(author))}/">@{esc(author)}</a> · {esc((record["created_at"] or "")[:10])}
{(' · 视频提示词' if record['videos'] else '')}</p>
{''.join(media)}
<div class="chips">{chips}</div>
<h2>提示词（原文，逐字保留）</h2>
<div class="prompt" lang="en">{prompt_original}</div>
<h2>中文译文</h2>
<div class="prompt">{esc(zh or '（该条目暂无中文译文）')}</div>
<h2>来源与署名</h2>
<p>原文由 <strong>@{esc(author)}</strong> 发布在 X：<a href="{esc(record['tweet_url'])}" rel="nofollow noopener">查看原推文</a>。
本页逐字保留原文并提供机器翻译的中文解读；版权归原作者所有。</p>
<p><a href="/">在画廊中浏览</a></p>
{related_html}"""

    structured = detail_structured(record, canonical, title_base)
    return layout(
        lang="zh-Hans",
        title=title,
        description=description,
        canonical=canonical,
        body=body,
        structured=structured,
        robots="" if index_ok else "noindex, follow",
    )


def hub_cards(records: list[dict], dataset: Dataset) -> str:
    items = []
    for position, record in enumerate(records):
        image = record["images"][0] if record["images"] else None
        src = image_src(record, image) if image else ""
        if not src and record["videos"]:
            src = video_poster(record["videos"][0]) or ""
        ratio = dataset.ratio(record)
        size = f' width="{ratio[0]}" height="{ratio[1]}"' if ratio else ""
        # The first card is the likely LCP element: load it eagerly and high.
        loading = "eager" if position == 0 else "lazy"
        priority = ' fetchpriority="high"' if position == 0 else ""
        thumb = (
            f'<img src="{esc(src)}" alt="{esc(clamp(record["translation"] or record["prompt_text"], 80))}" '
            f'loading="{loading}"{priority}{size}>'
            if src
            else ""
        )
        items.append(
            f'<li><a href="/p/{esc(record["tweet_id"])}/">{thumb}'
            f'<span class="t">{esc(clamp(record["translation"] or record["prompt_text"], 84))}</span>'
            f'<span class="a">@{esc(record["author"])} · {esc(record["tool"])}</span></a></li>'
        )
    return f'<ul class="grid">{"".join(items)}</ul>'


def hub_page(*, heading: str, intro: str, path: str, records: list[dict], dataset: Dataset,
             total: int, page: int, index_ok: bool, extra_links: list[tuple[str, str]] | None = None) -> str:
    canonical = f"{SITE}{path}"
    title = f"{clamp(heading, 44)} — Open Image Prompts"
    description = clamp(intro, 155)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    pager = []
    if page > 1:
        previous = f"/{path.strip('/').rsplit('/page/', 1)[0]}/" if page == 2 else f"/{path.strip('/').rsplit('/page/', 1)[0]}/page/{page - 1}/"
        pager.append(f'<a href="{esc(previous)}">← 上一页</a>')
    if page < pages:
        pager.append(f'<a href="/{esc(path.strip("/"))}/page/{page + 1}/">下一页 →</a>')
    links_html = ""
    if extra_links:
        links_html = "<h2>相关入口</h2>" + "".join(
            f'<a class="chip" href="{esc(href)}">{esc(label)}</a>' for label, href in extra_links
        )
    structured = json_ld(
        {
            "@context": "https://schema.org",
            "@graph": [
                {
                    "@type": "CollectionPage",
                    "name": heading,
                    "description": description,
                    "url": canonical,
                    "isPartOf": {"@type": "WebSite", "name": "Open Image Prompts", "url": f"{SITE}/"},
                    "mainEntity": {
                        "@type": "ItemList",
                        "numberOfItems": total,
                        "itemListElement": [
                            {
                                "@type": "ListItem",
                                "position": index + 1 + (page - 1) * PAGE_SIZE,
                                "url": f"{SITE}/p/{record['tweet_id']}/",
                                "name": clamp(record["translation"] or record["prompt_text"], 80),
                            }
                            for index, record in enumerate(records)
                        ],
                    },
                },
                {
                    "@type": "BreadcrumbList",
                    "itemListElement": [
                        {"@type": "ListItem", "position": 1, "name": "Prompts", "item": f"{SITE}/"},
                        {"@type": "ListItem", "position": 2, "name": heading},
                    ],
                },
            ],
        }
    )
    body = f"""<nav class="crumbs"><a href="/">提示词库</a> › {esc(heading)}</nav>
<h1>{esc(heading)}</h1>
<p class="meta">{esc(intro)}</p>
{hub_cards(records, dataset)}
<div class="pager">{' '.join(pager)}</div>
{links_html}"""
    return layout(
        lang="zh-Hans",
        title=title,
        description=description,
        canonical=canonical,
        body=body,
        structured=structured,
        robots="" if index_ok else "noindex, follow",
    )


def trust_page(slug: str) -> str:
    heading, description = TRUST_PAGES[slug]
    canonical = f"{SITE}/{slug}/"
    if slug == "takedown":
        content = """<h2>如果你是创作者</h2>
<p>这个档案收录的是 X 上公开发布的提示词与作品。如果你的内容出现在这里而你不希望它被收录，告诉我们，我们会移除。</p>
<h2>怎么提交</h2>
<p>在 <a href="https://github.com/NanmiCoder/open-image-prompts/issues" rel="noopener">GitHub issue</a> 里提交，或发邮件到仓库主页列出的维护者邮箱，附上原推链接。我们会在 7 天内：</p>
<ul>
<li>从网站隐藏该条目（页面返回 410）；</li>
<li>从公开数据集与 sitemap 中移除；</li>
<li>在下一个数据集版本里删除对应图片/视频文件与数据库记录。</li>
</ul>
<p>不需要说明理由，也不需要证明身份。</p>"""
    else:
        content = """<h2>这是什么</h2>
<p>Open Image Prompts 是一个开放的、可验证的 AI 提示词档案：把 X 上创作者公开发布的提示词、生成结果与来源链接逐字归档，便于检索、比对与复用。</p>
<h2>数据从哪来</h2>
<p>条目来自公开帖子：原文逐字保留，中文译文作为独立字段提供，图片与视频尽量自托管以保证可访问。每条都标注作者并链接回原推。</p>
<h2>怎么保证质量</h2>
<ul>
<li>只有同时满足「有完整提示词原文、有对应图片或视频、有中文译文」的条目才会被搜索引擎收录；</li>
<li>标签来自固定的分类体系，不是猜测；</li>
<li>数据集与代码开源，任何人可以核对（<a href="https://github.com/NanmiCoder/open-image-prompts" rel="noopener">GitHub</a>）。</li>
</ul>
<h2>版权与移除</h2>
<p>提示词与作品的版权属于创作者。我们只做署名式的公开归档，并接受任何创作者的移除请求：<a href="/takedown/">创作者 opt-out / takedown</a>。</p>"""
    structured = json_ld(
        {
            "@context": "https://schema.org",
            "@type": "WebPage",
            "name": heading,
            "description": description,
            "url": canonical,
        }
    )
    body = f'<nav class="crumbs"><a href="/">提示词库</a> › {esc(heading)}</nav>\n<h1>{esc(heading)}</h1>\n{content}'
    return layout(
        lang="zh-Hans",
        title=f"{heading} — Open Image Prompts",
        description=description,
        canonical=canonical,
        body=body,
        structured=structured,
    )


class Writer:
    """Incremental, self-pruning writer with a content manifest."""

    def __init__(self, out: Path, dry_run: bool = False):
        self.out = out
        self.dry_run = dry_run
        self.manifest: dict[str, str] = {}
        self.written: set[str] = set()
        self.stats = {"written": 0, "unchanged": 0, "removed": 0, "bytes": 0}

    def load_manifest(self) -> None:
        path = self.out / MANIFEST_NAME
        try:
            self.manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.manifest = {}

    def add(self, relative: str, content: str) -> None:
        digest = sha256_text(content)
        self.written.add(relative)
        if self.manifest.get(relative) == digest:
            self.stats["unchanged"] += 1
            return
        self.manifest[relative] = digest
        self.stats["written"] += 1
        self.stats["bytes"] += len(content.encode("utf-8"))
        if self.dry_run:
            return
        target = self.out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def prune(self) -> None:
        for relative in sorted(set(self.manifest) - self.written):
            self.manifest.pop(relative, None)
            self.stats["removed"] += 1
            if self.dry_run:
                continue
            target = self.out / relative
            if target.is_file():
                target.unlink()
                parent = target.parent
                while parent != self.out and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent

    def save(self) -> None:
        if self.dry_run:
            return
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / MANIFEST_NAME).write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=0, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def sitemap_chunks(entries: list[tuple[str, str]]) -> list[str]:
    pieces: list[str] = []
    for start in range(0, len(entries), SITEMAP_CHUNK):
        chunk = entries[start : start + SITEMAP_CHUNK]
        body = "".join(
            f"<url><loc>{xml_escape(url)}</loc><lastmod>{xml_escape(lastmod)}</lastmod></url>"
            for url, lastmod in chunk
        )
        pieces.append(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + body + "</urlset>"
        )
    return pieces


def write_hub(writer: Writer, *, slug_path: str, heading: str, intro: str, tweet_ids: list[str],
              dataset: Dataset, index_ok: bool, extra_links=None) -> int:
    total = len(tweet_ids)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    indexed = 0
    for page in range(1, pages + 1):
        window = tweet_ids[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        records = []
        for start in range(0, len(window), dataset.chunk):
            batch = window[start : start + dataset.chunk]
            found = dataset.fetch_batch(batch)
            records.extend(found[tweet_id] for tweet_id in batch if tweet_id in found)
        if not records:
            continue
        path = f"/{slug_path}/" if page == 1 else f"/{slug_path}/page/{page}/"
        writer.add(
            path.strip("/") + "/index.html",
            hub_page(
                heading=heading,
                intro=intro,
                path=path,
                records=records,
                dataset=dataset,
                total=total,
                page=page,
                index_ok=index_ok and page <= 10,
                extra_links=extra_links if page == 1 else None,
            ),
        )
        if index_ok and page <= 10:
            indexed += 1
    return indexed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0, help="only the newest N prompts (0 = all)")
    parser.add_argument("--index-limit", type=int, default=INDEX_LIMIT, help="cap on indexable prompt pages")
    parser.add_argument("--min-chars", type=int, default=MIN_PROMPT_CHARS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-detail", action="store_true", help="hubs and sitemaps only")
    args = parser.parse_args()

    database = args.db or default_database()
    if database is None or not Path(database).is_file():
        print("seo-pages: no dataset database found; skipping page generation")
        return 0

    connection = sqlite3.connect(f"file:{database}?immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    dims = load_dims()
    dataset = Dataset(connection, dims)
    dataset.load_indexes()

    writer = Writer(args.out, dry_run=args.dry_run)
    writer.load_manifest()
    started = datetime.now(timezone.utc)

    order = list(dataset.tool_of.keys())
    if args.limit:
        order = order[: args.limit]

    sitemap_entries: list[tuple[str, str]] = []
    indexable_count = 0
    if not args.skip_detail:
        for start in range(0, len(order), dataset.chunk):
            batch = order[start : start + dataset.chunk]
            records = dataset.fetch_batch(batch)
            for tweet_id in batch:
                record = records.get(tweet_id)
                if not record:
                    continue
                eligible = indexable(record, args.min_chars)
                if eligible and indexable_count < args.index_limit:
                    index_ok = True
                    indexable_count += 1
                else:
                    index_ok = False
                writer.add(f"p/{tweet_id}/index.html", render_detail(record, dataset, index_ok))
                if index_ok:
                    sitemap_entries.append(
                        (f"{SITE}/p/{tweet_id}/", (record["collected_at"] or record["created_at"] or "")[:10])
                    )

    # -- hubs ------------------------------------------------------------
    hub_entries: list[tuple[str, str]] = []
    landings = sorted(
        ((count, tool) for tool, count in dataset.indexable_tools.items()), reverse=True
    )
    for count, tool in landings:
        slug_path = f"tool/{slugify(tool)}"
        pages = write_hub(
            writer,
            slug_path=slug_path,
            heading=f"{tool} 提示词合集",
            intro=f"{count} 条来自 X 创作者的 {tool} 提示词，含逐字原文、中文译文与生成结果。",
            tweet_ids=dataset.by_tool.get(tool, []),
            dataset=dataset,
            index_ok=True,
            extra_links=[
                (landing_tool, f"/tool/{slugify(landing_tool)}/")
                for _, landing_tool in landings[:8]
            ],
        )
        if pages:
            hub_entries.append((f"{SITE}/{slug_path}/", started.strftime("%Y-%m-%d")))

    author_landings = sorted(
        ((count, author) for author, count in dataset.indexable_authors.items()), reverse=True
    )
    for count, author in author_landings:
        slug_path = f"u/{slugify(author)}"
        pages = write_hub(
            writer,
            slug_path=slug_path,
            heading=f"@{author} 的提示词",
            intro=f"@{author} 在 X 上公开发布的 {count} 条提示词归档，含原文与中文译文。",
            tweet_ids=[tweet_id for tweet_id in order if dataset.author_of.get(tweet_id) == author],
            dataset=dataset,
            index_ok=count >= 20,
        )
        if pages and count >= 20:
            hub_entries.append((f"{SITE}/{slug_path}/", started.strftime("%Y-%m-%d")))

    for (dimension, tag, label), count in sorted(dataset.tag_counts.items(), key=lambda item: -item[1]):
        slug_path = f"tag/{slugify(dimension)}/{slugify(tag)}"
        pages = write_hub(
            writer,
            slug_path=slug_path,
            heading=f"{label}（{dimension}）提示词",
            intro=f"按分类体系标记为 {label} 的 {count} 条提示词，含原文与中文译文。",
            tweet_ids=[tweet_id for tweet_id in order if (dimension, tag) in dataset.tags_of.get(tweet_id, [])],
            dataset=dataset,
            index_ok=count >= 20,
        )
        if pages:
            hub_entries.append((f"{SITE}/{slug_path}/", started.strftime("%Y-%m-%d")))

    months: dict[str, list[str]] = {}
    for tweet_id in order:
        months.setdefault(
            month_of(dataset.created_of.get(tweet_id), dataset.collected_of.get(tweet_id)), []
        ).append(tweet_id)
    for month, tweet_ids in sorted(months.items(), reverse=True)[:12]:
        if month == "unknown":
            continue
        write_hub(
            writer,
            slug_path=f"archive/{month}",
            heading=f"{month} 的提示词归档",
            intro=f"{len(tweet_ids)} 条在该月发布或收录的提示词。",
            tweet_ids=tweet_ids,
            dataset=dataset,
            index_ok=False,
        )

    # -- trust pages -----------------------------------------------------
    for slug in TRUST_PAGES:
        writer.add(f"{slug}/index.html", trust_page(slug))

    # -- crawl entry points ----------------------------------------------
    entries = sitemap_entries + hub_entries
    chunks = sitemap_chunks(entries)
    for index, chunk in enumerate(chunks, start=1):
        writer.add(f"sitemap-{index}.xml", chunk)
    index_body = "".join(
        f"<sitemap><loc>{SITE}/sitemap-{index}.xml</loc>"
        f"<lastmod>{started.strftime('%Y-%m-%d')}</lastmod></sitemap>"
        for index in range(1, len(chunks) + 1)
    )
    writer.add(
        "sitemap.xml",
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + index_body
        + "</sitemapindex>",
    )
    writer.prune()
    writer.save()
    connection.close()

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print(
        "seo-pages: {written} written, {unchanged} unchanged, {removed} removed, "
        "{mb:.1f} MB, {count} indexable, {sitemaps} sitemap file(s), {elapsed:.1f}s".format(
            written=writer.stats["written"],
            unchanged=writer.stats["unchanged"],
            removed=writer.stats["removed"],
            mb=writer.stats["bytes"] / 1e6,
            count=indexable_count,
            sitemaps=len(chunks),
            elapsed=elapsed,
        )
    )
    return 0


def default_database() -> Path | None:
    candidates = [os.getenv("OIP_SEO_DB"), os.getenv("OIP_DB_PATH")]
    candidates.extend(
        [
            "/var/lib/open-image-prompts/prompts.db",
            str(REPO_ROOT / "db" / "prompts.db"),
            str(REPO_ROOT / ".oip" / "runtime" / "prompts.db"),
        ]
    )
    staged = sorted(Path("/var/lib/open-image-prompts").glob(".staging-*/prompts.db"))
    candidates = [str(path) for path in staged[-1:]] + [c for c in candidates if c]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def load_dims() -> dict:
    for candidate in (REPO_ROOT / "web" / "dims.json", REPO_ROOT / "dims.json"):
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


if __name__ == "__main__":
    sys.exit(main())
