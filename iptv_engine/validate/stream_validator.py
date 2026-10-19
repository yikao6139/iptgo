"""
流级真实验证器 —— 本项目的核心质量门槛。

设计哲学：**HTTP 200 不等于能播**。
大量"僵尸源"返回 200 + 一个空 m3u8，播放器打开是黑屏。
因此采用五级递进验证，逐级加压，任何一级不过即淘汰：

  L1 网络可达     TCP 连接 + HTTP 状态码
  L2 内容真实     Content-Type 合理 + 体积非零 + 首字节像流/清单
  L3 清单解析     HLS: #EXTM3U + 变体/分片；DASH: MPD 根元素
  L4 分片可达     真实拉取首个分片，确认 200/206 且非空
  L5 真实解码     ffprobe 读出轨道 + ffmpeg 抽帧，拿到码率/分辨率

只有走完 L5（或按配置豁免）才允许入库。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from ..logging_utils import get_logger
from ..normalizer import is_private_host, normalize_url

log = get_logger("validate.probe")


@dataclass
class ProbeResult:
    """单条流的验证结果。"""

    url: str
    ok: bool = False
    stage: str = ""              # 停在哪一级
    reason: str = ""             # 失败原因
    http_status: int = 0
    content_type: str = ""
    content_length: int = 0
    manifest_kind: str = ""      # hls / dash / other
    variants: int = 0            # HLS 变体数
    first_segment_ok: bool = False
    width: int = 0
    height: int = 0
    video_codec: str = ""
    audio_codec: str = ""
    fps: float = 0.0
    bitrate_kbps: int = 0
    audio_channels: str = ""
    duration_probe: float = 0.0
    elapsed: float = 0.0
    score: float = 0.0
    has_audio: bool = False
    has_video: bool = False
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["notes"] = self.notes[:8]
        return d


# ---------------------------------------------------------------- ffmpeg 定位

# 项目根目录（iptv_engine/validate/ -> 上溯两级）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _find_ffmpeg(name: str) -> Optional[str]:
    """
    定位 ffmpeg/ffprobe，按优先级：

      1. 环境变量 FFMPEG_BIN / FFPROBE_BIN（显式指定）
      2. 项目内 ffmpeg/bin/（便携部署，零配置 —— 解压到项目根即可用）
      3. 系统 PATH
      4. Windows 常见安装路径

    返回可执行文件的绝对路径，找不到返回 None。
    """
    exe = f"{name}.exe" if os.name == "nt" else name

    # 1) 环境变量
    env = os.environ.get(f"{name.upper()}_BIN")
    if env and os.path.exists(env):
        return env

    # 2) 项目内便携目录：ffmpeg/bin/ffmpeg.exe、ffmpeg/ffmpeg.exe
    for rel in (Path("ffmpeg") / "bin" / exe,
                Path("ffmpeg") / exe,
                Path("bin") / exe):
        p = _PROJECT_ROOT / rel
        if p.exists():
            return str(p)

    # 3) 系统 PATH
    found = shutil.which(name)
    if found:
        return found

    # 4) Windows 常见安装位置
    if os.name == "nt":
        for base in (
            r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin",
            r"C:\ProgramData\chocolatey\bin",
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"),
        ):
            p = os.path.join(base, exe)
            if os.path.exists(p):
                return p
    return None


FFMPEG_BIN = _find_ffmpeg("ffmpeg")
FFPROBE_BIN = _find_ffprobe = _find_ffmpeg("ffprobe")
HAS_FFMPEG = bool(FFMPEG_BIN and FFPROBE_BIN)


# ---------------------------------------------------------------- HLS 解析

def parse_hls(content: str, base_url: str) -> Tuple[int, List[str]]:
    """
    解析 m3u8。

    返回 (变体数, URI 列表)。
    - master playlist：变体数 > 0，URI 列表为各清晰度档的入口 m3u8
    - media playlist：变体数 == 0，URI 列表为 ts/fmp4 分片
    - 空清单：两者皆 0（上层据此判定为僵尸源）

    注意：HLS 的 URI 绝大多数是相对路径，必须以 base_url 解析，
    不能只收绝对地址。
    """
    variants: List[str] = []
    segments: List[str] = []
    lines = content.splitlines()
    pending_uri = False
    extinf_pending = False

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            # master playlist：标签的下一行是变体 URI（可能是相对路径）
            if "URI=" in line:
                m = re.search(r'URI="([^"]+)"', line)
                if m:
                    variants.append(urljoin(base_url, m.group(1)))
            else:
                pending_uri = True
            continue
        if line.startswith("#EXT-X-I-FRAME-STREAM-INF"):
            # URI="xxx" 内联形式（独立标签行）
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                variants.append(urljoin(base_url, m.group(1)))
            continue
        if line.startswith("#EXTINF"):
            extinf_pending = True
            continue
        if line.startswith("#"):
            # 其它标签不打断 URI 挂起（#EXT-X-MEDIA 后也可能跟 URI）
            continue

        # 非注释行 = URI 行，统一交给 urljoin 以 base_url 解析
        if pending_uri:
            variants.append(urljoin(base_url, line))
            pending_uri = False
        elif extinf_pending:
            segments.append(urljoin(base_url, line))
            extinf_pending = False

    # master 里变体为 0，但有 media 标签 => 视为合法
    if not variants and not segments and "#EXTM3U" in content:
        # 尝试找任意 URI= 引用
        variants = [urljoin(base_url, u) for u in re.findall(r'URI="([^"]+)"', content)]

    return len(variants), (variants or segments)


def looks_like_dash(content: str) -> bool:
    head = content.lstrip()[:400].lower()
    return head.startswith("<mpd") or "<mpd " in head or 'xmlns="http://www.w3.org/2001/manifest"' in head


# ---------------------------------------------------------------- 探测器

class StreamValidator:
    """
    单条流的验证器。线程安全，每个实例绑定一个 HTTP 会话。

    注意：本类设计为"每线程一个实例"使用，通过 validator_factory 提供。
    """

    def __init__(self, cfg: dict, session_factory):
        self.cfg = cfg
        v = cfg.get("validation", {}) or {}
        self.http_timeout = int(v.get("http_timeout", 10))
        self.allow_status = set(v.get("allow_status", [200, 206, 301, 302]))
        self.require_manifest = bool(v.get("require_manifest", True))
        self.require_decode = bool(v.get("require_decode", True))
        self.ffmpeg_timeout = int(v.get("ffmpeg_timeout", 18))
        self.probe_duration = int(v.get("probe_duration", 6))
        self.min_kbps = int(v.get("min_video_kbps", 120))
        self.min_width = int(v.get("min_resolution_width", 160))
        self.per_host_concurrency = int(v.get("per_host_concurrency", 3))
        # 允许访问内网地址（仅本地演示/自测用，默认关闭）
        self.allow_private = bool(v.get("allow_private_hosts", False))
        self.user_agent = (cfg.get("crawler", {}) or {}).get("user_agent", "Mozilla/5.0")
        self.sf = session_factory
        # 每主机并发闸
        self._host_sem: Dict[str, threading.Semaphore] = {}
        self._host_sem_lock = threading.Lock()

    # ------------------------------------------------------------ 工具
    def _sem_for(self, host: str) -> threading.Semaphore:
        with self._host_sem_lock:
            sem = self._host_sem.get(host)
            if sem is None:
                if len(self._host_sem) > 4096:
                    self._host_sem.clear()
                sem = threading.Semaphore(self.per_host_concurrency)
                self._host_sem[host] = sem
            return sem

    def _headers_for(self, candidate) -> Dict[str, str]:
        h = {"User-Agent": candidate.user_agent or self.user_agent}
        if candidate.referer:
            h["Referer"] = candidate.referer
        return h

    # ------------------------------------------------------------ L1/L2
    def _l1_l2_http(self, url: str, headers: Dict[str, str]) -> Tuple[ProbeResult, str]:
        """L1 网络可达 + L2 内容真实。返回 (result, body_text)。"""
        res = ProbeResult(url=url)
        host = (urlsplit(url).hostname or "").lower()
        sem = self._sem_for(host)

        with sem:
            try:
                self.sf.limiter.wait()
                session = self.sf.get()
                resp = session.get(
                    url,
                    headers=headers,
                    timeout=(self._l1_connect_timeout(), self.http_timeout),
                    allow_redirects=True,
                    stream=True,
                )
            except Exception as exc:  # noqa: BLE001
                res.stage = "L1"
                res.reason = f"连接失败: {type(exc).__name__}"
                return res, ""

        res.http_status = resp.status_code
        res.content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
        final_url = resp.url

        if resp.status_code not in self.allow_status:
            res.stage = "L1"
            res.reason = f"HTTP {resp.status_code}"
            resp.close()
            return res, ""

        # ---- L2：读首块，判断是不是"活的"内容 ----
        try:
            head = resp.raw.read(65536, decode_content=True) or b""
        except Exception:  # noqa: BLE001
            head = b""
        res.content_length = int(resp.headers.get("Content-Length", 0) or 0)
        resp.close()

        if not head:
            res.stage = "L2"
            res.reason = "空响应体"
            return res, ""

        # 采集完整文本（清单类需要全文，媒体流只看头）
        text = ""
        is_text_like = (
            "mpegurl" in res.content_type
            or "dash+xml" in res.content_type
            or res.content_type.startswith("text/")
            or url.lower().split("?")[0].endswith((".m3u8", ".m3u", ".mpd"))
            or head[:7] == b"#EXTM3U"
        )
        if is_text_like:
            try:
                resp2 = session.get(url, headers=headers, timeout=(5, self.http_timeout))
                text = resp2.text
                resp2.close()
            except Exception:  # noqa: BLE001
                text = head.decode("utf-8", errors="ignore")

        res.stage = "L2"
        return res, text

    def _l1_connect_timeout(self) -> int:
        return (self.cfg.get("crawler", {}) or {}).get("connect_timeout", 8)

    # ------------------------------------------------------------ L3 清单
    def _l3_manifest(self, res: ProbeResult, text: str, url: str) -> Tuple[ProbeResult, List[str]]:
        """解析清单，返回 (result, 分片/变体URL列表)。"""
        if not text.strip():
            res.stage = "L3"
            res.reason = "无清单内容"
            return res, []

        stripped = text.lstrip()[:200]
        if looks_like_dash(text):
            res.manifest_kind = "dash"
            res.stage = "L3"
            # DASH 不做分片验证，直接进解码
            return res, []

        if "#EXTM3U" not in text[:4000]:
            # 不是清单：如果是媒体二进制流（ts/flv/mp4），标记 other 直接进解码
            res.manifest_kind = "other"
            res.stage = "L3"
            return res, []

        res.manifest_kind = "hls"
        # parse_hls 返回 (变体数, URI列表)；URI 列表对 master 是各档入口，
        # 对 media 是分片。任一非空即视为有效清单。
        variant_count, uris = parse_hls(text, url)
        res.variants = variant_count

        if uris:
            return res, uris

        res.stage = "L3"
        res.reason = "m3u8 无任何 URI（空清单/仅含广告标签）"
        return res, []

    # ------------------------------------------------------------ L4 分片
    def _l4_segment(self, res: ProbeResult, urls: List[str], headers: Dict[str, str]) -> ProbeResult:
        """拉取首个分片/变体，确认真实可达。"""
        if not urls:
            return res
        target = urls[0]
        try:
            self.sf.limiter.wait()
            session = self.sf.get()
            resp = session.get(
                target,
                headers=headers,
                timeout=(5, self.http_timeout),
                allow_redirects=True,
                stream=True,
            )
            code = resp.status_code
            ctype = resp.headers.get("Content-Type", "")
            data = b""
            try:
                for chunk in resp.iter_content(32768):
                    data += chunk
                    if len(data) >= 8192:
                        break
            finally:
                resp.close()

            if code in (200, 206) and data:
                res.first_segment_ok = True
                res.stage = "L4"
                return res
            res.stage = "L4"
            res.reason = f"分片不可达 HTTP {code}"
            return res
        except Exception as exc:  # noqa: BLE001
            res.stage = "L4"
            res.reason = f"分片请求异常 {type(exc).__name__}"
            return res

    # ------------------------------------------------------------ L5 解码
    def _l5_ffprobe(self, res: ProbeResult, url: str, headers: Dict[str, str]) -> ProbeResult:
        """ffprobe 读取轨道信息。"""
        if not FFPROBE_BIN:
            res.stage = "L5"
            res.reason = "ffprobe 不可用"
            return res

        cmd = [
            FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
            "-user_agent", headers.get("User-Agent", self.user_agent),
            "-rw_timeout", str(self.http_timeout * 1_000_000),
            "-analyzeduration", "6000000",
            "-probesize", "1000000",
            "-show_streams", "-show_format", url,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=self.ffmpeg_timeout,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            res.stage = "L5"
            res.reason = "ffprobe 超时"
            return res
        except Exception as exc:  # noqa: BLE001
            res.stage = "L5"
            res.reason = f"ffprobe 执行失败 {type(exc).__name__}"
            return res

        if proc.returncode != 0 or not proc.stdout:
            err = proc.stderr.decode("utf-8", errors="ignore").strip().splitlines()
            res.stage = "L5"
            res.reason = f"ffprobe 无法解析: {err[-1][:80] if err else 'unknown'}"
            return res

        try:
            info = json.loads(proc.stdout.decode("utf-8", errors="ignore"))
        except json.JSONDecodeError:
            res.stage = "L5"
            res.reason = "ffprobe 输出解析失败"
            return res

        streams = info.get("streams", []) or []
        fmt = info.get("format", {}) or {}
        for st in streams:
            ctype = st.get("codec_type")
            if ctype == "video" and not res.has_video:
                # 排除封面图轨
                if st.get("disposition", {}).get("attached_pic"):
                    continue
                res.has_video = True
                res.width = int(st.get("width", 0) or 0)
                res.height = int(st.get("height", 0) or 0)
                res.video_codec = st.get("codec_name", "")
                fps = st.get("avg_frame_rate") or st.get("r_frame_rate") or "0/1"
                try:
                    num, den = fps.split("/")
                    res.fps = round(float(num) / float(den), 2) if float(den) else 0.0
                except (ValueError, ZeroDivisionError):
                    res.fps = 0.0
                br = int(st.get("bit_rate", 0) or 0)
                res.bitrate_kbps = max(res.bitrate_kbps, br // 1000)
            elif ctype == "audio" and not res.has_audio:
                res.has_audio = True
                res.audio_codec = st.get("codec_name", "")
                ch = st.get("channels", 0)
                res.audio_channels = f"{ch}ch" if ch else ""

        if fmt.get("bit_rate"):
            res.bitrate_kbps = max(res.bitrate_kbps, int(fmt["bit_rate"]) // 1000)
        try:
            res.duration_probe = float(fmt.get("duration", 0) or 0)
        except (TypeError, ValueError):
            res.duration_probe = 0.0

        if not res.has_video and not res.has_audio:
            res.stage = "L5"
            res.reason = "无音视频轨道（空流）"
            return res

        res.stage = "L5"
        return res

    def _l5_ffmpeg_decode(self, res: ProbeResult, url: str) -> ProbeResult:
        """ffmpeg 真抽帧：这是最硬的一道门槛，确认真的能解码出画面。"""
        if not FFMPEG_BIN:
            return res
        cmd = [
            FFMPEG_BIN, "-hide_banner", "-loglevel", "error",
            "-user_agent", self.user_agent,
            "-rw_timeout", str(self.http_timeout * 1_000_000),
            "-i", url,
            "-t", str(self.probe_duration),
            "-map", "0:v?", "-map", "0:a?",
            "-f", "null", "-",
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=self.ffmpeg_timeout, shell=False,
            )
        except subprocess.TimeoutExpired:
            # 超时但已可能解出部分数据，按"有数据"处理，靠 ffprobe 结果兜底
            res.notes.append("ffmpeg 解码超时（按部分成功处理）")
            return res
        except Exception as exc:  # noqa: BLE001
            res.notes.append(f"ffmpeg 执行异常 {type(exc).__name__}")
            return res

        stderr = proc.stderr.decode("utf-8", errors="ignore")
        # 致命错误关键词
        fatal = ("Invalid data found", "404 Not Found", "Connection refused",
                 "Server returned", "Protocol not supported", "No such file")
        for kw in fatal:
            if kw in stderr:
                res.stage = "L5"
                res.reason = f"解码失败: {kw}"
                return res
        return res

    # ------------------------------------------------------------ 评分
    def _score(self, res: ProbeResult) -> float:
        """给通过验证的流打分，用于输出排序。"""
        score = 50.0
        if res.manifest_kind == "hls":
            score += 15
        elif res.manifest_kind == "dash":
            score += 8
        if res.has_video:
            score += 15
            w = res.width or 0
            if w >= 1920: score += 15
            elif w >= 1280: score += 12
            elif w >= 720: score += 8
            elif w >= 480: score += 4
        if res.has_audio:
            score += 5
        if res.bitrate_kbps >= 4000: score += 10
        elif res.bitrate_kbps >= 1500: score += 7
        elif res.bitrate_kbps >= 600: score += 4
        if res.variants > 1:
            score += 3
        if res.fps and res.fps >= 25:
            score += 3
        # 高分但低码率可能是转码低清，轻微扣分
        if res.has_video and res.bitrate_kbps and res.bitrate_kbps < self.min_kbps:
            score -= 15
        return round(max(0.0, min(100.0, score)), 2)

    # ------------------------------------------------------------ 主流程
    def validate(self, candidate) -> ProbeResult:
        """对单条候选执行完整五级验证。"""
        url = candidate.url
        started = time.monotonic()
        res = ProbeResult(url=url)

        # L0 前置校验：只拦空地址与内网/保留地址（SSRF 防护）。
        # 注意 is_private_host 返回 True 表示"该拦截"，
        # 这里取反会导致把正常公网源全部误杀。
        if not url or not url.strip():
            res.ok = False
            res.stage = "L0"
            res.reason = "空地址"
            return res
        # allow_private 仅供本地演示/自测使用（默认关闭）。
        # 开启前请确认你清楚风险：验证器会去访问内网地址。
        if is_private_host(url) and not self.allow_private:
            res.ok = False
            res.stage = "L0"
            res.reason = "内网/保留地址（已拦截）"
            return res

        headers = self._headers_for(candidate)

        # L1 + L2
        res, text = self._l1_l2_http(url, headers)
        if res.reason:
            res.elapsed = time.monotonic() - started
            return res

        # L3
        res, sub_urls = self._l3_manifest(res, text, url)
        if res.reason:
            res.elapsed = time.monotonic() - started
            return res

        # L4
        if self.require_manifest and sub_urls:
            res = self._l4_segment(res, sub_urls, headers)
            if res.reason:
                res.elapsed = time.monotonic() - started
                return res

        # L5
        if self.require_decode:
            # 优先用选中的变体/分片做解码，master 清单直接喂可能不解码
            probe_url = sub_urls[0] if (res.manifest_kind == "hls" and sub_urls) else url
            res = self._l5_ffprobe(res, probe_url, headers)
            if res.reason:
                res.elapsed = time.monotonic() - started
                return res
            res = self._l5_ffmpeg_decode(res, probe_url)
            if res.reason:
                res.elapsed = time.monotonic() - started
                return res
        else:
            res.has_video = res.has_video or res.manifest_kind in ("hls", "dash")
            res.notes.append("已跳过解码验证（配置豁免）")

        # ---- 质量门槛 ----
        # 分辨率：ffprobe 对视频轨的 width/height 读数可靠，可作硬门槛。
        if res.has_video and res.width and res.width < self.min_width:
            res.ok = False
            res.reason = f"分辨率过低 ({res.width}x{res.height})"
            res.elapsed = time.monotonic() - started
            return res

        # 码率：**不作为准入门槛**。
        # 实测 ffprobe 读 HLS 清单时 format.bit_rate 恒为 0（清单本身无此信息），
        # 只有落到具体分片才读得到，而分片仅 2 秒左右，读数剧烈波动。
        # 拿它做硬门槛会大量误杀真实的低码率直播源（如新闻标清 200-400kbps）。
        # 因此码率只参与评分排序（见 _score），不做淘汰判定。
        if res.has_video and res.bitrate_kbps and res.bitrate_kbps < self.min_kbps:
            res.notes.append(f"码率偏低({res.bitrate_kbps}kbps)，仅降权不淘汰")

        res.ok = True
        res.score = self._score(res)
        res.elapsed = time.monotonic() - started
        return res