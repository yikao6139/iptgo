"""
本地演示模式：用 ffmpeg 现场生成真实的 HLS 直播流，
走完整的「发现 → 验证 → 注册表 → 淘汰 → 渲染」链路，
产出**真实可播放**的 live.m3u / live.txt。

用途：
  1. 验证渲染逻辑在真实数据下的正确性（不只是单元测试里的假数据）
  2. 产出一份格式规范、能直接喂给播放器的示例播放列表
  3. 当外网挖不到活源时，仍可完整演示流程

这些流由本地 HTTP 服务器提供，仅本机可播；正式使用请跑真实发现流程。

用法：
  python tests/demo_local.py            # 生成并写出到 output/
  python tests/demo_local.py --serve    # 生成后常驻，本机可直接播放
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from iptv_engine.config import ROOT_DIR, load_config
from iptv_engine.extractor import Candidate
from iptv_engine.logging_utils import SessionFactory, setup_logging
from iptv_engine.registry import SourceRegistry
from iptv_engine.renderer import render_all
from iptv_engine.validate.stream_validator import (
    FFMPEG_BIN,
    HAS_FFMPEG,
    StreamValidator,
    parse_hls,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from logo_gen import make_logo  # noqa: E402

setup_logging("INFO")
log = __import__("iptv_engine.logging_utils", fromlist=["get_logger"]).get_logger("demo")

WORK = Path(tempfile.mkdtemp(prefix="iptv_demo_"))

# 台标参数：(频道名, 分组, 宽, 高, 帧率, 测试图)
CHANNELS = [
    ("CCTV-1 综合", "央视", 1280, 720, 25, "smptebars"),
    ("CCTV-5+ 体育", "体育", 960, 540, 25, "smptehdbars"),
    ("湖南卫视", "卫视", 1280, 720, 25, "testsrc"),
    ("CCTV-13 新闻", "新闻", 854, 480, 25, "rgbtestsrc"),
    ("东方卫视", "卫视", 1280, 720, 25, "testsrc2"),
    ("CCTV-6 电影", "影视", 1280, 720, 24, "testsrc"),
    ("深圳卫视", "卫视", 1024, 576, 25, "smptebars"),
    ("金鹰卡通", "少儿", 640, 360, 25, "testsrc2"),
    ("CHC 高清剧场", "影视", 1280, 720, 25, "testsrc"),
    ("CCTV-4 中文国际", "国际", 1280, 720, 25, "smptehdbars"),
    ("辽宁卫视", "卫视", 1280, 720, 25, "testsrc"),
    ("北京卫视", "卫视", 1280, 720, 25, "smptebars"),
]


class Handler(BaseHTTPRequestHandler):
    files: dict = {}

    def do_GET(self):
        name = self.path.split("?")[0].lstrip("/")
        body = self.files.get(name)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode("utf-8") if isinstance(body, str) else body
        rng = self.headers.get("Range")
        if rng and "bytes=" in rng:
            try:
                start = int(rng.split("bytes=")[1].split("-")[0] or 0)
            except ValueError:
                start = 0
            chunk = data[start:start + 65536]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{start+len(chunk)-1}/{len(data)}")
        else:
            chunk = data
            self.send_response(200)
        self.send_header("Content-Type",
                         "application/vnd.apple.mpegurl" if name.endswith(".m3u8")
                         else "video/mp2t")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)

    def log_message(self, *a):
        pass


def make_channel(slug: str, w: int, h: int, fps: int, pattern: str,
                 duration: int = 8) -> bool:
    """
    用 ffmpeg 生成一条真实的 HLS 流。

    注意：ffmpeg 写 m3u8 时用的是**分片的裸文件名**（如 seg0.ts），
    所以注册 Handler 键名时必须与 m3u8 里引用的完全一致，
    否则验证器的分片请求会 404。
    """
    seg_dir = WORK / slug
    seg_dir.mkdir(exist_ok=True)
    m3u8 = WORK / f"{slug}.m3u8"
    try:
        subprocess.run([
            FFMPEG_BIN, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"{pattern}=size={w}x{h}:rate={fps}:duration={duration}",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-g", str(fps), "-b:v", "2500k",
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
            "-hls_segment_filename", str(seg_dir / "seg%d.ts"), str(m3u8),
        ], check=True, capture_output=True, timeout=180)
    except Exception as exc:  # noqa: BLE001
        log.warning("生成 %s 失败: %s", slug, str(exc)[:90])
        return False

    content = m3u8.read_text(encoding="utf-8")
    Handler.files[f"{slug}.m3u8"] = content

    # 按 m3u8 里实际引用的 URI 注册分片（urljoin 后与请求路径一致）
    variant_count, uris = parse_hls(content, f"{slug}.m3u8")
    registered = 0
    for uri in uris:
        # uri 可能带 query，统一去 query
        key = uri.split("?")[0].lstrip("/")
        src = WORK / key
        if not src.exists():
            src = seg_dir / key
        if src.exists():
            Handler.files[key] = src.read_bytes()
            registered += 1
    # 兜底：把目录下所有 ts 也按裸文件名注册（防 m3u8 引用形式与预期不同）
    for p in seg_dir.glob("*.ts"):
        Handler.files.setdefault(f"{slug}/{p.name}", p.read_bytes())
        Handler.files.setdefault(p.name, p.read_bytes())

    if registered == 0:
        log.warning("%s 分片注册失败（m3u8 引用: %s）", slug, uris[:2])
        return False
    return True


def run_demo(out_dir: Path, registry_path: Path, serve: bool) -> int:
    if not HAS_FFMPEG:
        print("\n需要 ffmpeg 才能生成演示流。请确认 ffmpeg/bin/ 存在。")
        return 1

    print("\n" + "=" * 72)
    print("  本地演示模式：生成真实可播的 HLS 流并渲染播放列表")
    print("=" * 72)

    # 1. 启动本地服务器
    Handler.files = {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"\n[1/5] 本地流服务器已启动: {base}")

    # 2. 生成流
    print(f"[2/5] 用 ffmpeg 生成 {len(CHANNELS)} 条真实 HLS 流与台标…")
    candidates = []
    ok = 0
    for i, (name, group, w, h, fps, pattern) in enumerate(CHANNELS, 1):
        slug = f"ch{i:02d}"
        if not make_channel(slug, w, h, fps, pattern):
            continue

        # 生成台标（tvg-logo）。真实流程里 logo 来自上游 m3u 的 tvg-logo 属性，
        # 演示频道是全新的，需要本地合成，否则播放器里只能显示文字。
        logo_url = ""
        png = make_logo(FFMPEG_BIN, name, group, WORK, slug)
        if png:
            Handler.files[f"{slug}.png"] = png.read_bytes()
            logo_url = f"{base}/{slug}.png"

        candidates.append(Candidate(
            url=f"{base}/{slug}.m3u8", name=name,
            # 演示源统一归入「演示」分组，与真实发现的直播源区分开，
            # 避免播放列表里混进 127.0.0.1 的假源
            group="演示", logo=logo_url, found_via="local-demo",
        ))
        ok += 1
        print(f"      {i:2d}/{len(CHANNELS)}  {name:16s} {w}x{h} @{fps}fps"
              f"{'  [台标✓]' if logo_url else '  [台标✗]'}")
    print(f"      生成成功 {ok}/{len(CHANNELS)} 条，"
          f"其中台标 {sum(1 for c in candidates if c.logo)} 个")

    if not candidates:
        print("演示流生成失败")
        srv.shutdown()
        return 1

    # 3. 注册表入库
    print("[3/5] 写入注册表…")
    if registry_path.exists():
        registry_path.unlink()
    reg = SourceRegistry(registry_path)
    reg.upsert_candidates(candidates)

    # 4. 五级验证（服务器在 127.0.0.1，需显式放开内网访问）
    print("[4/5] 执行五级真实验证（ffprobe + ffmpeg 抽帧）…")
    cfg = load_config()
    cfg["validation"]["require_decode"] = True
    cfg["validation"]["allow_private_hosts"] = True
    sf = SessionFactory(user_agent="demo", pool_size=6, retries=0, qps=50)
    validator = StreamValidator(cfg, sf)

    passed = 0
    for c in candidates:
        res = validator.validate(c)
        if res.ok:
            passed += 1
            reg.record_success(res, name=c.name, group=c.group)
            print(f"      \033[92m✓\033[0m {c.name:16s} "
                  f"{res.width}x{res.height} {res.bitrate_kbps:>5}kbps "
                  f"{res.video_codec} 评分 {res.score}")
        else:
            reg.record_failure(res, name=c.name)
            print(f"      \033[91m✗\033[0m {c.name:16s} [{res.stage}] {res.reason[:44]}")

    # 5. 渲染
    print("[5/5] 渲染播放列表…")
    stats = render_all(reg, cfg, out_dir, extra_stats=reg.compute_stats())
    reg.save()

    print("\n" + "=" * 72)
    print(f"  完成：{passed}/{len(candidates)} 条通过真实解码验证")
    print(f"  产物：{out_dir / 'live.m3u'}")
    print(f"        {out_dir / 'live.txt'}")
    print("=" * 72)

    if serve:
        print("\n【常驻服务】本机可直接用 VLC 播放以下地址，Ctrl+C 退出：")
        for c in candidates:
            print(f"  {c.url}")
        print(f"\n同时订阅生成的文件（需播放器支持本地 m3u）：")
        print(f"  file:///{(out_dir / 'live.m3u').as_posix()}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n已停止")

    srv.shutdown()
    shutil.rmtree(WORK, ignore_errors=True)
    return 0 if passed else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="本地演示模式")
    ap.add_argument("--serve", action="store_true", help="生成后常驻服务器供本机播放")
    ap.add_argument("--out", default=str(ROOT_DIR / "output"), help="输出目录")
    ap.add_argument("--registry", default=str(ROOT_DIR / "sources" / "registry.json"))
    args = ap.parse_args()
    return run_demo(Path(args.out), Path(args.registry), args.serve)


if __name__ == "__main__":
    sys.exit(main())