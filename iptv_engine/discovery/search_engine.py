"""
搜索引擎发现器：解析 DuckDuckGo / Bing / 搜狗 的 HTML 结果页。

不依赖任何 API Key，纯 HTML 解析。注意：这些引擎的 HTML 结构会变，
所以解析策略采取"宽松 + 多重回退"，并在全部失败时静默降级（不抛异常）。
"""

from __future__ import annotations

import html
import re
from typing import List, Optional, Set
from urllib.parse import parse_qs, quote_plus, unquote, urlsplit

from ..config import get as cfg_get
from ..logging_utils import get_logger
from .base import BaseDiscoverer

log = get_logger("discovery.search")

# 站内过滤：这些站点扫全文性价比低/无直播源
BLOCKED_HOST_SUFFIX = (
    "youtube.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "pinterest.com", "tiktok.com", "amazon.", "ebay.", "aliexpress",
    "baike.baidu.com", "zhihu.com/question", "taobao.com", "jd.com",
    "bilibili.com/video", "tmall.com",
)
# 结果页去重后的站点扩展名
STREAMY_EXT = (".m3u8", ".m3u", ".mpd", ".ts", ".flv")


def _unwrap_ddg(url: str) -> Optional[str]:
    """DuckDuckGo 用 //duckduckgo.com/l/?uddg=<encoded> 包裹真实 URL。"""
    if "duckduckgo.com/l/" in url or url.startswith("//duckduckgo.com/l/"):
        qs = parse_qs(urlsplit("https:" + url if url.startswith("//") else url).query)
        for key in ("uddg", "u3"):
            if key in qs and qs[key]:
                return unquote(qs[key][0])
    return None


class SearchEngineDiscoverer(BaseDiscoverer):
    name = "search"

    def discover(self) -> List[Candidate]:
        disc = cfg_get(self.cfg, "discovery", {}) or {}
        engines = [e for e in (disc.get("search_engines") or []) if e.get("enabled")]
        keywords = disc.get("search_keywords") or []
        pages = int(disc.get("search_pages", 2))
        # 搜索侧的关键词偏向"找页面"，与 GitHub 侧略有差异，全部由配置驱动
        web_keywords = disc.get("web_keywords") or [
            "免费直播源 m3u8 列表",
            "免费电视直播 m3u8 在线播放源",
            "全球直播源 m3u 分享",
            "央视直播源 m3u8 直连",
        ]

        urls: List[str] = []
        for eng in engines:
            for kw in web_keywords:
                urls.extend(self._search_one(eng, kw, pages))
        # 顺带把 GitHub 侧的关键词也送进搜索引擎，扩大覆盖面
        for kw in keywords[:4]:
            for eng in engines:
                urls.extend(self._search_one(eng, kw, 1))

        urls = self._filter_result_urls(urls)
        self.log.info("搜索通路共收集 %d 个候选落地页", len(urls))
        return self.crawl_and_extract(urls)

    # ------------------------------------------------------------ 单引擎
    def _search_one(self, engine: dict, keyword: str, pages: int) -> List[str]:
        name = engine.get("name", "?")
        endpoint = engine.get("endpoint", "")
        if not endpoint:
            return []
        found: List[str] = []
        for page in range(pages):
            query = self._build_query(name, keyword, page)
            if not query:
                continue
            url = endpoint + ("&" if "?" in endpoint else "?") + query
            res = self.fetcher.fetch(url)
            if not res.ok or not res.content:
                self.log.debug("[%s] 搜索失败 keyword=%s page=%d: %s",
                               name, keyword, page, res.error)
                continue
            links = self._parse_results(name, res.content)
            self.log.info("  [%s] 「%s」p%d → %d 条结果", name, keyword[:24], page + 1, len(links))
            found.extend(links)
        return found

    @staticmethod
    def _build_query(name: str, keyword: str, page: int) -> str:
        if name == "duckduckgo":
            q = f"q={quote_plus(keyword)}"
            return q if page == 0 else f"{q}&s={page * 30}"
        if name == "bing":
            q = f"q={quote_plus(keyword)}"
            return q if page == 0 else f"{q}&first={page * 10 + 1}"
        if name == "sogou":
            q = f"query={quote_plus(keyword)}"
            return q if page == 0 else f"{q}&page={page + 1}"
        return ""

    def _parse_results(self, engine: str, html_text: str) -> List[str]:
        """多策略解析引擎结果页，任一策略命中即返回。"""
        out: List[str] = []
        t = html_text

        if engine == "duckduckgo":
            out += [html.unescape(m) for m in
                    re.findall(r'class="result__a"[^>]*href="([^"]+)"', t)]
            out += [html.unescape(m) for m in
                    re.findall(r'<a[^>]+class="[^"]*result-link[^"]*"[^>]+href="([^"]+)"', t)]
        elif engine == "bing":
            out += [html.unescape(m) for m in
                    re.findall(r'<h2><a href="([^"]+)"', t)]
            out += [html.unescape(m) for m in
                    re.findall(r'<a[^>]+class="[^"]*tilk[^"]*"[^>]+href="([^"]+)"', t)]
        elif engine == "sogou":
            out += [html.unescape(m) for m in
                    re.findall(r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"', t)]
        # 通用回退：所有 href
        if not out:
            out += [html.unescape(m) for m in re.findall(r'href="(https?://[^"]+)"', t)]

        cleaned: List[str] = []
        for u in out:
            real = _unwrap_ddg(u) or u
            real = real.strip()
            if real.startswith("http") and len(real) < 500:
                cleaned.append(real)
        return cleaned

    # ------------------------------------------------------------ 结果过滤
    def _filter_result_urls(self, urls: List[str]) -> List[str]:
        """过滤搜索引擎自身的域名、被封站点、明显的社交页。"""
        seen: Set[str] = set()
        out: List[str] = []
        engine_hosts = ("duckduckgo.com", "bing.com", "sogou.com", "microsoft.com",
                        "msn.com", "go.microsoft.com", "w3.org")
        for u in urls:
            host = (urlsplit(u).hostname or "").lower()
            if not host:
                continue
            if any(h in host for h in engine_hosts):
                continue
            if any(b in u.lower() for b in BLOCKED_HOST_SUFFIX):
                continue
            key = u.split("#", 1)[0]
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out