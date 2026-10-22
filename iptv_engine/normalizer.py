"""
归一化 + 双重去重引擎。

【双重去重】含义：
  1. URL 归一化 —— 剥离追踪参数、统一大小写/端口/路径、去尾斜杠，
     解决同一个流被不同站点以不同 query 顺序转载导致的重复。
  2. 频道名归一化 —— 去掉空格/全角/大小写/装饰符号/频道号后缀，
     解决 "CCTV-1 综合" / "CCTV1综合" / "CCTV 1 综合" 这类别名重复。

最终 dedup_key = sha1(url_norm + '|' + name_norm)
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 需要剥离的追踪/无意义参数（不影响播放）
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "ref", "referrer", "referer", "share", "share_token",
    "from_spmid", "timestamp", "_t", "t", "r", "random", "v", "version",
    "cache_bust", "nocache", "wsSecret", "wsTime", "sign", "signature",
    "token_expire", "_", "rand",
}

# 保留的参数（对播放至关重要）
KEEP_PARAMS = {
    "token", "auth", "key", "session", "hdnts", "hdnea", "hdntl", "expires",
    "policy", "kid", "hdn", "st", "e", "us", "h", "channel", "src",
}

DEFAULT_PORTS = {"http": 80, "https": 443, "rtsp": 554, "rtmp": 1935, "rtmps": 443}


@dataclass
class DedupEntry:
    url_norm: str
    name_norm: str
    dedup_key: str
    payload: dict = field(default_factory=dict)


# ---------------------------------------------------------------- URL 归一化

def normalize_url(url: str) -> str:
    """
    URL 归一化。同一逻辑流的等价写法必须收敛到同一字符串。

    处理：
      - 协议/主机小写，去默认端口
      - 路径去掉重复斜杠与结尾斜杠
      - 剥离追踪参数，对保留参数按 key 排序
      - 去掉 fragment
    """
    if not url:
        return ""
    url = url.strip().strip("\"'")
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.lower()

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https", "rtsp", "rtmp", "rtmps"):
        return url.lower()

    host = (parts.hostname or "").lower()
    if not host:
        return url.lower()

    # IPv6 去括号
    port = parts.port
    netloc = host
    if port and port != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"

    # 路径：合并 //，去结尾 /，保留开头 /
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    # 查询参数：保留白名单 + 非追踪参数，排序保证顺序无关
    kept: List[Tuple[str, str]] = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        lk = k.lower()
        if lk in TRACKING_PARAMS and lk not in KEEP_PARAMS:
            continue
        kept.append((k, v))
    # 值本身是 token 的（如 ?hdnts=exp=xxx~acl=/*），不排序避免破坏
    if all(re.match(r"^[A-Za-z0-9_\-]+$", k) for k, _ in kept):
        kept.sort(key=lambda kv: kv[0])
    query = urlencode(kept, doseq=False)

    return urlunsplit((scheme, netloc, path, query, ""))


# -------------------------------------------------------------- 频道名归一化

_CJK_PUNCT = "　-〿＀-￯·—…、。〈〉《》「」『』【】〔〕"


def normalize_name(name: str) -> str:
    """
    频道名归一化：抹平排版差异，让同一个频道的不同写法收敛。

    CCTV-1 综合 / 央视1综合(高清) / 【CCTV1】综合  ->  cctv1综合
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", name)          # 全角 -> 半角
    s = s.translate({ord(c): None for c in _CJK_PUNCT})
    s = s.lower()
    # 去掉画质/徽标类噪声后缀
    s = re.sub(r"\b(hd|sd|fhd|uhd|4k|蓝光|超清|高清|标清|直播|备用|线路|无广告)\b", "", s)
    s = re.sub(r"[（(\[【].*?[）)\]】]", "", s)                    # 去括号内容
    s = re.sub(r"[-_.·、,，:：;；'\"^~|/\\\s]+", "", s)
    s = re.sub(r"\d{2,}期?$", "", s)                              # 去末尾期号
    return s.strip()


def name_core(name: str) -> str:
    """更激进的频道核心名：抹掉 'cctv/fj/卫视' 等前缀，用于跨源识别同一频道。"""
    s = normalize_name(name)
    s = re.sub(r"^(cctv|央视|fj|广东|湖南|浙江|江苏|山东|辽宁|黑龙江|吉林|河南|河北|四川|重庆|安徽|福建|深圳|云南|贵州|陕西|甘肃|新疆|内蒙古|广西|海南|宁夏|青海|西藏)", "", s)
    return s


# ---------------------------------------------------------------- 去重键

def dedup_key(url: str, name: str) -> str:
    u = normalize_url(url)
    n = normalize_name(name)
    raw = f"{u}|{n}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 分组归一化

# 标准分组名 → 匹配关键词（顺序敏感：先匹配到的先返回）
# 只收「中国频道 + 港澳台」，国际频道归到「国际」。
# 刻意不做激进映射（如把 "Movie" 归到「影视」）——
# 上游分组体系各不相同，过度归一会丢失信息。
GROUP_ALIASES: List[Tuple[str, Tuple[str, ...]]] = [
    ("央视", ("央视频道", "cctv", "cctv频道", "央视台", "中央电视台")),
    ("卫视", ("卫视台", "省级卫视", "卫星电视")),
    ("港澳台", ("港澳", "台湾", "香港", "澳门", "港台", "港澳台")),
    ("地方", ("地方台", "省市", "省内", "本地")),
    ("体育", ("体育频道", "sports")),
    ("少儿", ("少儿频道", "卡通", "动画", "儿童")),
    ("新闻", ("新闻频道", "news")),
    ("影视", ("影视", "电影", "剧场", "movies")),
]


def normalize_group(group: str) -> str:
    """
    把上游五花八门的分组名归一到标准分组。

    为什么需要：不同 m3u 源对同一类频道各有命名 ——
    「央视」「央视频道」「01·央视频道」「CCTV」其实是同一类，
    不归一就会在播放列表里裂成多个分组，且难以按优先级排序。

    匹配规则：
      1. 去掉编号前缀（如「01·」「02-」「1.」）与空白
      2. 逐条比对别名表：别名出现在输入里 或 输入出现在别名里都算命中
         （前者覆盖「01·央视频道」，后者覆盖「央视」本身）
      3. 都未命中则原样返回
    """
    if not group:
        return "其他"
    g = (group or "").strip()
    # 去掉编号前缀：「01·央视频道」「02-卫视」「1. 央视」
    g = re.sub(r"^\s*\d{1,2}\s*[·\-_.、]\s*", "", g)
    g = re.sub(r"\s+", "", g)
    if not g:
        return "其他"

    low = g.lower()
    for std, aliases in GROUP_ALIASES:
        if low == std or std in low:
            return std
        for a in aliases:
            if a in low or low in a:
                return std
    return g


def is_private_host(url: str) -> bool:
    """过滤内网/保留地址，避免把 SSRF 面暴露出去，也避免无意义源。"""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return True
    if not host:
        return True
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        # 域名：拦截明显内网用名
        low = host.lower()
        if low.endswith((".local", ".internal", ".lan", ".home.arpa")):
            return True
        return False


# ---------------------------------------------------------------- 集合级去重

class Deduper:
    """流式去重器：URL 维度 + 频道维度双键集合。"""

    def __init__(self) -> None:
        self._keys: set = set()
        self._urls: set = set()
        self._names: Dict[str, str] = {}   # name_norm -> dedup_key（保留首个）
        self.stats = {"input": 0, "dup_url": 0, "dup_name": 0, "kept": 0}

    def seen(self, url: str, name: str) -> bool:
        k = dedup_key(url, name)
        u = normalize_url(url)
        n = normalize_name(name)
        if k in self._keys or u in self._urls:
            self.stats["dup_url"] += 1
            return True
        if n and n in self._names:
            self.stats["dup_name"] += 1
            return True
        self._keys.add(k)
        self._urls.add(u)
        if n:
            self._names[n] = k
        return False

    def add(self, items: Iterable) -> List:
        """批量过滤，返回去重后的对象列表（保持首次出现顺序）。"""
        out = []
        for it in items:
            self.stats["input"] += 1
            if self.seen(it.url, it.name):
                continue
            out.append(it)
        self.stats["kept"] = len(out)
        return out


def dedup_candidates(candidates: Iterable) -> List:
    """便捷函数：一次性去重。"""
    return Deduper().add(candidates)
