"""
全流程演练：用模拟数据走完"发现→验证→注册表→淘汰→渲染→发布"的完整链路。
目的：验证模块间契约正确（不依赖外网）。
"""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from iptv_engine.config import load_config
from iptv_engine.extractor import Candidate, parse_m3u
from iptv_engine.normalizer import dedup_candidates
from iptv_engine.registry import SourceRegistry
from iptv_engine.renderer import render_all, collect_active
from iptv_engine.pipeline import _ProbeShim

cfg = load_config()
tmp = Path(tempfile.mkdtemp(prefix="iptv_e2e_"))
reg_path = tmp / "sources" / "registry.json"
out_dir = tmp / "output"

# ---- 1. 模拟"发现"：一段真实结构的 m3u（含重复与别名）----
RAW_M3U = """#EXTM3U
#EXTINF:-1 tvg-id="cctv1" tvg-logo="http://x/1.png" group-title="央视",CCTV-1 综合
http://cdn.a.com/live/cctv1.m3u8
#EXTINF:-1 group-title="央视",CCTV-1 综合
http://cdn.a.com/live/cctv1.m3u8?utm_source=x
#EXTINF:-1 group-title="卫视",湖南卫视 HD
http://cdn.a.com/live/hunan.m3u8
#EXTINF:-1 group-title="体育",CCTV5+
https://cdn.b.com/hls/cctv5/index.m3u8
#EXTINF:-1 group-title="少儿",CCTV少儿
http://cdn.a.com/live/cctvkids.m3u8
#EXTINF:-1 group-title="国际",BBC News
http://cdn.c.com/live/bbc.m3u8
"""
cands = parse_m3u(RAW_M3U, found_via="github:test/repo", source_ref="https://raw.githubusercontent.com/x/y/main/live.m3u")
print(f"1. 解析出候选: {len(cands)} 条")
for c in cands:
    print(f"     [{c.group:4s}] {c.name:12s} {c.url}")

ded = dedup_candidates(cands)
print(f"2. 双重去重后: {len(ded)} 条 (剔除 {len(cands)-len(ded)} 条重复/别名)")

# ---- 2. 模拟验证结果 ----
reg = SourceRegistry(reg_path)
reg.upsert_candidates(ded)

def mk(url, ok, reason="", score=80.0, w=1920, h=1080, br=3000):
    return _ProbeShim({"url":url,"ok":ok,"reason":reason,"stage":"L5" if ok else "L4",
                       "score":score,"width":w,"height":h,"bitrate_kbps":br,
                       "manifest_kind":"hls","video_codec":"h264","audio_codec":"aac"})

names = {c.url: c.name for c in ded}
groups = {c.url: c.group for c in ded}
logos = {c.url: c.logo for c in ded}

ok_urls = list(names)[:3]          # 前 3 条通过
for u in names:
    if u in ok_urls:
        high = "cctv1" in u       # 央视这条给高分高清
        reg.record_success(mk(u, True,
                              score=95.0 if high else 75.0,
                              w=1920 if high else 1280,
                              h=1080 if high else 720,
                              br=4500 if high else 1200),
                           name=names[u], group=groups[u], logo=logos[u])
    else:
        reg.record_failure(mk(u, False, "分片不可达 HTTP 404"), name=names[u])
print(f"3. 模拟验证: 通过 {len(ok_urls)} 失败 {len(names)-len(ok_urls)}")

# ---- 3. 淘汰：让失败的源连续失败到阈值 ----
evict_cfg = {**cfg, "validation": {**(cfg.get("validation") or {}), "max_consecutive_failures": 2,
                                    "newbie_grace_checks": 0},
             "lifecycle": {"max_idle_days": 0, "keep_tombstone": True}}
for _ in range(3):
    for u in names:
        if u not in ok_urls:
            reg.record_failure(mk(u, False, "超时"), name=names[u])
stats = reg.evict(evict_cfg)
print(f"4. 淘汰统计: {stats}")
print(f"   注册表状态: {reg.compute_stats()}")

# ---- 4. 渲染 ----
render_stats = render_all(reg, cfg, out_dir, extra_stats=reg.compute_stats())
print(f"5. 渲染: {render_stats}")

m3u = (out_dir / "live.m3u").read_text(encoding="utf-8")
txt = (out_dir / "live.txt").read_text(encoding="utf-8")
meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))

print("\n----- live.m3u 前 14 行 -----")
for line in m3u.splitlines()[:14]:
    print("  " + line)
print("\n----- live.txt 前 10 行 -----")
for line in txt.splitlines()[:10]:
    print("  " + line)
print("\n----- meta.json -----")
print(json.dumps(meta, ensure_ascii=False, indent=2)[:700])

# ---- 5. 断言 ----
assert m3u.startswith("#EXTM3U"), "m3u 首行错误"
n_ext = m3u.count("#EXTINF")
assert n_ext == render_stats["m3u"], f"EXTINF 数不匹配 {n_ext} vs {render_stats['m3u']}"
assert reg.compute_stats()["active"] == render_stats["m3u"], "活跃数与渲染数不符"
assert "#EXTVLCOPT" not in m3u or "http-referrer" in m3u, "EXTVLCOPT 格式异常"
print(f"\n✓ 全流程演练通过：{render_stats['m3u']} 个活跃频道进入输出，"
      f"{reg.compute_stats()['retired']} 个已淘汰")
print(f"  产物目录: {out_dir}")