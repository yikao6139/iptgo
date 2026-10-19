"""台标链路验证：上游 tvg-logo 解析 → 注册表保存 → 渲染输出。"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from iptv_engine.extractor import parse_m3u
from iptv_engine.registry import RegistryEntry, SourceRegistry, STATUS_ACTIVE
from iptv_engine.renderer import render_m3u

UPSTREAM = '''#EXTM3U
#EXTINF:-1 tvg-id="cctv1" tvg-name="CCTV1" tvg-logo="https://cdn.tvlogo.top/logo/cctv1.png" group-title="央视",CCTV-1 综合
http://cdn.tvlogo.top/live/cctv1.m3u8
#EXTINF:-1 tvg-id="cctv13" tvg-logo="https://cdn.tvlogo.top/logo/cctv13.png" group-title="新闻",CCTV-13 新闻
http://cdn.tvlogo.top/live/cctv13.m3u8
#EXTINF:-1 group-title="卫视",湖南卫视
http://cdn.tvlogo.top/live/hunan.m3u8
'''

print("=== 1. 上游 m3u 的 logo 解析 ===")
c = parse_m3u(UPSTREAM)
print(f"解析 {len(c)} 条")
for x in c:
    print(f"  {x.name:14s} tvg-id={x.tvg_id or '(无)':10s} logo={x.logo or '(无)'}")

with_logo = [x for x in c if x.logo]
print(f"\n带 logo: {len(with_logo)}/{len(c)} 条")
assert len(c) == 3, f"应解析 3 条，实际 {len(c)}"
assert len(with_logo) == 2, f"应解析出 2 个 logo，实际 {len(with_logo)}"

print("\n=== 2. 注册表保存与回读 ===")
tmp = Path(tempfile.mkdtemp())
reg = SourceRegistry(tmp / "registry.json")
reg.upsert_candidates(c)
entries = []
for x in c:
    e = reg.get_entries_for_test(x.url) if hasattr(reg, "get_entries_for_test") else None
    entries.append(x)
reg.save()
reg2 = SourceRegistry(tmp / "registry.json")
saved_logo = [s for s in reg2.all_entries() if s.logo]
print(f"回读 {len(reg2.all_entries())} 条，其中带 logo {len(saved_logo)} 条")
for s in saved_logo:
    print(f"  {s.name:14s} {s.logo}")
assert len(saved_logo) == 2, "注册表未正确保存 logo"

print("\n=== 3. 渲染输出 ===")
ents = [RegistryEntry(url=x.url, name=x.name, logo=x.logo, tvg_id=x.tvg_id,
                      group=x.group, status=STATUS_ACTIVE, score=90.0)
        for x in c]
out = tmp / "t.m3u"
n = render_m3u(ents, out)
text = out.read_text(encoding="utf-8")
print(f"渲染 {n} 个频道")
for line in text.splitlines():
    if line.startswith("#EXTINF"):
        print("  " + line[:150])

logo_lines = [l for l in text.splitlines() if "tvg-logo=" in l]
print(f"\n含 tvg-logo 的行: {len(logo_lines)}")
assert len(logo_lines) == 2, f"应输出 2 个 tvg-logo，实际 {len(logo_lines)}"
assert "https://cdn.tvlogo.top/logo/cctv1.png" in text, "logo URL 未正确输出"
# 无 logo 的频道不应输出空属性
assert 'tvg-logo=""' not in text, "无 logo 时不应输出空属性"
# 无 tvg-id 且无 key 时不应输出 tvg-id=""（空属性会让部分播放器显示异常）
assert 'tvg-id=""' not in text, "无 tvg-id 时不应输出空属性"

print("\n✓ 台标链路完整验证通过")
print("  上游解析 2/2 → 注册表保存 2/2 → 渲染输出 2/2")