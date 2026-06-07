"""Nyaa.si scraper.

Queries the public RSS feed (preferred, structured) and falls back to parsing
the HTML results table when the RSS endpoint is unavailable. Returns a list of
:class:`NyaaResult` objects exposing the magnet/torrent links the leech engine
consumes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

import aiohttp

_BASE = "https://nyaa.si"
_RSS_NS = {"nyaa": "https://nyaa.si/xmlns/nyaa"}
_USER_AGENT = "Mozilla/5.0 (compatible; SignSubBot/1.0; +https://nyaa.si)"


@dataclass(slots=True)
class NyaaResult:
    """A single Nyaa listing."""

    title: str
    magnet: Optional[str]
    torrent_url: Optional[str]
    size: str
    seeders: int
    leechers: int
    info_hash: Optional[str] = None

    @property
    def best_source(self) -> Optional[str]:
        """Prefer the magnet link, falling back to the .torrent URL."""

        return self.magnet or self.torrent_url


def _is_nyaa_url(text: str) -> bool:
    return "nyaa.si" in text.lower()


def _view_id_from_url(url: str) -> Optional[str]:
    match = re.search(r"/view/(\d+)", url)
    return match.group(1) if match else None


class NyaaScraper:
    """Async client for searching and resolving Nyaa.si entries."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._headers = {"User-Agent": _USER_AGENT}

    async def _fetch_text(self, url: str, params: Optional[dict[str, str]] = None) -> str:
        async with aiohttp.ClientSession(timeout=self._timeout, headers=self._headers) as session:
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                return await resp.text()

    async def search(self, query: str, *, limit: int = 10) -> list[NyaaResult]:
        """Search Nyaa for ``query`` and return up to ``limit`` results."""

        params = {"page": "rss", "q": query, "c": "0_0", "f": "0", "s": "seeders", "o": "desc"}
        try:
            xml = await self._fetch_text(f"{_BASE}/", params=params)
            results = self._parse_rss(xml)
            if results:
                return results[:limit]
        except (aiohttp.ClientError, ET.ParseError):
            pass
        # Fallback to HTML scraping.
        html = await self._fetch_text(f"{_BASE}/?q={quote_plus(query)}&s=seeders&o=desc")
        return self._parse_html(html)[:limit]

    async def resolve(self, url: str) -> Optional[NyaaResult]:
        """Resolve a single Nyaa ``/view/<id>`` URL to a downloadable result."""

        view_id = _view_id_from_url(url)
        if not view_id:
            return None
        html = await self._fetch_text(f"{_BASE}/view/{view_id}")
        title_match = re.search(r"<h3[^>]*class=\"panel-title\"[^>]*>(.*?)</h3>", html, re.S)
        title = (title_match.group(1).strip() if title_match else f"nyaa-{view_id}")
        magnet = self._first(re.findall(r'href="(magnet:\?[^"]+)"', html))
        torrent = self._first(re.findall(r'href="(/download/\d+\.torrent)"', html))
        info_hash = None
        if magnet:
            ih = re.search(r"btih:([0-9a-fA-F]+)", magnet)
            info_hash = ih.group(1).lower() if ih else None
        return NyaaResult(
            title=_strip_tags(title),
            magnet=magnet,
            torrent_url=f"{_BASE}{torrent}" if torrent else None,
            size="unknown",
            seeders=0,
            leechers=0,
            info_hash=info_hash,
        )

    # -- parsers ------------------------------------------------------------

    def _parse_rss(self, xml: str) -> list[NyaaResult]:
        root = ET.fromstring(xml)
        results: list[NyaaResult] = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()  # the .torrent URL
            seeders = int(_text(item, "nyaa:seeders") or 0)
            leechers = int(_text(item, "nyaa:leechers") or 0)
            size = _text(item, "nyaa:size") or "unknown"
            info_hash = _text(item, "nyaa:infoHash")
            magnet = self._magnet_from_hash(info_hash, title) if info_hash else None
            results.append(
                NyaaResult(
                    title=title,
                    magnet=magnet,
                    torrent_url=link or None,
                    size=size,
                    seeders=seeders,
                    leechers=leechers,
                    info_hash=info_hash,
                )
            )
        return results

    def _parse_html(self, html: str) -> list[NyaaResult]:
        results: list[NyaaResult] = []
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)
        for row in rows:
            title_match = re.search(r'/view/\d+"[^>]*title="([^"]+)"', row)
            if not title_match:
                title_match = re.search(r'href="/view/\d+"[^>]*>([^<]+)<', row)
            if not title_match:
                continue
            magnet = self._first(re.findall(r'href="(magnet:\?[^"]+)"', row))
            torrent = self._first(re.findall(r'href="(/download/\d+\.torrent)"', row))
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            size = _strip_tags(cells[3]).strip() if len(cells) > 3 else "unknown"
            seeders = _to_int(_strip_tags(cells[5])) if len(cells) > 5 else 0
            leechers = _to_int(_strip_tags(cells[6])) if len(cells) > 6 else 0
            results.append(
                NyaaResult(
                    title=_strip_tags(title_match.group(1)).strip(),
                    magnet=magnet,
                    torrent_url=f"{_BASE}{torrent}" if torrent else None,
                    size=size,
                    seeders=seeders,
                    leechers=leechers,
                )
            )
        return results

    @staticmethod
    def _magnet_from_hash(info_hash: Optional[str], title: str) -> Optional[str]:
        if not info_hash:
            return None
        trackers = [
            "udp://tracker.opentrackr.org:1337/announce",
            "udp://open.stealth.si:80/announce",
            "udp://tracker.openbittorrent.com:6969/announce",
            "udp://exodus.desync.com:6969/announce",
        ]
        tr = "".join(f"&tr={quote_plus(t)}" for t in trackers)
        return f"magnet:?xt=urn:btih:{info_hash}&dn={quote_plus(title)}{tr}"

    @staticmethod
    def _first(items: list[str]) -> Optional[str]:
        return items[0] if items else None


def _text(item: ET.Element, tag: str) -> Optional[str]:
    if ":" in tag:
        prefix, local = tag.split(":", 1)
        ns = _RSS_NS.get(prefix)
        if ns:
            el = item.find(f"{{{ns}}}{local}")
            return el.text.strip() if el is not None and el.text else None
    el = item.find(tag)
    return el.text.strip() if el is not None and el.text else None


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _to_int(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else 0


def is_nyaa_link(text: str) -> bool:
    return _is_nyaa_url(text) and "/view/" in text
