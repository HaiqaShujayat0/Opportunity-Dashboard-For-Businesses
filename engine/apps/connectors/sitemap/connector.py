"""Public XML sitemap connector with recursive index and gzip support."""

import gzip
from datetime import datetime, time, timezone as datetime_timezone
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests
from django.utils.dateparse import parse_date, parse_datetime

from apps.connectors.base import BaseConnector


class SitemapError(RuntimeError):
    """A sitemap could not be fetched or parsed safely."""


class SitemapConnector(BaseConnector):
    source_name = "sitemap"
    user_agent = "OpportunityEngine/1.0 (+sitemap ingestion)"

    def __init__(self, run, market, *, session=None, timeout=30,
                 max_sitemaps=1000, max_urls=100000,
                 max_bytes=50 * 1024 * 1024):
        super().__init__(run, market)
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_sitemaps = max_sitemaps
        self.max_urls = max_urls
        self.max_bytes = max_bytes

    @staticmethod
    def _validate_url(url):
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SitemapError(f"Invalid sitemap URL: {url!r}")
        return url

    @staticmethod
    def _last_modified(value):
        if not value:
            return None
        parsed = parse_datetime(value)
        if parsed:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime_timezone.utc)
        parsed_day = parse_date(value)
        if parsed_day:
            return datetime.combine(parsed_day, time.min, tzinfo=datetime_timezone.utc)
        return None

    def _download(self, url):
        try:
            response = self.session.get(
                url, timeout=self.timeout,
                headers={"User-Agent": self.user_agent, "Accept": "application/xml,text/xml,*/*"},
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise SitemapError(f"Could not fetch sitemap {url}: {exc}") from exc
        content = response.content
        if len(content) > self.max_bytes:
            raise SitemapError(f"Sitemap {url} exceeds the size safety limit")
        if content[:2] == b"\x1f\x8b" or urlparse(url).path.lower().endswith(".gz"):
            try:
                content = gzip.decompress(content)
            except (OSError, EOFError) as exc:
                raise SitemapError(f"Invalid gzip sitemap {url}") from exc
            if len(content) > self.max_bytes:
                raise SitemapError(f"Expanded sitemap {url} exceeds the size safety limit")
        return content

    @staticmethod
    def _children(element, name):
        return [child for child in element if child.tag.rsplit("}", 1)[-1] == name]

    def _parse(self, content, url):
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as exc:
            raise SitemapError(f"Invalid XML in sitemap {url}: {exc}") from exc
        root_name = root.tag.rsplit("}", 1)[-1]
        if root_name not in {"urlset", "sitemapindex"}:
            raise SitemapError(f"Unsupported sitemap root <{root_name}> in {url}")
        item_name = "url" if root_name == "urlset" else "sitemap"
        records = []
        for item in self._children(root, item_name):
            values = {
                child.tag.rsplit("}", 1)[-1]: (child.text or "").strip()
                for child in item
            }
            location = values.get("loc", "")
            if location:
                self._validate_url(location)
                records.append({"url": location, "last_modified": self._last_modified(values.get("lastmod"))})
        return root_name, records

    def _fetch_document(self, url):
        params = {"url": url}
        cached = self._check_cache(url, params, ttl_hours=24)
        if cached:
            if cached.run_id != self.run.pk:
                self._log_fetch(url, params, cached.payload, cost_usd=0)
            return cached.payload
        try:
            document_type, records = self._parse(self._download(url), url)
            payload = {
                "document_type": document_type,
                "entries": [{
                    "url": record["url"],
                    "last_modified": record["last_modified"].isoformat() if record["last_modified"] else None,
                } for record in records],
            } 
            self._log_fetch(url, params, payload, cost_usd=0)
            return payload
        except Exception as exc:
            self._log_fetch(url, params, {"error": str(exc)}, cost_usd=0)
            raise

    def fetch(self, sitemap_url=None):
        root_url = self._validate_url(sitemap_url or self.market.sitemap_url)
        pending, visited, pages = [root_url], set(), {}
        while pending:
            sitemap = pending.pop(0)
            if sitemap in visited:
                continue
            visited.add(sitemap)
            if len(visited) > self.max_sitemaps:
                raise SitemapError("Sitemap index exceeds the document safety limit")
            payload = self._fetch_document(sitemap)
            entries = payload.get("entries")
            document_type = payload.get("document_type")
            if document_type not in {"urlset", "sitemapindex"} or not isinstance(entries, list):
                raise SitemapError(f"Malformed cached sitemap payload for {sitemap}")
            if document_type == "sitemapindex":
                pending.extend(entry["url"] for entry in entries)
                continue
            for entry in entries:
                page_url = self._validate_url(entry.get("url", ""))
                pages[page_url] = {"url": page_url, "last_modified": self._last_modified(entry.get("last_modified"))}
                if len(pages) > self.max_urls:
                    raise SitemapError("Sitemap exceeds the URL safety limit")
        return list(pages.values())
