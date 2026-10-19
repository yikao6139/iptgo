"""
元列表递归爬虫。

从 config.seed_repositories（冷启动元列表）出发，抓 README/导航页，
抽出其中的外链，判定类型后分派：
  - GitHub 仓库链接 -> 回灌给 GitHub 通路扫描
  - 疑似播放列表 / 直播源页面 -> 抓取并抽取
  - 其它外链（可能藏着源） -> 限深度抓取

这是"新源不断涌现"的关键：元列表每天都在被人更新，我们跟着爬就能持续拿到新源。
"""

from __future__ import annotations

import html
import re
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlsplit

from ..config import get as cfg_get
from ..extractor import extract
from ..logging_utils import get_logger
from .base import BaseDiscoverer

log = get_logger("discovery.meta")

HREF_RE = re.compile(r'href\s*=\s*["\']([^"\'#][^"\']*)["\']', re.IGNORECASE)

# 看起来像播放列表/直播源的页面
PLAYLIST_HINTS = (
    "m3u8", "m3u", "playlist", "直播源", "直播列表", "免费直播", "电视直播",
    "live.tv", "tv-live", "iptv", "台标", "频道", "直播地址", "源地址",
    "live-channels", "tvchannels",
)
# 明显不是源的页面
SKIP_HINTS = (
    "github.com", "gitee.com", "twitter", "facebook", "youtube",
    "telegram", "t.me", "paypal", "donate", "sponsor", "ad.",
    "google.", "baidu.com", "zhihu", "bilibili", "weibo", "qq.com",
    "163.com", "sohu.com", "taobao", "jd.com", "aliyun", "cloudflare.com",
    "mozilla.org", "w3.org", "creativecommons", "wikipedia", "license",
)


def _looks_like_playlist_url(u: str) -> bool:
    low = u.lower()
    if any(h in low for h in PLAYLIST_HINTS):
        return True
    return low.endswith((".m3u", ".m3u8"))


class MetaListDiscoverer(BaseDiscoverer):
    name = "meta"

    def discover(self) -> List[Candidate]:
        seeds: List[str] = list(self.cfg.get("seed_repositories", []) or [])
        if not seeds:
            self.log.warning("未配置 seed_repositories，冷启动通路关闭")
            return []

        depth = int(cfg_get(self.cfg, "discovery.meta_list_depth", 2))
        max_links = int(cfg_get(self.cfg, "discovery.max_links_per_meta_page", 60))

        self.log.info("从 %d 个种子元列表开始，递归深度 %d", len(seeds), depth)

        # BFS 队列：(url, depth, 溯源描述)
        queue: List[Tuple[str, int, str]] = []
        for repo in seeds:
            queue.append((f"https://github.com/{repo}", 0, f"seed:{repo}"))

        visited: Set[str] = set()
        candidates: List = []
        discovered_repos: Set[str] = set()
        page_budget = 60          # 页面总数硬上限，防止失控
        pages_done = 0

        while queue and pages_done < page_budget:
            url, dep, origin = queue.pop(0)
            key = url.split("#", 1)[0].rstrip("/")
            if key in visited:
                continue
            visited.add(key)
            pages_done += 1

            res = self.fetcher.fetch(url)
            if not res.ok or not res.content:
                continue

            # 页面自身若直接含流地址，立刻抽取
            direct = extract(res.content, found_via="meta", source_ref=url)
            if direct:
                candidates.extend(direct)
                self.log.info("  %s 直接含 %d 条源", _short(url), len(direct))

            if dep >= depth:
                continue

            links = self._extract_links(res.content, url, max_links)
            self.log.debug("  %s → %d 个外链 (depth=%d)", _short(url), len(links), dep + 1)

            for link in links:
                low = link.lower()
                if "github.com" in low:
                    repo = _github_repo_from_url(link)
                    if repo:
                        discovered_repos.add(repo)
                        if dep + 1 <= depth:
                            queue.append((f"https://github.com/{repo}", dep + 1, origin))
                    continue
                if any(h in low for h in SKIP_HINTS):
                    continue
                if _looks_like_playlist_url(link):
                    # 高价值目标，直接抓
                    queue.append((link, dep + 1, origin))

            # 对非 GitHub 的普通外链也做浅层跟进（很多源藏在二级页）
            if dep + 1 <= depth:
                for link in links[:max_links // 2]:
                    if "github.com" in link.lower() or any(h in link.lower() for h in SKIP_HINTS):
                        continue
                    if _looks_like_playlist_url(link):
                        continue
                    host = (urlsplit(link).hostname or "").lower()
                    # 只跟进看起来像内容站的域名
                    if host and not host.endswith((".js", ".css")):
                        queue.append((link, dep + 1, origin))

        self.log.info("元列表通路：访问 %d 个页面，抽取 %d 条候选，发现 %d 个新仓库",
                      pages_done, len(candidates), len(discovered_repos))
        self.found_repos = discovered_repos
        return candidates

    def _extract_links(self, content: str, base_url: str, limit: int) -> List[str]:
        """抽出页面所有外链，解析相对路径，过滤噪音。"""
        out: List[str] = []
        seen: Set[str] = set()
        for m in HREF_RE.finditer(content):
            href = html.unescape(m.group(1)).strip()
            if not href or href.startswith(("javascript:", "mailto:", "data:", "#")):
                continue
            try:
                absolute = urljoin(base_url, href)
            except ValueError:
                continue
            if not absolute.startswith(("http://", "https://")):
                continue
            absolute = absolute.split("#", 1)[0]
            if absolute in seen or len(absolute) > 400:
                continue
            seen.add(absolute)
            out.append(absolute)
            if len(out) >= limit:
                break
        return out

    @property
    def found_repos_list(self) -> List[str]:
        return sorted(getattr(self, "found_repos", set()))


_GH_REPO_RE = re.compile(r"github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+)")


def _github_repo_from_url(url: str) -> Optional[str]:
    m = _GH_REPO_RE.search(url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if owner.lower() in ("sponsors", "features", "topics", "orgs", "settings",
                          "marketplace", "pricing", "about", "login"):
        return None
    repo = re.sub(r"\.git$", "", repo)
    if repo.lower() in ("", "."):
        return None
    return f"{owner}/{repo}"


def _short(u: str, n: int = 60) -> str:
    return u if len(u) <= n else u[:n] + "…"