#!/usr/bin/env python3
"""
命令行入口。

用法示例：
  # 完整跑一轮（发现 + 验证 + 渲染 + 发布）
  python main.py run

  # 只跑发现，看看能捞到多少候选（不验证不发布）
  python main.py discover

  # 只验证已有注册表里的源
  python main.py validate

  # 只渲染输出（改完配置想重新排版时用）
  python main.py render

  # 只发布本地已生成的产物
  python main.py publish

  # 查看注册表统计
  python main.py stats

  # 本地冒烟自检（不需要网络）
  python main.py selftest
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from iptv_engine.config import ROOT_DIR, load_config            # noqa: E402
from iptv_engine.logging_utils import setup_logging, get_logger  # noqa: E402
from iptv_engine.pipeline import Pipeline                       # noqa: E402

log = get_logger("cli")


def cmd_run(args) -> int:
    pipe = Pipeline(
        config_path=args.config,
        registry_path=args.registry,
        log_level=args.log_level,
    )
    summary = pipe.run(
        skip_discovery=args.skip_discovery,
        skip_validation=args.skip_validation,
        skip_publish=args.skip_publish,
        max_check=args.max_check,
    )
    return 0


def cmd_discover(args) -> int:
    """只做发现，打印候选统计，用于调参。"""
    setup_logging(args.log_level)
    cfg = load_config(args.config)
    from iptv_engine.discovery import DiscoveryDispatcher

    disp = DiscoveryDispatcher(cfg)
    try:
        res = disp.run()
    finally:
        disp.close()

    log.info("候选样本（前 20 条）：")
    for c in res.candidates[:20]:
        log.info("  [%s] %s  ← %s", c.group, c.name, c.url)
    log.info("统计：%s", json.dumps(res.stats, ensure_ascii=False))
    if args.export:
        Path(args.export).write_text(
            json.dumps([c.to_dict() for c in res.candidates], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        log.info("已导出 %d 条候选 → %s", len(res.candidates), args.export)
    return 0


def cmd_validate(args) -> int:
    pipe = Pipeline(config_path=args.config, registry_path=args.registry,
                    log_level=args.log_level)
    return cmd_run_argparse(args, pipe, discovery=False)


def cmd_render(args) -> int:
    from iptv_engine.registry import SourceRegistry
    from iptv_engine.renderer import render_all

    setup_logging(args.log_level)
    cfg = load_config(args.config)
    reg = SourceRegistry(args.registry)
    stats = render_all(reg, cfg, ROOT_DIR / "output", extra_stats=reg.compute_stats())
    log.info("渲染完成：%s", json.dumps(stats, ensure_ascii=False))
    return 0


def cmd_publish(args) -> int:
    setup_logging(args.log_level)
    cfg = load_config(args.config)
    from iptv_engine.publish import build_publisher, collect_output_files

    pub = build_publisher(cfg)
    if pub is None:
        log.error("未配置 GitHub 发布（需设置 GITHUB_REPOSITORY 与 GITHUB_TOKEN）")
        return 1
    files = collect_output_files(ROOT_DIR / "output", Path(args.registry))
    ok = pub.publish(files)
    for fn, u in pub.raw_urls(["live.m3u", "live.txt"]).items():
        log.info("订阅地址 [%s]: %s", fn, u)
    return 0 if ok else 1


def cmd_stats(args) -> int:
    from iptv_engine.registry import SourceRegistry

    setup_logging(args.log_level)
    reg = SourceRegistry(args.registry)
    stats = reg.compute_stats()
    print(json.dumps(stats, ensure_ascii=False, indent=2))

    actives = sorted(reg.active_entries(), key=lambda e: -e.score)[:20]
    print("\nTOP 20 活跃频道（按评分）:")
    for e in actives:
        print(f"  {e.score:6.1f}  {e.width}x{e.height:<5} {e.bitrate_kbps:>6}kbps  "
              f"[{e.group}] {e.name}  <- {e.url[:70]}")
    return 0


def cmd_selftest(args) -> int:
    """离线冒烟自检：验证配置守卫 + 抽取 + 归一化 + M3U 渲染。"""
    from tests.selftest import run_selftest

    setup_logging(args.log_level)
    return 0 if run_selftest() else 1


def cmd_netcheck(args) -> int:
    """网络通路体检：判断本机能跑哪些发现功能 + ffmpeg 是否就绪。"""
    import subprocess

    script = Path(__file__).resolve().parent / "tests" / "network_check.py"
    # 用子进程跑，避免测试模块污染主进程状态
    proc = subprocess.run([sys.executable, "-B", str(script)])
    return proc.returncode


def cmd_ui(args) -> int:
    """启动本地 Web 控制台（浏览器访问 127.0.0.1）。"""
    from iptv_engine.ui import serve

    serve(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        registry_path=args.registry,
    )
    return 0


def cmd_demo(args) -> int:
    """本地演示模式：用 ffmpeg 生成真实可播的流并渲染播放列表。"""
    import subprocess

    script = Path(__file__).resolve().parent / "tests" / "demo_local.py"
    cmd = [sys.executable, "-B", str(script)]
    if args.serve:
        cmd.append("--serve")
    proc = subprocess.run(cmd)
    return proc.returncode


def cmd_l5test(args) -> int:
    """L5 真实解码验证专项测试（需 ffmpeg）。"""
    from tests.l5_decode_test import run

    return 0 if run() else 1


def cmd_test(args) -> int:
    """
    一键跑完本地测试套件（不需要网络的部分）。

    顺序：离线自检 → 端到端演练 → 发布链路 → 网络体检
    网络体检标为可选，可用 --offline 跳过。
    """
    import subprocess

    root = Path(__file__).resolve().parent
    tests_dir = root / "tests"

    suites = [
        ("离线自检", tests_dir / "selftest.py"),
        ("端到端演练", tests_dir / "e2e_test.py"),
        ("发布链路", tests_dir / "publish_test.py"),
        ("台标链路", tests_dir / "logo_pipeline_test.py"),
        ("L5 解码验证", tests_dir / "l5_decode_test.py"),
        ("UI 后端", tests_dir / "ui_test.py"),
    ]
    if not args.offline:
        suites.append(("网络体检", tests_dir / "network_check.py"))

    print("=" * 72)
    print("  本地测试套件")
    print("=" * 72)

    passed, failed = [], []
    for label, script in suites:
        print(f"\n>>> [{label}] {script.name}")
        print("-" * 72)
        sys.stdout.flush()
        proc = subprocess.run([sys.executable, "-B", str(script)])
        sys.stdout.flush()
        if proc.returncode == 0:
            passed.append(label)
        else:
            failed.append(label)

    print()
    print("=" * 72)
    print(f"  测试结果：{len(passed)} 通过 / {len(failed)} 失败")
    for label in passed:
        print(f"    \033[92m✓\033[0m {label}")
    for label in failed:
        print(f"    \033[91m✗\033[0m {label}")
    print("=" * 72)

    if failed:
        return 1
    print("\n下一步：python main.py run --max-check 30 --skip-publish --registry /tmp/test.json")
    return 0


def cmd_run_argparse(args, pipe, discovery: bool) -> int:
    summary = pipe.run(
        skip_discovery=not discovery,
        skip_validation=args.skip_validation,
        skip_publish=args.skip_publish,
        max_check=args.max_check,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="iptv-engine",
        description="具备自主发现能力的 IPTV 直播源整合引擎",
    )
    p.add_argument("--config", type=Path, default=None, help="配置文件路径")
    p.add_argument("--registry", type=Path, default=ROOT_DIR / "sources" / "registry.json",
                   help="注册表路径")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp, validation=True, discovery=False):
        sp.add_argument("--max-check", type=int, default=0,
                        help="本轮最多验证多少条（0=用配置默认值）")
        sp.add_argument("--skip-validation", action="store_true")
        sp.add_argument("--skip-publish", action="store_true")
        if discovery:
            sp.add_argument("--skip-discovery", action="store_true")

    sp = sub.add_parser("run", help="完整运行一轮")
    add_common(sp, discovery=True)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("discover", help="只做发现")
    sp.add_argument("--export", type=str, default=None, help="把候选导出为 JSON")
    sp.set_defaults(func=cmd_discover)

    sp = sub.add_parser("validate", help="只做验证（巡检已有源）")
    add_common(sp)
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("render", help="只渲染输出文件")
    sp.set_defaults(func=cmd_render)

    sp = sub.add_parser("publish", help="只推送本地产物到 GitHub")
    sp.set_defaults(func=cmd_publish)

    sp = sub.add_parser("stats", help="查看注册表统计")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("selftest", help="离线冒烟自检")
    sp.set_defaults(func=cmd_selftest)

    sp = sub.add_parser("netcheck", help="网络通路体检（判断本机能跑哪些功能）")
    sp.set_defaults(func=cmd_netcheck)

    sp = sub.add_parser("l5test", help="L5 真实解码验证专项测试（需 ffmpeg）")
    sp.set_defaults(func=cmd_l5test)

    sp = sub.add_parser("demo", help="本地演示：生成真实可播的流与播放列表（需 ffmpeg）")
    sp.add_argument("--serve", action="store_true", help="生成后常驻服务器供本机播放")
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("ui", help="启动本地 Web 控制台（浏览器操作）")
    sp.add_argument("--host", default="127.0.0.1",
                    help="监听地址（默认仅本机，勿改为 0.0.0.0）")
    sp.add_argument("--port", type=int, default=8765, help="端口（被占用会自动 +1）")
    sp.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    sp.set_defaults(func=cmd_ui)

    sp = sub.add_parser("test", help="一键跑完本地测试套件")
    sp.add_argument("--offline", action="store_true", help="跳过网络体检（离线环境用）")
    sp.set_defaults(func=cmd_test)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        log.warning("已被用户中断")
        return 130
    except Exception as exc:  # noqa: BLE001
        log.exception("执行失败: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())