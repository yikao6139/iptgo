"""
本地网络通路体检：逐条探测发现/验证链路依赖的外部服务，
帮你在本地判断哪些功能能测、哪些受网络限制。

用法：python tests/network_check.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from iptv_engine.config import load_config
from iptv_engine.discovery.base import Fetcher
from iptv_engine.logging_utils import build_session_factory, setup_logging
from iptv_engine.validate.stream_validator import HAS_FFMPEG, FFMPEG_BIN, FFPROBE_BIN

setup_logging("WARNING")
cfg = load_config()
# 走统一工厂，TLS/代理配置与主流程保持一致
sf = build_session_factory(cfg, pool_size=8, retries=1, qps=5)
f = Fetcher(sf, cfg)

# (名称, URL, 所属环节, 失败影响)
# 判定标准用「能否正常抓取内容」而不是「HTTP 状态码」——
# 某些站点（如 raw.githubusercontent.com）本就没有 robots.txt，
# 返回 404 属正常，不能算作不通。
CHECKS = [
    ("GitHub API", "https://api.github.com/rate_limit", "发现·github",
     "GitHub 通路完全不可用"),
    ("GitHub 网页", "https://github.com/", "发现·meta",
     "元列表爬取不可用"),
    ("GitHub Raw", "https://raw.githubusercontent.com/octocat/Hello-World/master/README",
     "发现·github", "GitHub 文件拉取不可用"),
    ("Bing 搜索", "https://www.bing.com/search?q=test", "发现·search",
     "搜索引擎通路不可用"),
    ("DuckDuckGo", "https://html.duckduckgo.com/html/?q=test", "发现·search",
     "备用搜索不可用"),
    ("外网连通性", "https://example.com", "基础", "所有网络功能不可用"),
]

print("=" * 74)
print("  本地网络通路体检")
print("=" * 74)

results = []
for name, url, stage, impact in CHECKS:
    t0 = time.monotonic()
    r = f.fetch(url)
    dt = time.monotonic() - t0
    ok = r.ok
    results.append((name, ok, stage, impact, dt))
    mark = "\033[92m可通\033[0m" if ok else "\033[91m不通\033[0m"
    print(f"  {mark}  {name:16s} {stage:14s} {dt:5.2f}s")
    if not ok:
        print(f"        └─ {r.error[:70]}  →  {impact}")

print()
print("=" * 74)
print("  ffmpeg 状态")
print("=" * 74)
if HAS_FFMPEG:
    print(f"  \033[92m可用\033[0m  ffmpeg  = {FFMPEG_BIN}")
    print(f"        ffprobe = {FFPROBE_BIN}")
    print("  → L5 真实解码验证可正常执行（完整验证）")
else:
    print("  \033[93m缺失\033[0m  未找到 ffmpeg / ffprobe")
    print("  → L5 解码验证会被自动关闭，验证止步于 L4（清单+分片可达）")
    print()
    print("  安装方式（任选其一）：")
    print("    winget install Gyan.FFmpeg")
    print("    choco install ffmpeg")
    print("    或从 https://www.gyan.dev/ffmpeg/builds/ 下载后加到 PATH")
    print("  装完新开终端再跑本脚本即可检测到")

print()
ok_n = sum(1 for _, ok, *_ in results if ok)
print("=" * 74)
print(f"  汇总：{ok_n}/{len(results)} 个外部服务可达")
if ok_n == 0:
    print("  → 本地无法测试任何网络功能，请确认网络/代理设置")
elif ok_n < len(results):
    print("  → 部分通路可用，建议用 --max-check 小批量验证流程")
    print("     python main.py run --max-check 50 --skip-publish")
else:
    print("  → 全部通路可用，可执行完整流程验证")
    print("     python main.py run --max-check 200 --skip-publish")
print("=" * 74)