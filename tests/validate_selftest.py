"""
验证器环境自检。

背景：CI 上曾出现 4000 条源全部卡在 L1、reason 显示
「连接失败: AttributeError」。但这个信息只记了异常类名，
无法定位是 requests / urllib3 / truststore / 我们的代码哪一层出错，
导致要跑 2 小时才知道失败原因。

本模块在跑主流程**之前**先做一次端到端探针：
用真实的 SessionFactory 发一个请求，把完整异常堆栈打出来。
这样问题会在 5 秒内暴露，而不是 2 小时后。

用法（已挂在 pipeline 与 selftest 里）：
    python main.py validate-selftest
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from iptv_engine.config import load_config
from iptv_engine.extractor import Candidate
from iptv_engine.logging_utils import build_session_factory, setup_logging
from iptv_engine.validate import stream_validator as SV

# 探针目标：选几个必定可访问且轻量的站点
PROBES = [
    ("HTTP 明文", "http://example.com"),
    ("HTTPS 受信CA", "https://example.com"),
    ("DNS 解析", "https://api.github.com/rate_limit"),
]


def probe(verbose: bool = True) -> bool:
    """逐项探测，返回是否全部正常。"""
    setup_logging("INFO" if verbose else "WARNING")
    log = SV.log

    cfg = load_config()
    cfg.setdefault("validation", {})["allow_private_hosts"] = True
    sf = build_session_factory(cfg, pool_size=2, retries=0, qps=5)
    v = SV.StreamValidator(cfg, sf)

    print("=" * 72)
    print("  验证器环境自检")
    print("=" * 72)
    print(f"  ffmpeg  : {SV.FFMPEG_BIN or '缺失'}")
    print(f"  ffprobe : {SV.FFPROBE_BIN or '缺失'}")
    print(f"  解码验证: {'启用' if cfg['validation'].get('require_decode') else '关闭'}")
    print(f"  CA 校验 : {sf.verify}")
    print(f"  绕过代理: {sf.bypass_env_proxy}")
    print()

    ok_all = True
    for label, url in PROBES:
        try:
            res, _ = v._l1_l2_http(url, {"User-Agent": "Mozilla/5.0"})
            # 走到 L2 说明网络+TLS 都通了（404/403 也算连通）
            good = res.stage != "L1" or not res.reason.startswith("连接失败")
            mark = "\033[92m通过\033[0m" if good else "\033[91m失败\033[0m"
            print(f"  {mark}  {label:14s} stage={res.stage:4s} {res.reason[:40]}")
            ok_all = ok_all and good
        except Exception as exc:  # noqa: BLE001
            ok_all = False
            print(f"  \033[91m异常\033[0m  {label:14s} {type(exc).__name__}: {exc}")
            if verbose:
                traceback.print_exc()

    # 若失败，把第一项的完整堆栈打出来，这是排障的关键信息
    if not ok_all:
        print()
        print("  \033[93m自检未通过 —— 下面是完整异常堆栈，用于定位问题：\033[0m")
        for label, url in PROBES:
            try:
                v._l1_l2_http(url, {"User-Agent": "Mozilla/5.0"})
            except Exception:  # noqa: BLE001
                print(f"\n  --- {label} ({url}) ---")
                traceback.print_exc()
                break
        else:
            # 没有抛异常但仍失败：说明是 requests 内部异常被吞了
            print("  未捕获到异常，说明异常发生在 requests 内部。")
            print("  常见原因：")
            print("   1. truststore 注入后与 urllib3 版本不兼容")
            print("   2. 代理返回 502，requests 内部包装后触发异常")
            print("   3. 证书链有问题且 verify 设置异常")
            print("\n  逐项关闭特性定位：")
            import os
            for env, desc in (("IPTV_NO_SYSTEM_CA", "禁用系统CA(truststore)"),
                              ("HTTP_PROXY", "清空代理")):
                keys = ("IPTV_NO_SYSTEM_CA",) if env == "IPTV_NO_SYSTEM_CA" else (
                    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
                saved = {k: os.environ.get(k) for k in keys}
                if env == "IPTV_NO_SYSTEM_CA":
                    os.environ[env] = "1"
                else:
                    for k in keys:
                        os.environ.pop(k, None)
                try:
                    sf2 = build_session_factory(cfg, pool_size=2, retries=0, qps=5)
                    v2 = SV.StreamValidator(cfg, sf2)
                    r2, _ = v2._l1_l2_http(url, {"User-Agent": "Mozilla/5.0"})
                    good2 = not r2.reason.startswith("连接失败")
                    print(f"    {desc:26s} -> {'恢复' if good2 else '仍失败'}"
                          f"  stage={r2.stage} {r2.reason[:30]}")
                except Exception as e2:  # noqa: BLE001
                    print(f"    {desc:26s} -> 异常 {type(e2).__name__}: "
                          f"{str(e2)[:60]}")
                finally:
                    for k, val in saved.items():
                        if val is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = val

    print()
    if ok_all:
        print("\033[92m  验证器环境自检通过，可以正常跑验证流程。\033[0m")
    else:
        print("\033[91m  验证器环境自检未通过，验证阶段会全军覆没。\033[0m")
        print("  请把上面的异常堆栈发出来，据此定位。")
    print("=" * 72)
    return ok_all


def main() -> int:
    return 0 if probe(verbose=True) else 1


if __name__ == "__main__":
    sys.exit(main())