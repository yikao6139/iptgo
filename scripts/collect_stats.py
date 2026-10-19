#!/usr/bin/env python3
"""CI 辅助脚本：统计本轮验证结果，用于 Actions 日志与摘要。"""
import collections
import json
import pathlib
import sys

p = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "data/last_validation.json")
if not p.exists():
    print("无验证数据文件")
    raise SystemExit(0)

try:
    data = json.loads(p.read_text(encoding="utf-8"))
except Exception as exc:  # noqa: BLE001
    print("验证数据解析失败:", exc)
    raise SystemExit(0)

ok = sum(1 for r in data if r.get("ok"))
fail = sum(1 for r in data if not r.get("ok"))
stages = collections.Counter(r.get("stage", "?") for r in data if not r.get("ok"))

print(f"验证总数: {len(data)}  通过: {ok}  失败: {fail}")
if ok + fail:
    print(f"通过率: {100.0 * ok / max(1, ok + fail):.1f}%")
print("失败分层(停留阶段):", dict(stages.most_common()))

# 失败原因 Top5
reasons = collections.Counter(
    r.get("reason", "") for r in data if not r.get("ok") and r.get("reason")
)
if reasons:
    print("\n失败原因 Top5:")
    for reason, n in reasons.most_common(5):
        print(f"  {n:5d}  {reason[:70]}")

# 质量分布
passed = [r for r in data if r.get("ok")]
if passed:
    scores = [r.get("score", 0) for r in passed]
    res = [f"{r.get('width',0)}x{r.get('height',0)}" for r in passed]
    brs = [r.get("bitrate_kbps", 0) for r in passed]
    print("\n通过源的画质分布:")
    for k, v in collections.Counter(res).most_common(8):
        print(f"  {v:5d}  {k}")
    print(f"平均评分: {sum(scores)/len(scores):.1f}   "
          f"平均码率: {sum(brs)//max(1,len(brs))} kbps")