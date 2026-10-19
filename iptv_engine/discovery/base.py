"""发现模块公共基类与调度器。"""

from __future__ import annotations

import abc
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set

from ..extractor import Candidate, extract
from ..logging_utils import SessionFactory, get_logger

log = get_logger("discovery.base")


@dataclass
class FetchResult:
    """一次网络抓取的结果。"""
    url: str
    ok: bool
    content: str = ""
    status: int = 0
    content_type: str = ""
    error: str = ""


ROBOTS_EXEMPT_HOSTS = (
    # 搜索引擎的结果页是本引擎的合法入口：robots.txt 明确 Disallow /search，
    # 但我们要抓的就是它。这些站点是公开检索接口，抓取频率低、不影响其业务。
    "duckduckgo.com",
    "bing.com",
    "www.bing.com",
    "sogou.com",
    "www.sogou.com",
    # raw.githubusercontent.com 不提供 robots.txt（返回 404），
    # 但它是 GitHub 官方的文件分发域名，拉取公开仓库内容属于其既定用途。
    "raw.githubusercontent.com",
)


class Fetcher:
    """
    通用抓取器：带并发控制、超时、截断、重试。

    所有 discovery 子模块共用一个 Fetcher，避免重复建连接。
    """

    def __init__(self, session_factory: SessionFactory, cfg: dict):
        cf = cfg.get("crawler", {}) or {}
        self.sf = session_factory
        self.connect_timeout = cf.get("connect_timeout", 8)
        self.read_timeout = cf.get("read_timeout", 15)
        self.max_bytes = cf.get("max_response_bytes", 8 * 1024 * 1024)
        self.retries = cf.get("retries", 2)
        self.pool = ThreadPoolExecutor(
            max_workers=cf.get("max_workers", 32),
            thread_name_prefix="fetch",
        )
        self._robots_cache: Dict[str, Optional[set]] = {}
        self._robots_lock = __import__("threading").Lock()
        self.respect_robots = cf.get("respect_robots", True)

    # ------------------------------------------------------------ robots
    def _robots_allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        from urllib.parse import urlsplit

        try:
            p = urlsplit(url)
        except ValueError:
            return False
        host = (p.hostname or "").lower()
        # 搜索引擎入口豁免（见 ROBOTS_EXEMPT_HOSTS 说明）
        if host in ROBOTS_EXEMPT_HOSTS:
            return True
        origin = f"{p.scheme}://{p.netloc}"
        with self._robots_lock:
            if origin in self._robots_cache:
                disallow = self._robots_cache[origin]
            else:
                disallow = None
                try:
                    s = self.sf.get()
                    r = s.get(
                        f"{origin}/robots.txt",
                        timeout=(self.connect_timeout, 5),
                    )
                    if r.status_code == 200 and len(r.text) < 512 * 1024:
                        disallow = set()
                        agent = (self.sf.user_agent or "*").split("/")[0]
                        applies = False
                        for line in r.text.splitlines():
                            line = line.split("#", 1)[0].strip()
                            if not line or ":" not in line:
                                continue
                            k, v = line.split(":", 1)
                            k, v = k.strip().lower(), v.strip()
                            if k == "user-agent":
                                applies = v in ("*", agent.lower())
                            elif k == "disallow" and applies and v:
                                disallow.add(v)
                    else:
                        disallow = set()   # 无 robots 视为允许
                except Exception:
                    disallow = set()
                self._robots_cache[origin] = disallow

        if not disallow:
            return True
        path = p.path or "/"
        for rule in disallow:
            if rule == "/":
                return False
            if path.startswith(rule):
                return False
        return True

    # ------------------------------------------------------------ 抓取
    def fetch(self, url: str, headers: Optional[dict] = None) -> FetchResult:
        """抓取单个 URL，返回文本内容（超限截断）。"""
        if not url or not url.lower().startswith(("http://", "https://")):
            return FetchResult(url=url, ok=False, error="非 http(s) 地址")
        if not self._robots_allowed(url):
            return FetchResult(url=url, ok=False, error="robots.txt 禁止")

        last_err = ""
        for attempt in range(self.retries + 1):
            try:
                self.sf.limiter.wait()
                s = self.sf.get()
                resp = s.get(
                    url,
                    headers=headers or {},
                    timeout=(self.connect_timeout, self.read_timeout),
                    allow_redirects=True,
                    stream=True,
                )
                ctype = resp.headers.get("Content-Type", "")
                # 流式读取并截断
                chunks, total = [], 0
                for chunk in resp.iter_content(65536):
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= self.max_bytes:
                        break
                raw = b"".join(chunks)[: self.max_bytes]
                resp.close()

                if resp.status_code >= 400:
                    last_err = f"HTTP {resp.status_code}"
                    if resp.status_code in (429, 503):
                        self.sf.jitter(2.0)
                        continue
                    return FetchResult(url=url, ok=False, status=resp.status_code,
                                       content_type=ctype, error=last_err)

                # 编码探测：m3u8/txt 用 utf-8 优先，回退 gbk/latin-1
                text = self._decode(raw, ctype)
                return FetchResult(url=url, ok=True, content=text,
                                   status=resp.status_code, content_type=ctype)
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    self.sf.jitter(1.0)
                    continue
        return FetchResult(url=url, ok=False, error=last_err)

    @staticmethod
    def _decode(raw: bytes, ctype: str) -> str:
        encodings = ["utf-8"]
        m = ctype.lower()
        if "gb" in m or "gbk" in m or "gb2312" in m:
            encodings = ["gb18030", "utf-8"]
        elif "utf" in m:
            encodings = ["utf-8"]
        encodings += ["gb18030", "latin-1"]
        for enc in encodings:
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="ignore")

    # ------------------------------------------------------------ 并发
    def map(self, items: Iterable, fn: Callable, desc: str = "") -> List:
        """并发 map，保持输入顺序。"""
        items = list(items)
        if not items:
            return []
        results: List[Optional] = [None] * len(items)
        done = 0
        with ThreadPoolExecutor(max_workers=min(len(items), 64),
                                thread_name_prefix="map") as ex:
            fut2idx = {ex.submit(fn, it): i for i, it in enumerate(items)}
            for fut in as_completed(fut2idx):
                i = fut2idx[fut]
                try:
                    results[i] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    log.debug("map 任务异常 [%s]: %s", desc, exc)
                    results[i] = None
                done += 1
                if done % 100 == 0:
                    log.info("  %s 进度 %d/%d", desc, done, len(items))
        return results

    def close(self) -> None:
        self.pool.shutdown(wait=False)


# ---------------------------------------------------------------- 抽象发现器

class BaseDiscoverer(abc.ABC):
    """所有发现通路的统一接口。"""

    name = "base"

    def __init__(self, fetcher: Fetcher, cfg: dict):
        self.fetcher = fetcher
        self.cfg = cfg
        self.log = get_logger(f"discovery.{self.name}")

    @abc.abstractmethod
    def discover(self) -> List[Candidate]:
        """执行发现，返回候选流列表。"""

    def crawl_and_extract(self, urls: Iterable[str]) -> List[Candidate]:
        """并发抓取一批页面并抽取内容。"""
        urls = [u for u in urls if u]
        if not urls:
            return []

        def worker(u: str) -> List[Candidate]:
            res = self.fetcher.fetch(u)
            if not res.ok or not res.content:
                return []
            return extract(res.content, found_via=self.name, source_ref=u)

        log.info("[%s] 抓取 %d 个页面…", self.name, len(urls))
        chunks = self.fetcher.map(urls, worker, desc=self.name)
        out: List[Candidate] = []
        for c in chunks:
            if c:
                out.extend(c)
        self.log.info("[%s] 抽取到 %d 条候选", self.name, len(out))
        return out


@dataclass
class DiscoveryResult:
    """一次完整发现的汇总。"""
    candidates: List[Candidate] = field(default_factory=list)
    repos: Set[str] = field(default_factory=set)       # 发现到的仓库坐标
    stats: Dict[str, int] = field(default_factory=dict)