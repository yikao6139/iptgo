"""
抽取引擎：从任意噪声文本（HTML / JS / M3U / JSON / README）中提取候选播放地址。

这是发现链路的"矿工"。它不关心来源，只负责把字节流里的
`https://xxx/live/yyy.m3u8` 连同周边上下文（频道名、tvg-logo、group-title）一起挖出来。

两种模式：
1. parse_m3u()      —— 严格模式，解析标准 M3U/M3U8，字段完整
2. scrape_text()    —— 宽松模式，正则从任意文本里捞地址 + 启发式猜频道名
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

# ------------------------------------------------------------------ 数据结构

@dataclass
class Candidate:
    """一条候选流。found_via 记录发现渠道，用于后续归因与权重调整。"""

    url: str
    name: str = ""
    logo: str = ""
    group: str = ""
    tvg_id: str = ""
    attributes: Dict[str, str] = field(default_factory=dict)
    found_via: str = ""          # discovery channel
    source_ref: str = ""         # 溯源：来自哪个 URL / 仓库
    referer: str = ""            # 部分源校验需要 Referer
    user_agent: str = ""         # 部分源校验需要 UA

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "name": self.name,
            "logo": self.logo,
            "group": self.group,
            "tvg_id": self.tvg_id,
            "attributes": self.attributes,
            "found_via": self.found_via,
            "source_ref": self.source_ref,
            "referer": self.referer,
            "user_agent": self.user_agent,
        }

    @staticmethod
    def from_dict(d: dict) -> "Candidate":
        return Candidate(
            url=d.get("url", ""),
            name=d.get("name", ""),
            logo=d.get("logo", ""),
            group=d.get("group", ""),
            tvg_id=d.get("tvg_id", ""),
            attributes=d.get("attributes", {}) or {},
            found_via=d.get("found_via", ""),
            source_ref=d.get("source_ref", ""),
            referer=d.get("referer", ""),
            user_agent=d.get("user_agent", ""),
        )


# ------------------------------------------------------------------ 正则定义

# 播放地址主正则：支持 http/https/rtmp/rtmps/rtsp + 常见流容器后缀。
# 注意：这里刻意不加前置负向断言 —— href="..." 场景下断言极易因紧邻字符
# （= " : 等）导致整体漏匹配。改为"宽松匹配 + 匹配后清洗校验"。
STREAM_URL_RE = re.compile(
    r"""(
        (?:https?|rtmps?|rtsp)://
        [^\s"'<>\x00-\x1f\\]{4,600}?
        \.(?:m3u8|m3u|mpd|flv|ts|aac|mp4)(?:\?[^\s"'<>\x00-\x1f\\]*)?
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# 兜底：无扩展名但形态像流的地址。
# 必须严格限定 —— 之前用过宽的 /play|/stream 后缀，把 linkedin.com/company/playhop、
# 植物大战僵尸视频页这类普通网页全当成直播流了。
# 现在只接受「流路径 + 路径末尾带分片/清单常见形态」或「.ts 后缀」的组合。
STREAM_HINT_RE = re.compile(
    r"""(
        (?:https?|rtmps?)://
        [^\s"'<>\x00-\x1f\\]{4,300}?
        (?:
            # 形如 /live/xxx.m3u8、/hls/xxx/playlist.m3u8、/live/xxx/720p/index.m3u8
            /(?:live|hls|stream|media|video|playlist|tv|channel)(?:/[^\s"'<>\x00-\x1f\\]*)?
            \.(?:m3u8|m3u|mpd|ts|flv)(?:\?[^\s"'<>\x00-\x1f\\]*)?
            |
            # 形如 /live/xxx/playlist、/hls/xxx/master（无扩展名但强流特征）
            /(?:live|hls|stream|playlist)(?:/[^\s"'<>\x00-\x1f\\]*)*/(?:master|playlist|index|manifest|chunklist)(?:\.[a-z0-9]{1,5})?(?:\?[^\s"'<>\x00-\x1f\\]*)?
        )
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# 明显不是直播源的路径特征
# 否定词表：只放「明显不是直播流」的资源类型与占位域名。
#
# 注意：这里**刻意不含**内网地址（127.0.0.1 / localhost / 192.168.* 等）。
# 内网过滤由 normalizer.is_private_host() 在注册与验证环节统一负责 ——
# 抽取层只判断"像不像流"，不该掺入 SSRF 防护职责。
# 否则会误伤局域网 IPTV 盒子这类真实场景（很多人在用）。
NEGATIVE_PATH_HINTS = (
    "/schema.org",
    "/w3.org",
    "/creativecommons.org",
    "/fonts/",
    "/css/",
    "/js/",
    ".js",
    ".css",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".woff",
    ".ttf",
    ".map",
    "example.com",
    "your-domain",
    "yourdomain",
    "xxx.com",
    "domain.com",
    "test.m3u8",
    "sample.m3u8",
)

# 直播流"容器"后缀白名单：只有这些才算直播流。
# 刻意排除 .mp4/.aac —— 技术博客里的 Big Buck Bunny / Sintel 之类的
# 演示影片不是直播源，混进播放列表会让用户看到一堆死频道。
LIVE_EXTENSIONS = (".m3u8", ".m3u", ".mpd", ".flv", ".ts")

# 已知测试样例/演示流主机：能播但对用户毫无价值
DEMO_HOSTS = (
    "test-streams.mux.dev",
    "bitdash-a.akamaihd.net",
    "bitdash-b.akamaihd.net",
    "demo.unified-streaming.com",
    "devimages.apple.com",
    "media.w3.org",
    "commondatastorage.googleapis.com",
    "storage.googleapis.com/shaka-demo",
    "dash.akamaized.net",
    "streamlock.net",
    "wowzaec2demo",
    "sintel",
    "bigbuckbunny",
    "big_buck_bunny",
    "elephantsdream",
    "tears-of-steel",
)

# M3U 属性解析
EXTINF_RE = re.compile(r"^#EXTINF:(?P<dur>-?[\d.]+)\s*(?P<attrs>.*?),(?P<name>.*)$")
ATTR_RE = re.compile(r'(?P<k>[\w-]+)\s*=\s*"(?P<v>[^"]*)"')


def _looks_like_stream(url: str) -> bool:
    """
    判断 URL 是否像一条直播流。

    规则：
      1. 否定词表（js/css/图片/示例域名…）直接否决
      2. 带扩展名的：必须是直播流容器后缀白名单（.mp4/.aac 不算直播）
      3. 无扩展名的：靠 /live/ /hls/ 等路径特征判断
      4. rtsp/rtmp 一律放行（这类协议基本只用于流）
    """
    u = url.lower()
    if any(hint in u for hint in NEGATIVE_PATH_HINTS):
        return False
    if any(demo in u for demo in DEMO_HOSTS):
        return False
    if not re.match(r"^(https?|rtmps?|rtsp)://", u, re.I):
        return False

    scheme = u.split("://", 1)[0]
    if scheme in ("rtsp", "rtmp", "rtmps"):
        return True

    # 取 pathname（去掉 query），看是否带已知扩展名
    path = u.split("?", 1)[0].split("#", 1)[0]
    last_seg = path.rsplit("/", 1)[-1]
    ext = last_seg.rsplit(".", 1)[-1] if "." in last_seg else ""

    if ext and len(ext) <= 5:
        # 有明确扩展名：必须是直播流容器。
        # 注意不能用 ext.isalpha() 过滤 —— "m3u8"/"mpd" 这类扩展名
        # 本身就含数字，用 isalpha() 会把标准直播扩展名全部误杀。
        return f".{ext}" in LIVE_EXTENSIONS

    # 无扩展名：仅在具备强流特征时放行（如 /live/xxx/master）
    # 否则一律拒绝 —— 普通网页 URL（/company/playhop）不该被当直播流。
    return bool(re.search(
        r"/(?:live|hls|stream|playlist)(?:/[^/?#]*)*"
        r"/(?:master|playlist|index|manifest|chunklist)(?:\.[a-z0-9]{1,5})?$",
        path, re.I))


def _unescape_url(url: str) -> str:
    """
    还原 JS/JSON 字符串里的转义斜杠：https:\\/\\/host\\/a.m3u8 -> https://host/a.m3u8
    这是抓取 JS 页面时的常态，不处理会漏掉大量源。
    """
    if "\\/" in url:
        url = url.replace("\\/", "/")
    # 处理被整体转义的十六进制形式 \u002F
    if "\\u002f" in url.lower():
        url = re.sub(r"(?i)\\u002f", "/", url)
    return url


def unwrap_redirect(url: str) -> str:
    """
    还原跳转站包装的真实地址。

    国内技术站大量使用 ?target=<urlencoded> 跳转（掘金 link.zhihu.com、
    百度、CSDN 等）。不还原的话入库的全是跳转链，播放器会直接失败。

    例：https://link.juejin.cn?target=http%3A%2F%2Fhost%2Fa.m3u8
      -> http://host/a.m3u8
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    qs = parse_qs(parts.query)
    # 常见参数名
    for key in ("target", "url", "u", "link", "redirect", "to", "jump"):
        vals = qs.get(key)
        if not vals:
            continue
        cand = unquote(vals[0].strip())
        if cand.lower().startswith(("http://", "https://", "rtmp://", "rtsps://", "rtsp://")):
            return cand
    return url


def _attrs_to_dict(attrs: str) -> Dict[str, str]:
    return {m.group("k").lower(): html.unescape(m.group("v")) for m in ATTR_RE.finditer(attrs or "")}


def guess_name_from_url(url: str) -> str:
    """从 URL 路径反推一个可读频道名（兜底用，质量不高）。"""
    try:
        from urllib.parse import urlparse

        p = urlparse(url)
        segs = [s for s in p.path.split("/") if s]
        # 优先取倒数第二段（频道名常在路径倒数第二层）
        for s in reversed(segs[:-1] if len(segs) > 1 else segs):
            s = s.strip()
            if s and not s.lower().endswith((".m3u8", ".m3u", ".mpd", ".ts", ".flv")) and len(s) > 1:
                s = re.sub(r"[_\-.]+", " ", s)
                s = re.sub(r"\b\d{2,}\b", "", s).strip()
                if s and len(s) <= 40 and re.search(r"[A-Za-z\u4e00-\u9fff]", s):
                    return s
        host = p.netloc.split(".")[0]
        return host or "unknown"
    except Exception:
        return "unknown"


def guess_group_from_name(name: str) -> str:
    """按频道名做粗粒度分组，供播放器目录展示。"""
    n = name or ""
    rules = (
        ("央视", ("cctv", "央视", "央视网")),
        ("卫视", ("卫视", "卫视台")),
        ("地方", ("地方", "台", "都市")),
        ("港澳台", ("香港", "澳门", "台湾", "tvb", "翡翠", "明珠", "华视", "中视")),
        ("国际", ("国际", "world", "cnn", "bbc", "nhk")),
        ("体育", ("体育", "sport", "cctv5", "espn", "football")),
        ("影视", ("影视", "电影", "movie", "影院")),
        ("少儿", ("少儿", "儿童", "卡通", "动画", "kids", "nick")),
        ("新闻", ("新闻", "news", "cctv13", "cctv2")),
    )
    low = n.lower()
    for group, keys in rules:
        if any(k in low for k in keys):
            return group
    return "其他"


# ------------------------------------------------------------------ M3U 解析

def parse_m3u(content: str, found_via: str = "m3u", source_ref: str = "") -> List[Candidate]:
    """
    严格解析标准 M3U/M3U8 播放列表。
    支持 #EXTINF 属性、#EXTGRP 分组、以及相对地址（按 source_ref 补全）。
    """
    results: List[Candidate] = []
    pending: Dict[str, object] = {}
    # #EXTGRP 是一个"作用域"标签：设置后持续作用于后续条目，
    # 直到遇到新的 #EXTGRP，或条目自身显式声明 group-title 才被覆盖。
    current_grp = ""

    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue

        if line.startswith("#EXTM3U"):
            continue

        if line.startswith("#EXTGRP:"):
            current_grp = line.split(":", 1)[1].strip()
            pending["group"] = current_grp
            continue

        if line.upper().startswith("#EXTINF"):
            m = EXTINF_RE.match(line)
            if m:
                attrs = _attrs_to_dict(m.group("attrs"))
                name = html.unescape(m.group("name").strip())
                pending = {
                    "name": name,
                    "attrs": attrs,
                    "tvg_id": attrs.get("tvg-id", ""),
                    "logo": attrs.get("tvg-logo", ""),
                    # 优先级：条目自身 group-title > #EXTGRP 作用域
                    "group": attrs.get("group-title", "") or current_grp,
                }
            continue

        if line.startswith("#"):
            continue

        # 到这里 line 是地址行
        line = _unescape_url(line)
        if source_ref and not line.lower().startswith(("http://", "https://", "rtmp://", "rtmps://", "rtsp://")):
            line = urljoin(source_ref, line)

        if not _looks_like_stream(line):
            continue

        name = str(pending.get("name") or "").strip()
        if not name:
            name = guess_name_from_url(line)
        group = str(pending.get("group") or "").strip() or guess_group_from_name(name)

        results.append(
            Candidate(
                url=line,
                name=name,
                logo=str(pending.get("logo") or ""),
                group=group,
                tvg_id=str(pending.get("tvg_id") or ""),
                attributes=dict(pending.get("attrs") or {}),
                found_via=found_via,
                source_ref=source_ref,
            )
        )
        pending = {}

    return results


# ------------------------------------------------------------------ 宽松抓取

_TAG_RE = re.compile(r"<[^>]+>")


def _clean_text(raw: str) -> str:
    t = _TAG_RE.sub(" ", raw)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def scrape_text(
    content: str,
    found_via: str = "html",
    source_ref: str = "",
) -> List[Candidate]:
    """
    宽松模式：从任意文本中抽取所有候选流地址。

    频道名推断策略（按优先级）：
      1. 地址所在行的上下文中出现的中文/英文词组（≥2 字符）
      2. 该 URL 的上级 <a> 文本
      3. 从 URL 路径反推
    """
    if not content:
        return []

    seen: set = set()
    out: List[Candidate] = []

    # 关键预处理：JS/JSON 里常见的转义斜杠 https:\/\/host\/a.m3u8
    # 会让 URL 正则在 "\" 处断裂，必须先整体还原再匹配。
    content = _unescape_url(content)

    for regex in (STREAM_URL_RE, STREAM_HINT_RE):
        for m in regex.finditer(content):
            url = _unescape_url(m.group(1)).rstrip("\\\"'),;")
            # 还原 ?target=<encoded> 之类的跳转包装，拿到真实地址
            url = unwrap_redirect(url)
            if not _looks_like_stream(url) or url in seen:
                continue
            seen.add(url)

            # 起始位置可能被"宽松匹配"往前吃掉字符，需要重新定位
            pos = m.start(1)
            ctx_name = _name_from_context(content, pos)
            group = guess_group_from_name(ctx_name)
            out.append(
                Candidate(
                    url=url,
                    name=ctx_name or guess_name_from_url(url),
                    group=group,
                    found_via=found_via,
                    source_ref=source_ref,
                    referer=source_ref if source_ref.startswith("http") else "",
                )
            )

    return out


def _name_from_context(content: str, pos: int, window: int = 120) -> str:
    """
    取地址附近的频道名。

    原则：**就近取名**。优先用离 URL 最近的那个候选文本，而不是窗口内
    第一个 —— 博客页面头部常有 <title>（如"【建议收藏】常用公共的M3U8"），
    按"第一个匹配"取会把整篇文章的标题当成所有流的频道名。
    """
    start = max(0, pos - window)

    # 候选来源，按可靠性排序；每类都记录"离 pos 的距离"用于就近择优
    cands: List[Tuple[int, int, str]] = []  # (优先级, 距离, 文本)

    def collect(pattern: str, prio: int, flags=re.DOTALL | re.IGNORECASE, source: str = content):
        for mm in re.finditer(pattern, source, flags):
            t = _clean_text(mm.group(1))
            if not _is_probable_name(t):
                continue
            cands.append((prio, abs(mm.start() - pos), t))

    # 1) 锚文本（最像频道名）
    collect(r"<a[^>]*>(.{1,60}?)</a>", 0)
    # 2) 行内标题/图片替代文本
    collect(r"<h[1-6][^>]*>(.{1,60}?)</h[1-6]>", 1)
    collect(r'alt="([^"]{1,60})"', 1)
    collect(r'title="([^"]{1,60})"', 2)

    # 3) URL 所在行的纯文本（代码块/文本列表场景）
    line_start = content.rfind("\n", start, pos) + 1
    line_end = content.find("\n", pos)
    line_text = _clean_text(content[line_start : line_end if line_end > 0 else pos + window])
    for token in re.split(r"[\s|,;、·>\-—:：]+", line_text):
        token = token.strip("[](){}<>\"'")
        if _is_probable_name(token):
            cands.append((3, abs(line_start - pos), token))

    if not cands:
        return ""

    # 先按优先级升序，同优先级按距离升序
    cands.sort(key=lambda x: (x[0], x[1]))
    return cands[0][2]


_NAME_OK_RE = re.compile(r"[A-Za-z\u4e00-\u9fff]")
_NAME_BAD_RE = re.compile(
    r"^(?:http|https|www|com|cn|org|net|html?|php|aspx|javascript|function|return|var|const|let|class|import|export|from|true|false|null|none|iframe|script|style|link|meta|div|span|img|src|href)",
    re.IGNORECASE,
)


def _is_probable_name(t: str) -> bool:
    """判断一段文本是否可能是频道名（而非代码、URL、标题、导航词）。"""
    if not t or len(t) < 2 or len(t) > 40:
        return False
    if not _NAME_OK_RE.search(t):
        return False
    if _NAME_BAD_RE.match(t):
        return False
    if t.count(" ") > 4:
        return False
    # 文章标题 / 营销词 / 技术术语 —— 这些是页面文案，不是频道名
    if _TITLE_NOISE_RE.search(t):
        return False
    return True


# 明显是"文章标题/营销文案/技术术语"而非频道名的特征
_TITLE_NOISE_RE = re.compile(
    r"(?:"
    r"建议|收藏|分享|附上|如下|本文|教程|怎么|如何|方法|步骤|方案|技巧|汇总|大全|更新|"
    r"直播源地址|直播源分享|源地址|测试地址|示例|如下地址|可用的|推荐|"
    r"m3u8\s*(?:列表|地址|源)?$|"
    r"playlist|source|stream|channel|video|media|html|javascript|typescript"
    r")",
    re.IGNORECASE,
)


def extract(
    content: str,
    found_via: str = "html",
    source_ref: str = "",
    strict_m3u: bool = True,
) -> List[Candidate]:
    """统一入口：先试 M3U 严格解析，失败/无结果再退回宽松正则。"""
    if not content:
        return []
    if strict_m3u and "#EXTM3U" in content[:2000]:
        got = parse_m3u(content, found_via=found_via, source_ref=source_ref)
        if got:
            # M3U 里也可能夹杂裸地址行，统一补一遍
            got += [c for c in scrape_text(content, found_via, source_ref)
                    if c.url not in {g.url for g in got}]
            return got
    return scrape_text(content, found_via, source_ref)