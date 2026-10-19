"""
发现调度器：并行驱动所有发现通路，汇总去重后交给验证层。

通路清单：
  1. github     GitHub REST 搜索 + 目录树遍历 + Contents 拉取（主力）
  2. meta       冷启动元列表递归爬取（含仓库坐标反哺）
  3. search     搜索引擎 HTML 结果页解析（覆盖 GitHub 之外）
  4. api        直接探测 API 型项目自带的 playlist 接口（可选，需要发现阶段先拿到）
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Set

from ..extractor import Candidate
from ..logging_utils import build_session_factory, get_logger
from ..normalizer import dedup_candidates, is_private_host
from .base import DiscoveryResult, Fetcher
from .github import GitHubDiscoverer
from .meta_crawler import MetaListDiscoverer
from .search_engine import SearchEngineDiscoverer

log = get_logger("discovery.dispatcher")


class DiscoveryDispatcher:
    """所有发现通路的总入口。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.sf = build_session_factory(cfg)
        self.fetcher = Fetcher(self.sf, cfg)

    def run(self) -> DiscoveryResult:
        started = time.monotonic()
        result = DiscoveryResult()

        # 通路开关：本地网络受限时可关闭不可用的通路（如 config.discovery_channels.github=false）
        ch = self.cfg.get("discovery_channels", {}) or {}
        meta_on = ch.get("meta", True)
        gh_on = ch.get("github", True)
        search_on = ch.get("search", True)
        active = [n for n, on in (("meta", meta_on), ("github", gh_on),
                                  ("search", search_on)) if on]
        log.info("启用的发现通路：%s", ", ".join(active) if active else "（无，全部禁用）")

        # ---- 元列表通路（须先跑）：产出新仓库坐标，反哺 GitHub 通路 ----
        if meta_on:
            meta = MetaListDiscoverer(self.fetcher, self.cfg)
            meta_cands = meta.discover()
            result.candidates.extend(meta_cands)
            result.stats["meta"] = len(meta_cands)
            result.repos.update(getattr(meta, "found_repos", set()))
        else:
            log.info("跳过元列表通路（discovery_channels.meta=false）")

        # ---- GitHub 通路：种子 + 元列表产出 + API 实时搜索 ----
        if gh_on:
            gh_cfg = dict(self.cfg)
            if result.repos:
                # 把元列表发现的仓库回灌为种子
                gh_cfg["seed_repositories"] = sorted(result.repos)
            gh = GitHubDiscoverer(self.fetcher, gh_cfg)
            gh_cands = gh.discover()
            result.candidates.extend(gh_cands)
            result.stats["github"] = len(gh_cands)
        else:
            log.info("跳过 GitHub 通路（discovery_channels.github=false）")

        # ---- 搜索引擎通路 ----
        if search_on:
            search = SearchEngineDiscoverer(self.fetcher, self.cfg)
            search_cands = search.discover()
            result.candidates.extend(search_cands)
            result.stats["search"] = len(search_cands)
        else:
            log.info("跳过搜索引擎通路（discovery_channels.search=false）")

        # ---- 统一过滤：内网地址 + 去重 ----
        raw_count = len(result.candidates)
        cleaned = [c for c in result.candidates if c.url and not is_private_host(c.url)]
        result.stats["private_filtered"] = raw_count - len(cleaned)

        deduped = dedup_candidates(cleaned)
        result.candidates = deduped
        result.stats["total_raw"] = raw_count
        result.stats["total_deduped"] = len(deduped)

        elapsed = time.monotonic() - started
        log.info(
            "发现完成：原始 %d → 去内网 %d → 去重后 %d 条候选（耗时 %.1fs）",
            raw_count, len(cleaned), len(deduped), elapsed,
        )
        for k in ("meta", "github", "search", "total_raw", "private_filtered", "total_deduped"):
            if k in result.stats:
                log.info("  %-18s = %s", k, result.stats[k])

        return result

    def close(self) -> None:
        self.fetcher.close()