"""
GitHub 发现器：走 GitHub REST API 搜索仓库 → 遍历目录树 → 拉取播放列表文件。

这是发现链路的主引擎（冷启动之后的绝大部分新源都从这里来）。
不硬编码任何仓库地址或播放地址，仓库坐标来自：
  - config.seed_repositories（冷启动元列表）
  - GitHub Search API 的实时搜索结果
  - 其他模块发现的仓库（动态注册表）
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Dict, List, Optional, Set, Tuple

from ..config import get as cfg_get
from ..extractor import Candidate, extract
from ..logging_utils import get_logger
from .base import BaseDiscoverer, Fetcher

log = get_logger("discovery.github")

# 值得拉取的文本文件（只拉这些，避免把二进制拖下来）
INTERESTING_EXT = {
    ".m3u", ".m3u8", ".txt", ".json", ".xml", ".csv",
    ".html", ".htm", ".js", ".php", ".asp", ".aspx", ".md",
    ".yml", ".yaml", ".conf", ".ini",
}
# 一眼假的路径
SKIP_PATH_PARTS = (
    "node_modules/", ".git/", "vendor/", "dist/", "build/",
    ".github/workflows/", "package-lock", "yarn.lock", ".min.js",
    # 便携 ffmpeg 目录（体积大且无播放列表价值）
    "ffmpeg/", "ffprobe/", ".venv/", "venv/",
    # 本项目自身产物，抓回来只是自我复制
    "output/", "sources/registry.json",
)
# 明显不是播放列表的文件名
SKIP_NAMES = {
    "readme.md", "license", "license.md", "changelog.md", "contributing.md",
    "requirements.txt", "package.json", "dockerfile", "makefile",
    ".gitignore", ".gitattributes", "security.md", "code_of_conduct.md",
    "sitemap.xml", "robots.txt",
}

# m3u/m3u8/txt 大概率是播放列表，优先且加倍权重
LIST_LIKE = (".m3u", ".m3u8")


class GitHubDiscoverer(BaseDiscoverer):
    name = "github"

    def __init__(self, fetcher: Fetcher, cfg: dict):
        super().__init__(fetcher, cfg)
        self.api_base = cfg_get(cfg, "github.api_base", "https://api.github.com")
        self.token = cfg_get(cfg, "github.token", "") or ""
        self.api_limiter = max(1.0, 3600 / int(cfg_get(cfg, "github.rate_limit_per_hour", 4800)))
        self._last_api = 0.0
        self._repo_cache: Set[str] = set()
        self._api_calls = 0

    # ------------------------------------------------------------ API 基础
    def _headers(self) -> Dict[str, str]:
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _api(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        """调用 GitHub API，内置限流与 403/429 退避。"""
        self._last_api = max(self._last_api, time.monotonic())
        wait = self.api_limiter - (time.monotonic() - self._last_api)
        if wait > 0:
            time.sleep(wait)
        self._last_api = time.monotonic()
        self._api_calls += 1

        try:
            s = self.fetcher.sf.get()
            resp = s.get(
                f"{self.api_base}{path}",
                params=params or {},
                headers=self._headers(),
                timeout=(self.fetcher.connect_timeout, self.fetcher.read_timeout),
            )
        except Exception as exc:  # noqa: BLE001
            self.log.debug("API 请求失败 %s: %s", path, exc)
            return None

        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            reset = int(resp.headers.get("X-RateLimit-Reset", "0") or 0)
            sleep_for = max(5, min(reset - int(time.time()), 300)) if reset else 60
            self.log.warning("触发 GitHub 限流，休眠 %ds", sleep_for)
            time.sleep(sleep_for)
            return None
        if resp.status_code == 401:
            self.log.warning("GitHub token 无效或缺失，降级为匿名请求（限流较低）")
            self.token = ""
            return None
        if resp.status_code != 200:
            self.log.debug("API %s 返回 %s", path, resp.status_code)
            return None
        try:
            return resp.json()
        except json.JSONDecodeError:
            return None

    # ------------------------------------------------------------ 仓库搜索
    def search_repos(self, keyword: str, per_page: int = 40) -> List[str]:
        """按关键词搜索仓库，返回 owner/repo 坐标列表。"""
        data = self._api(
            "/search/repositories",
            {
                "q": f"{keyword} in:name,description,readme",
                "sort": "updated",
                "order": "desc",
                "per_page": min(per_page, 100),
            },
        )
        if not data or "items" not in data:
            return []
        return [
            f"{it['full_name']}"
            for it in data["items"]
            if it.get("full_name") and not it.get("archived")
        ]

    def discover(self) -> List[Candidate]:
        disc_cfg = cfg_get(self.cfg, "discovery", {}) or {}
        keywords: List[str] = disc_cfg.get("search_keywords", []) or []
        per_kw = int(disc_cfg.get("max_search_results_per_keyword", 40))

        # 1) 汇总待扫描仓库：种子 + 上次动态发现 + 本次搜索
        repos: List[str] = list(self.cfg.get("seed_repositories", []) or [])
        repos += self._load_persisted_repos()

        self.log.info("GitHub 搜索 %d 个关键词…", len(keywords))
        for kw in keywords:
            got = self.search_repos(kw, per_kw)
            self.log.info("  关键词「%s」→ %d 个仓库", kw, len(got))
            repos.extend(got)
            time.sleep(1.0)  # 尊重 Search API 限流

        # 2) 仓库坐标去重，过滤明显非 IPTV 仓库
        seen: Set[str] = set()
        repo_list: List[str] = []
        for r in repos:
            r = (r or "").strip().strip("/")
            if not r or r.count("/") != 1 or r.lower() in seen:
                continue
            if not self._looks_like_iptv_repo(r):
                continue
            seen.add(r.lower())
            repo_list.append(r)

        # 3) 限池：优先扫描更新时间靠前的（已在 search 里排过序）
        max_repos = int(cfg_get(self.cfg, "max_discovered_repos", 400))
        repo_list = repo_list[:max_repos]
        self.log.info("本轮共扫描 %d 个仓库", len(repo_list))

        # 4) 遍历每个仓库，找播放列表文件并抽取
        candidates: List[Candidate] = []
        for idx, repo in enumerate(repo_list, 1):
            try:
                got = self._scan_repo(repo)
                if got:
                    candidates.extend(got)
                    self.log.info("[%d/%d] %s → %d 条", idx, len(repo_list), repo, len(got))
            except Exception as exc:  # noqa: BLE001
                self.log.debug("扫描仓库失败 %s: %s", repo, exc)

        if cfg_get(self.cfg, "persist_discovered_repos", True):
            self._persist_repos(repos)

        self.log.info("GitHub 通路合计抽取 %d 条候选（API 调用 %d 次）",
                      len(candidates), self._api_calls)
        return candidates

    # ------------------------------------------------------------ 单仓库扫描
    def _looks_like_iptv_repo(self, repo: str) -> bool:
        """用仓库名做粗筛，减少 API 消耗。放行含关键词的，其余交给内容判定。"""
        low = repo.lower()
        keys = ("iptv", "tv", "m3u", "live", "playlist", "直播", "影视", "hls", "channel")
        return any(k in low for k in keys)

    def _scan_repo(self, repo: str) -> List[Candidate]:
        if repo.lower() in self._repo_cache:
            return []
        self._repo_cache.add(repo.lower())

        meta = self._api(f"/repos/{repo}")
        if not meta:
            return []
        branch = meta.get("default_branch", "main")

        tree = self._api(f"/repos/{repo}/git/trees/{branch}", {"recursive": "1"})
        if not tree or "tree" not in tree:
            return []
        if tree.get("truncated"):
            self.log.debug("%s 目录树过大被截断，只扫前部", repo)

        targets = self._pick_targets(tree.get("tree", []))
        if not targets:
            return []

        found: List[Candidate] = []
        for path in targets:
            cands = self._fetch_repo_file(repo, branch, path)
            found.extend(cands)
        return found

    def _pick_targets(self, tree_entries: List[dict]) -> List[str]:
        """从目录树中挑出值得抓的播放列表文件。"""
        scored: List[Tuple[int, str]] = []
        for entry in tree_entries:
            if entry.get("type") != "blob":
                continue
            path = entry.get("path", "")
            low = path.lower()
            if any(s in low for s in SKIP_PATH_PARTS):
                continue
            base = low.rsplit("/", 1)[-1]
            if base in SKIP_NAMES:
                continue
            size = entry.get("size", 0) or 0
            if size > 12 * 1024 * 1024 or size == 0:
                continue
            if not any(low.endswith(e) for e in INTERESTING_EXT):
                continue

            # 评分：m3u8/m3u 最高；文件名/路径含 iptv/live/tv 的次之；大文件降权
            score = 0
            if low.endswith(LIST_LIKE):
                score += 100
            if low.endswith((".m3u", ".m3u8", ".txt")) and score == 0:
                score += 40
            if any(k in low for k in ("iptv", "live", "tv", "playlist", "直播", "全球", "央视", "卫视")):
                score += 35
            if any(k in low for k in ("package", "readme", "changelog", "example", "sample", "demo", "test")):
                score -= 30
            if size > 3 * 1024 * 1024:
                score -= 15
            if score > 0:
                scored.append((score, path))

        scored.sort(key=lambda x: -x[0])
        # 单仓文件数上限：每个文件都要发一次请求，40 个 × N 个仓库容易超时。
        # 优先抓评分最高的少量文件即可覆盖主要播放列表。
        limit = int(self.cfg.get("github", {}).get("max_files_per_repo", 8) or 8)
        return [p for _, p in scored[:limit]]

    def _raw_url(self, repo: str, branch: str, path: str) -> str:
        """构造 raw.githubusercontent 直链。

        相比 Contents API，raw 直链不需要 API 配额、不限速，
        大批量抓文件时应优先用它。
        """
        return f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"

    def _fetch_repo_file(self, repo: str, branch: str, path: str) -> List[Candidate]:
        """拉取文件并抽取内容。

        优先走 raw 直链（不走 API）：不消耗 5000 次/小时的 API 配额，
        也不受速率限制影响。只有 raw 失败时才回退到 Contents API。
        """
        ref = f"https://github.com/{repo}/blob/{branch}/{path}"
        via = f"github:{repo}"

        # 1) raw 直链（快、无配额限制）
        res = self.fetcher.fetch(self._raw_url(repo, branch, path))
        if res.ok and res.content:
            cands = extract(res.content, found_via=via, source_ref=ref)
            if cands:
                self.log.debug("  %s → %d 条（raw）", path, len(cands))
            return cands

        # 2) 回退 Contents API（raw 可能是 LFS 或被防盗链拦截）
        data = self._api(f"/repos/{repo}/contents/{path}", {"ref": branch})
        if not data:
            return []
        content_b64 = data.get("content")
        if not content_b64:
            return []
        try:
            raw = base64.b64decode(content_b64)
        except Exception:  # noqa: BLE001
            return []
        text = self.fetcher._decode(raw, data.get("type", ""))
        ref = f"https://github.com/{repo}/blob/{branch}/{path}"
        via = f"github:{repo}"
        cands = extract(text, found_via=via, source_ref=ref)
        if cands:
            self.log.debug("  %s → %d 条", path, len(cands))
        return cands

    # ------------------------------------------------------------ 动态注册表
    def _repos_file(self):
        from pathlib import Path

        from ..config import ROOT_DIR

        return ROOT_DIR / "data" / "discovered_repos.json"

    def _load_persisted_repos(self) -> List[str]:
        p = self._repos_file()
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            repos = data.get("repos", []) if isinstance(data, dict) else data
            return [r for r in repos if isinstance(r, str)]
        except Exception:  # noqa: BLE001
            return []

    def _persist_repos(self, repos: List[str]) -> None:
        p = self._repos_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        old = set(r.lower() for r in self._load_persisted_repos())
        merged = sorted(old | {r.lower() for r in repos})
        max_keep = int(cfg_get(self.cfg, "max_discovered_repos", 400))
        payload = {"updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "count": len(merged),
                   "repos": merged[:max_keep]}
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log.info("已持久化 %d 个动态仓库坐标 → %s", len(payload["repos"]), p.name)