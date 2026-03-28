#!/usr/bin/env python3
"""AI Tips RSS Personal Aggregator

A small personal RSS/Atom aggregator that:
- pulls from multiple RSS/Atom feeds
- filters for AI tips / how-to / tutorials
- deduplicates items
- stores a small local cache in SQLite
- serves a personal RSS feed locally
- optionally generates a static XML file for GitHub Pages / local use

Dependencies:
  pip install feedparser requests

Quick start:
  1) python ai_tips_rss_personal.py init
  2) edit config.json if needed
  3) python ai_tips_rss_personal.py fetch
  4) python ai_tips_rss_personal.py serve --host 127.0.0.1 --port 8088

Then subscribe to:
  http://127.0.0.1:8088/feed.xml

Recommended sources to start with:
- OpenAI News page
- Hugging Face Blog
- arXiv RSS/Atom feeds (e.g. cs.AI, cs.LG, stat.ML)

The script is intentionally conservative: it keeps only items that look like
practical AI technique content rather than broad news.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import sqlite3
import sys
import textwrap
from collections import OrderedDict
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree.ElementTree import Element, SubElement, tostring

try:
    import feedparser  # type: ignore
except ImportError:
    feedparser = None

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
DB_PATH = APP_DIR / "cache.sqlite3"
### STATIC_FEED_PATH = APP_DIR / "feed.xml"
### 改為for github pages
STATIC_FEED_PATH = Path("feed.xml")

DEFAULT_CONFIG = {
    "site_title": "AI Tips Personal Feed",
    "site_link": "http://127.0.0.1:8088/feed.xml",
    "site_description": "A personal RSS feed focused on practical AI tips, tutorials, and techniques.",
    "feed_limit": 50,
    "refresh_minutes": 120,
    "sources": [
        {
            "name": "OpenAI News",
            "url": "https://openai.com/news/rss.xml",
            "kind": "rss",
        },
        {
            "name": "Hugging Face Blog",
            "url": "https://huggingface.co/blog/feed.xml",
            "kind": "rss",
        },
        {
            "name": "arXiv cs.AI",
            "url": "https://export.arxiv.org/rss/cs.AI",
            "kind": "rss",
        },
        {
            "name": "arXiv cs.LG",
            "url": "https://export.arxiv.org/rss/cs.LG",
            "kind": "rss",
        },
        {
            "name": "arXiv stat.ML",
            "url": "https://export.arxiv.org/rss/stat.ML",
            "kind": "rss",
        },
    ],
    "include_keywords": [
        "tutorial",
        "how to",
        "guide",
        "prompt",
        "prompting",
        "workflow",
        "agent",
        "rag",
        "fine-tuning",
        "finetuning",
        "embedding",
        "evaluation",
        "optimization",
        "inference",
        "deployment",
        "onnx",
        "edge ai",
        "llm",
        "ai",
    ],
    "exclude_keywords": [
        "job",
        "hiring",
        "funding",
        "acquisition",
        "press release",
        "company announcement",
        "conference recap",
    ],
}


@dataclasses.dataclass
class Item:
    title: str
    link: str
    description: str
    published: str
    source_name: str
    source_url: str
    guid: str


def ensure_app_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()


def load_config() -> dict:
    ensure_app_dir()
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    ensure_app_dir()
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def init_db() -> None:
    ensure_app_dir()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                guid TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                description TEXT NOT NULL,
                published TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.commit()


def http_get(url: str, timeout: int = 20) -> bytes:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AI-Tips-RSS/1.0)",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    }
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_feed(url: str) -> list[dict]:
    if feedparser is not None:
        parsed = feedparser.parse(url)
        entries = []
        for e in parsed.entries:
            entries.append(
                {
                    "title": getattr(e, "title", "") or "",
                    "link": getattr(e, "link", "") or "",
                    "summary": getattr(e, "summary", "") or getattr(e, "description", "") or "",
                    "published": getattr(e, "published", "") or getattr(e, "updated", "") or "",
                    "id": getattr(e, "id", "") or getattr(e, "guid", "") or "",
                }
            )
        return entries

    # Fallback: very small parser for simple RSS/Atom feeds
    raw = http_get(url)
    text = raw.decode("utf-8", errors="ignore")
    return simple_xml_parse(text)


def simple_xml_parse(xml_text: str) -> list[dict]:
    from xml.etree import ElementTree as ET

    root = ET.fromstring(xml_text)
    entries = []
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "content": "http://purl.org/rss/1.0/modules/content/",
    }

    if root.tag.endswith("rss"):
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            summary = (item.findtext("description") or item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or "").strip()
            published = (item.findtext("pubDate") or "").strip()
            gid = (item.findtext("guid") or link or title).strip()
            entries.append({"title": title, "link": link, "summary": summary, "published": published, "id": gid})
    else:
        for entry in root.findall(".//atom:entry", ns):
            title = (entry.findtext("atom:title", namespaces=ns) or "").strip()
            link_el = entry.find("atom:link", ns)
            link = link_el.attrib.get("href", "").strip() if link_el is not None else ""
            summary = (entry.findtext("atom:summary", namespaces=ns) or entry.findtext("atom:content", namespaces=ns) or "").strip()
            published = (entry.findtext("atom:published", namespaces=ns) or entry.findtext("atom:updated", namespaces=ns) or "").strip()
            gid = (entry.findtext("atom:id", namespaces=ns) or link or title).strip()
            entries.append({"title": title, "link": link, "summary": summary, "published": published, "id": gid})
    return entries


def item_matches(item: dict, include_keywords: list[str], exclude_keywords: list[str]) -> bool:
    text = normalize_text(" ".join([item.get("title", ""), item.get("summary", "")]))
    if not text:
        return False
    if any(k in text for k in exclude_keywords):
        return False
    if any(k in text for k in include_keywords):
        return True
    # Keep some practical technique content even without exact keywords.
    technique_hints = ["code", "model", "pipeline", "benchmark", "evaluation", "dataset", "llm", "agent", "rag", "deployment"]
    return any(k in text for k in technique_hints)


def make_guid(source_name: str, source_url: str, item: dict) -> str:
    base = item.get("id") or item.get("link") or item.get("title") or ""
    return sha1("|".join([source_name, source_url, base]))


def to_isoish(value: str) -> str:
    if not value:
        return utc_now().isoformat()
    value = value.strip()
    try:
        return parsedate_to_datetime(value).isoformat()
    except Exception:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except Exception:
        pass
    return value


def fetch_and_store() -> list[Item]:
    cfg = load_config()
    init_db()
    include_keywords = [normalize_text(k) for k in cfg.get("include_keywords", [])]
    exclude_keywords = [normalize_text(k) for k in cfg.get("exclude_keywords", [])]
    collected: OrderedDict[str, Item] = OrderedDict()

    for src in cfg.get("sources", []):
        source_name = src.get("name", "Source")
        source_url = src.get("url", "")
        if not source_url:
            continue
        try:
            entries = parse_feed(source_url)
        except Exception as exc:
            print(f"[warn] failed to fetch {source_name}: {exc}", file=sys.stderr)
            continue

        for raw in entries:
            if not item_matches(raw, include_keywords, exclude_keywords):
                continue
            title = (raw.get("title") or "").strip()
            link = (raw.get("link") or "").strip()
            description = (raw.get("summary") or "").strip()
            published = to_isoish((raw.get("published") or "").strip())
            guid = make_guid(source_name, source_url, raw)
            item = Item(
                title=title,
                link=link,
                description=description,
                published=published,
                source_name=source_name,
                source_url=source_url,
                guid=guid,
            )
            collected.setdefault(guid, item)

    items = list(collected.values())
    limit = int(cfg.get("feed_limit", 50))

    with sqlite3.connect(DB_PATH) as conn:
        for it in items:
            conn.execute(
                "INSERT OR REPLACE INTO items (guid, title, link, description, published, source_name, source_url, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (it.guid, it.title, it.link, it.description, it.published, it.source_name, it.source_url, utc_now().isoformat()),
            )
        # 清除超過 limit 的舊資料，以防資料庫無限膨脹
        conn.execute(f"DELETE FROM items WHERE guid NOT IN (SELECT guid FROM items ORDER BY published DESC LIMIT {limit})")
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("last_fetch", utc_now().isoformat()))
        conn.commit()

    return items


def read_items(limit: Optional[int] = None) -> list[Item]:
    init_db()
    cfg = load_config()
    if limit is None:
        limit = int(cfg.get("feed_limit", 50))
    with sqlite3.connect(DB_PATH) as conn:
        # 注意: 這裡的 SELECT 順序必須與 Item dataclass 宣告屬性的順序一致
        rows = conn.execute(
            "SELECT title, link, description, published, source_name, source_url, guid FROM items ORDER BY published DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [Item(*row) for row in rows]


def build_rss_xml(items: Iterable[Item], cfg: dict) -> bytes:
    rss = Element("rss", version="2.0")
    channel = SubElement(rss, "channel")

    SubElement(channel, "title").text = cfg.get("site_title", DEFAULT_CONFIG["site_title"])
    SubElement(channel, "link").text = cfg.get("site_link", DEFAULT_CONFIG["site_link"])
    SubElement(channel, "description").text = cfg.get("site_description", DEFAULT_CONFIG["site_description"])
    SubElement(channel, "language").text = "zh-tw"
    SubElement(channel, "lastBuildDate").text = format_datetime(utc_now())

    for it in items:
        item = SubElement(channel, "item")
        SubElement(item, "title").text = it.title
        SubElement(item, "link").text = it.link
        SubElement(item, "guid").text = it.guid
        SubElement(item, "description").text = f"[{it.source_name}] {it.description}"
        SubElement(item, "pubDate").text = it.published if it.published else format_datetime(utc_now())

    xml = tostring(rss, encoding="utf-8", xml_declaration=True)
    return xml


def save_static_feed() -> Path:
    cfg = load_config()
    items = read_items(limit=int(cfg.get("feed_limit", 50)))
    xml = build_rss_xml(items, cfg)
    ensure_app_dir()
    STATIC_FEED_PATH.write_bytes(xml)
    return STATIC_FEED_PATH


def serve(host: str, port: int) -> None:
    cfg = load_config()
    init_db()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                items = read_items(limit=int(cfg.get("feed_limit", 50)))
                html = [
                    "<html><head><meta charset='utf-8'><title>AI Tips Personal Feed</title></head><body>",
                    f"<h1>{cfg.get('site_title', 'AI Tips Personal Feed')}</h1>",
                    f"<p>{cfg.get('site_description', '')}</p>",
                    "<p><a href='/feed.xml'>RSS feed</a></p>",
                    "<ul>",
                ]
                for it in items:
                    html.append(
                        f"<li><a href='{it.link}' target='_blank' rel='noopener noreferrer'>{escape_html(it.title)}</a> "
                        f"<small>({escape_html(it.source_name)})</small><br><span>{escape_html(it.description[:240])}</span></li>"
                    )
                html.extend(["</ul>", "</body></html>"])
                self._send(200, "\n".join(html).encode("utf-8"), "text/html; charset=utf-8")
                return

            if path == "/feed.xml":
                xml_path = save_static_feed()
                self._send(200, xml_path.read_bytes(), "application/rss+xml; charset=utf-8")
                return

            if path == "/health":
                self._send(200, b"ok", "text/plain; charset=utf-8")
                return

            self._send(404, b"not found", "text/plain; charset=utf-8")

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

    httpd = HTTPServer((host, port), Handler)
    print(f"Serving on http://{host}:{port}")
    print(f"Feed URL: http://{host}:{port}/feed.xml")
    httpd.serve_forever()


def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def cmd_init(_: argparse.Namespace) -> None:
    init_db()
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
    print(f"Initialized in {APP_DIR}")
    print(f"Config: {CONFIG_PATH}")
    print(f"Database: {DB_PATH}")


def cmd_fetch(_: argparse.Namespace) -> None:
    items = fetch_and_store()
    save_static_feed()
    print(f"Fetched {len(items)} items")
    print(f"Saved feed to {STATIC_FEED_PATH}")


def cmd_serve(args: argparse.Namespace) -> None:
    serve(args.host, args.port)


def cmd_show_config(_: argparse.Namespace) -> None:
    cfg = load_config()
    print(json.dumps(cfg, indent=2, ensure_ascii=False))


def cmd_edit_config(_: argparse.Namespace) -> None:
    cfg = load_config()
    print(f"Edit this file: {CONFIG_PATH}")
    print(json.dumps(cfg, indent=2, ensure_ascii=False))


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Personal AI tips RSS aggregator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            Examples:
              python ai_tips_rss_personal.py init
              python ai_tips_rss_personal.py fetch
              python ai_tips_rss_personal.py serve --host 127.0.0.1 --port 8088
            """
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create config and storage files")
    sub.add_parser("fetch", help="fetch sources and generate the RSS feed")
    sub.add_parser("show-config", help="print current config")
    sub.add_parser("edit-config", help="print config path for manual editing")

    s = sub.add_parser("serve", help="serve the RSS feed locally")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8088)

    return p


def main(argv: list[str]) -> int:
    parser = build_arg_parser()
    if not argv:
        parser.print_help()
        return 1
    args = parser.parse_args(argv)

    if args.cmd == "init":
        cmd_init(args)
    elif args.cmd == "fetch":
        cmd_fetch(args)
    elif args.cmd == "serve":
        cmd_serve(args)
    elif args.cmd == "show-config":
        cmd_show_config(args)
    elif args.cmd == "edit-config":
        cmd_edit_config(args)
    else:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
