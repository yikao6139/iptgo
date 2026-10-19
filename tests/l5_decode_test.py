"""
L5 解码验证专项测试。

用本地可控的方式验证 ffprobe/ffmpeg 判定逻辑：
  1. 先造一个本地可播的 HLS 流（ffmpeg 生成真实分片）→ 必须通过 L3/L4/L5
  2. 造一个"清单有效但内容损坏"的僵尸流 → 必须卡在 L5
  3. 造一个空清单 → 必须卡在 L3
这样不依赖外网，就能确认 L5 是"精准拦截"而不是"全部拦截"。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from iptv_engine.config import load_config
from iptv_engine.extractor import Candidate
from iptv_engine.logging_utils import SessionFactory
from iptv_engine.validate.stream_validator import (
    FFMPEG_BIN,
    HAS_FFMPEG,
    StreamValidator,
)

PASS = "\033[92m  PASS\033[0m"
FAIL = "\033[91m  FAIL\033[0m"
_results = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, cond))
    print(f"{PASS if cond else FAIL}  {name}" + (f"  --  {detail}" if detail and not cond else ""))


WORK = Path(tempfile.mkdtemp(prefix="l5_test_"))


class Handler(BaseHTTPRequestHandler):
    """按文件名返回预置内容；.m3u8 走真实 HTTP 流程。"""
    files: dict = {}

    def do_GET(self):
        name = self.path.split("?")[0].lstrip("/")
        # 支持 Range（探测分片时会带）
        rng = self.headers.get("Range")
        body = self.files.get(name)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode("utf-8") if isinstance(body, str) else body
        if rng and "bytes=" in rng:
            start = int(rng.split("bytes=")[1].split("-")[0] or 0)
            chunk = data[start:start + 32768]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{start+len(chunk)-1}/{len(data)}")
            self.send_header("Accept-Ranges", "bytes")
        else:
            chunk = data
            self.send_response(200)
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/vnd.apple.mpegurl"
                         if name.endswith(".m3u8") else "video/mp2t")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)

    def log_message(self, *a):
        pass


def make_real_hls() -> bool:
    """用 ffmpeg 生成一个真实的 6 秒 HLS 流（testsrc 有画面）。"""
    seg_dir = WORK / "seg"
    seg_dir.mkdir(exist_ok=True)
    m3u8 = WORK / "real.m3u8"
    try:
        subprocess.run([
            FFMPEG_BIN, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=640x480:rate=25:duration=6",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-g", "25", "-f", "hls", "-hls_time", "2",
            "-hls_list_size", "0", "-hls_segment_filename",
            str(seg_dir / "seg%d.ts"), str(m3u8),
        ], check=True, capture_output=True, timeout=120)
    except Exception as exc:  # noqa: BLE001
        print(f"  [跳过] 无法生成测试流: {exc}")
        return False
    # 收集分片内容进内存
    Handler.files["real.m3u8"] = m3u8.read_text(encoding="utf-8")
    for p in sorted(seg_dir.glob("seg*.ts")):
        Handler.files[p.name] = p.read_bytes()
    return True


def make_zombie_hls() -> None:
    """僵尸流：清单结构完全合法，但分片是垃圾字节。"""
    seg = b"\x00" * 2048          # 非 TS 数据，ffprobe 解不出
    Handler.files["zombie.m3u8"] = (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n"
        "#EXTINF:4.0,\nzseg0.ts\n#EXTINF:4.0,\nzseg1.ts\n#EXT-X-ENDLIST\n"
    )
    Handler.files["zseg0.ts"] = seg
    Handler.files["zseg1.ts"] = seg


def make_empty_hls() -> None:
    """空清单：无任何 URI。"""
    Handler.files["empty.m3u8"] = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"


def make_dead_link() -> None:
    """404：应卡在 L1。"""
    pass


def run() -> bool:
    print("\n" + "=" * 72)
    print("  L5 真实解码验证专项测试")
    print("=" * 72)

    if not HAS_FFMPEG:
        print("\n\033[91m  ffmpeg 不可用，无法测试 L5\033[0m")
        return False
    print(f"\nffmpeg  = {FFMPEG_BIN}")
    print(f"ffprobe = {sys.modules['iptv_engine.validate.stream_validator'].FFPROBE_BIN}")

    # 启动本地服务器（IPv4 + 非标准端口，避开权限与占用问题）
    Handler.files = {}
    ok_real = make_real_hls()
    make_zombie_hls()
    make_empty_hls()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    print(f"本地测试服务器: {base}")

    cfg = load_config()
    cfg["validation"]["require_decode"] = True
    # 本地测试服务器在 127.0.0.1，用显式开关放开内网访问
    # （而不是 monkey-patch 函数，避免污染模块全局状态）
    cfg["validation"]["allow_private_hosts"] = True
    sf = SessionFactory(user_agent="l5-test", pool_size=4, retries=0, qps=50)
    v = StreamValidator(cfg, sf)

    try:
        print("\n[1] 真实可播 HLS 流 → 必须走到 L5 并通过")
        if ok_real:
            res = v.validate(Candidate(url=f"{base}/real.m3u8", name="测试台标流"))
            print(f"    stage={res.stage} ok={res.ok} "
                  f"{res.width}x{res.height} {res.bitrate_kbps}kbps "
                  f"fps={res.fps} codec={res.video_codec}")
            if not res.ok:
                print(f"    reason={res.reason}")
            check("真实流通过 L5 解码验证", res.ok, f"stage={res.stage} {res.reason}")
            check("识别出分辨率 640x480", res.width == 640 and res.height == 480,
                  f"{res.width}x{res.height}")
            check("识别出有视频轨", res.has_video)
            check("给出了质量评分", res.score > 0, f"score={res.score}")
        else:
            print("    [跳过] 测试流生成失败")

        print("\n[2] 僵尸流（清单合法、分片是垃圾）→ 必须卡在 L5")
        res2 = v.validate(Candidate(url=f"{base}/zombie.m3u8", name="僵尸流"))
        print(f"    stage={res2.stage} ok={res2.ok} reason={res2.reason}")
        check("僵尸流未通过", not res2.ok)
        check("僵尸流卡在 L5（而非更早阶段）",
              res2.stage == "L5", f"stage={res2.stage}")
        check("确认已到清单解析阶段",
              res2.manifest_kind == "hls", res2.manifest_kind)

        print("\n[3] 空清单 → 必须卡在 L3")
        res3 = v.validate(Candidate(url=f"{base}/empty.m3u8", name="空清单"))
        print(f"    stage={res3.stage} ok={res3.ok} reason={res3.reason}")
        check("空清单卡在 L3", res3.stage == "L3", f"stage={res3.stage}")
        check("空清单未被通过", not res3.ok)

        print("\n[4] 不存在的路径 → 必须卡在 L1")
        res4 = v.validate(Candidate(url=f"{base}/notexist.m3u8", name="不存在"))
        print(f"    stage={res4.stage} ok={res4.ok} reason={res4.reason}")
        check("404 卡在 L1", res4.stage == "L1", f"stage={res4.stage}")
    finally:
        srv.shutdown()
        shutil.rmtree(WORK, ignore_errors=True)

    # ---- SSRF 防护仍然有效：必须用默认配置的新实例（allow_private=False）----
    print("\n[5] 默认配置下，SSRF 防护必须仍然生效")
    strict_cfg = load_config()
    strict_cfg["validation"]["allow_private_hosts"] = False
    strict = StreamValidator(strict_cfg, SessionFactory(
        user_agent="l5-test", pool_size=2, retries=0, qps=50))
    for private in ("http://192.168.1.1/live/a.m3u8",
                    "http://127.0.0.1/a.m3u8",
                    "http://10.0.0.5/a.m3u8"):
        r = strict.validate(Candidate(url=private, name="内网"))
        check(f"默认配置拦截 {private[:30]}", r.stage == "L0" and not r.ok,
              f"stage={r.stage}")
    # 反向验证：开启开关后内网可达（说明开关真的生效，不是形同虚设）
    check("allow_private=True 时开关已开启", v.allow_private is True, "开关未生效")
    r_open = v.validate(Candidate(url="http://192.168.1.1/live/a.m3u8", name="内网"))
    check("开关开启后内网不再卡在 L0", r_open.stage != "L0",
          f"stage={r_open.stage}")

    total = len(_results)
    failed = [n for n, c in _results if not c]
    print("\n" + "=" * 72)
    if failed:
        print(f"\033[91m  L5 专项测试未通过：{len(failed)}/{total} 项失败\033[0m")
        for n in failed:
            print(f"    - {n}")
        print("=" * 72)
        return False
    print(f"\033[92m  L5 专项测试全部通过：{total}/{total} 项\033[0m")
    print("=" * 72)
    return True


if __name__ == "__main__":
    sys.exit(0 if run() else 1)