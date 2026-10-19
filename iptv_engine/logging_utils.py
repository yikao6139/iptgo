"""结构化日志 + 单实例 HTTP 会话工厂。"""

from __future__ import annotations

import logging
import os
import random
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional

import requests
from requests.adapters import HTTPAdapter

_LOG_READY = False


def setup_logging(level: str = "INFO") -> None:
    global _LOG_READY
    if _LOG_READY:
        return
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # 屏蔽 urllib3 的重试噪音
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    _LOG_READY = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


class RateLimiter:
    """全局 QPS 限流，避免被目标站点/搜索引擎封禁。"""

    def __init__(self, qps: float):
        self.min_interval = 1.0 / max(qps, 0.1)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()


class SessionFactory:
    """带连接池复用 + 自适应重试 + 令牌桶限流的 requests 会话。"""

    def __init__(
        self,
        user_agent: str,
        pool_size: int = 64,
        retries: int = 2,
        qps: float = 20.0,
        proxy: Optional[str] = None,
        ca_bundle: Optional[str] = None,
        verify_ssl: bool = True,
        bypass_env_proxy: bool = False,
    ):
        self.user_agent = user_agent
        self.retries = retries
        self.limiter = RateLimiter(qps)
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        # bypass_env_proxy: 忽略 HTTP_PROXY / HTTPS_PROXY 环境变量。
        # 本机装了 Steam++/Watt Toolkit 之类工具时，它会设 http_proxy 指向
        # 本地加速代理，该代理对 GitHub 常返回 502/403。绕过它直连更稳。
        self.bypass_env_proxy = bypass_env_proxy
        self._local = threading.local()
        self._pool_size = pool_size
        self._sessions: Dict[int, requests.Session] = {}
        self._sessions_lock = threading.Lock()

        # ---- TLS 配置 ----
        # ca_bundle: 额外信任的 CA 证书文件（用于公司代理/中间人签发环境）
        #   典型场景：本机装了第三方 HTTPS 中间人（如 SteamTools / Fiddler /
        #   各类加速器），它给 GitHub 等站点签发自签证书，
        #   Python 不信任其根证书会报 CERTIFICATE_VERIFY_FAILED。
        # verify_ssl=False 只应在明确知晓风险时使用（流量可被解密）。
        self.verify: object = True
        if ca_bundle:
            path = Path(ca_bundle)
            if path.exists():
                self.verify = str(path)
            else:
                get_logger("tls").warning("CA 证书文件不存在: %s", ca_bundle)
        if not verify_ssl:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            self.verify = False

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.verify = self.verify
        if self.bypass_env_proxy:
            # 忽略系统/环境代理设置（Steam++、Watt Toolkit 等会注入 http_proxy）
            s.trust_env = False
        adapter = HTTPAdapter(
            pool_connections=self._pool_size,
            pool_maxsize=self._pool_size,
            max_retries=0,  # 重试逻辑自己控制，便于区分阶段
        )
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        s.headers.update(
            {
                "User-Agent": self.user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept": "*/*",
            }
        )
        if self.proxies:
            s.proxies = self.proxies
        return s

    def get(self, thread_id: Optional[int] = None) -> requests.Session:
        """每线程一个 Session，避免 requests Session 非线程安全问题。"""
        tid = thread_id if thread_id is not None else threading.get_ident()
        s = getattr(self._local, "session", None)
        if s is None:
            with self._sessions_lock:
                if tid not in self._sessions:
                    self._sessions[tid] = self._new_session()
                s = self._sessions[tid]
            self._local.session = s
        return s

    def jitter(self, base: float = 0.3) -> None:
        time.sleep(random.uniform(0, base))


def env_flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def enable_system_ca() -> bool:
    """
    让 requests/urllib3 使用操作系统证书库（Windows/macOS 的系统根证书）。

    为什么需要它：
      Python 的 ssl 模块能读系统证书库，但 requests 默认用 certifi 自带的
      CA bundle。在装有 HTTPS 中间人的机器上（加速器/抓包工具会往系统库
      注入根证书），会出现「socket 握手成功但 requests 报
      CERTIFICATE_VERIFY_FAILED」的怪现象 —— 此时 truststore 能对齐行为。

    返回是否注入成功。未安装时返回 False，调用方应回退到 certifi。
    """
    if os.environ.get("IPTV_NO_SYSTEM_CA"):
        return False
    try:
        import truststore
    except ImportError:
        return False
    try:
        truststore.inject_into_ssl()
        return True
    except Exception:  # noqa: BLE001
        return False


def build_session_factory(cfg: dict, *, pool_size: Optional[int] = None,
                          retries: Optional[int] = None,
                          qps: Optional[float] = None) -> SessionFactory:
    """
    按配置构造 SessionFactory，统一处理代理与 TLS 参数。

    避免各模块重复传参导致漏配 —— 尤其是 ca_bundle，
    漏一处就会出现「有的通路通、有的通路不通」的诡异现象。
    """
    cf = cfg.get("crawler", {}) or {}
    sf = SessionFactory(
        user_agent=cf.get("user_agent", "Mozilla/5.0"),
        pool_size=pool_size if pool_size is not None
        else int(cf.get("max_workers", 32)) * 2,
        retries=retries if retries is not None else int(cf.get("retries", 2)),
        qps=qps if qps is not None else float(cf.get("qps", 20)),
        proxy=cf.get("proxy") or None,
        ca_bundle=cf.get("ca_bundle") or None,
        verify_ssl=bool(cf.get("verify_ssl", True)),
        bypass_env_proxy=bool(cf.get("bypass_env_proxy", False)),
    )
    # 未显式指定 CA bundle 且要求校验时，尝试启用系统证书库
    if sf.verify is True and not os.environ.get("IPTV_NO_SYSTEM_CA"):
        enable_system_ca()
    return sf