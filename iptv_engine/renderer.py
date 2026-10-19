"""
输出渲染器：把注册表里 ACTIVE 的源渲染成播放器可直接订阅的标准文件。

产出：
  output/live.m3u   标准 M3U，EXTINF 带 tvg-id/tvg-name/tvg-logo/group-title，
                    播放器（VLC/TiviMate/APTV/DIYP）通用
  output/live.txt   分组纯文本，DIYP TV 等国产播放器友好
  output/meta.json  本次快照元信息（数量、分组统计、更新时间）

排序策略：group（按配置的分组优先级）-> score（降序）-> name
这样每天自动把高质量源顶到前面，播放器一拉就是最好的。
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

from .logging_utils import get_logger
from .registry import STATUS_ACTIVE, RegistryEntry

log = get_logger("renderer")

DEFAULT_GROUP_ORDER = ["央视", "卫视", "卫视台", "地方", "港澳台", "国际",
                       "体育", "影视", "少儿", "新闻", "其他"]


def _group_sort_key(group: str, priority: List[str]) -> int:
    g = (group or "其他").strip()
    for i, name in enumerate(priority):
        if g == name or (name in g):
            return i
    return len(priority)


def _sanitize_attr(v: str) -> str:
    """清洗 M3U 属性值：不能含双引号/逗号/换行。"""
    return (v or "").replace('"', "'").replace(",", " ").replace("\r", " ").replace("\n", " ").strip()


def _quality_tag(e: RegistryEntry) -> str:
    """给频道名加质量角标，帮助用户识别高清源。"""
    if e.height >= 2160:
        return "[4K]"
    if e.height >= 1080:
        return "[1080P]"
    if e.height >= 720:
        return "[720P]"
    if e.height >= 480:
        return "[480P]"
    return ""


def _display_name(e: RegistryEntry, with_tag: bool = True) -> str:
    name = (e.name or "").strip() or "未知频道"
    if with_tag:
        tag = _quality_tag(e)
        if tag and tag not in name:
            name = f"{name} {tag}"
    return name


def collect_active(registry, cfg: dict) -> List[RegistryEntry]:
    """取活跃源并按分组优先级 + 评分排序。"""
    out_cfg = cfg.get("output", {}) or {}
    priority = out_cfg.get("group_priority") or DEFAULT_GROUP_ORDER
    sort_by = out_cfg.get("sort_by", "score")

    actives = [e for e in registry.all_entries() if e.status == STATUS_ACTIVE and e.url]

    def key(e: RegistryEntry) -> tuple:
        g_rank = _group_sort_key(e.group, priority)
        if sort_by == "score":
            return (g_rank, -e.score, -e.bitrate_kbps, (e.name or ""))
        if sort_by == "name":
            return (g_rank, (e.name or "").lower())
        return (g_rank, -e.last_ok_at or "")

    actives.sort(key=key)
    return actives


def render_m3u(entries: List[RegistryEntry], out_path: Path) -> int:
    """生成标准 M3U。"""
    lines = ["#EXTM3U"]
    written = 0
    seen_names: set = set()

    for e in entries:
        name = _display_name(e)
        # 同名频道加序号，避免播放器里一堆" CCTV1"
        final_name = name
        if final_name in seen_names:
            i = 2
            while f"{name} {i}" in seen_names:
                i += 1
            final_name = f"{name} {i}"
        seen_names.add(final_name)

        attrs = []
        # tvg-id 缺失时直接省略属性，不要输出 tvg-id=""——
        # 空属性会让部分播放器显示异常（空白条目或图标错位）。
        if e.tvg_id or e.key:
            tid = e.tvg_id or e.key[:12]
            if tid:
                attrs.append(f'tvg-id="{_sanitize_attr(tid)}"')
        attrs.append(f'tvg-name="{_sanitize_attr(final_name)}"')
        if e.logo:
            attrs.append(f'tvg-logo="{_sanitize_attr(e.logo)}"')
        group = _sanitize_attr(e.group or "其他")
        attrs.append(f'group-title="{group}"')
        lines.append(f'#EXTINF:-1 {" ".join(attrs)},{final_name}')

        # 部分源必须带 Referer/UA 才能播，用 EXTVLCOPT 传递
        if e.referer:
            lines.append(f'#EXTVLCOPT:http-referrer={e.referer}')
        lines.append(e.url)
        written += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("已生成 %s：%d 个频道", out_path.name, written)
    return written


def render_txt(entries: List[RegistryEntry], out_path: Path) -> int:
    """生成分组纯文本（DIYP 等播放器直接支持「分组,名称,地址」格式）。"""
    groups: Dict[str, List[RegistryEntry]] = defaultdict(list)
    for e in entries:
        groups[e.group or "其他"].append(e)

    lines: List[str] = []
    written = 0
    for gname, items in groups.items():
        lines.append(f"{gname},#genre#")
        for e in items:
            lines.append(f"{gname},{_display_name(e, with_tag=False)},{e.url}")
            written += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("已生成 %s：%d 个频道，%d 个分组", out_path.name, written, len(groups))
    return written


def render_meta(entries: List[RegistryEntry], out_path: Path, cfg: dict,
                extra_stats: Dict | None = None) -> None:
    """生成快照元信息。"""
    by_group: Dict[str, int] = defaultdict(int)
    by_quality: Dict[str, int] = defaultdict(int)
    total_kbps = 0
    for e in entries:
        by_group[e.group or "其他"] += 1
        if e.height >= 2160: by_quality["4K"] += 1
        elif e.height >= 1080: by_quality["1080P"] += 1
        elif e.height >= 720: by_quality["720P"] += 1
        elif e.height >= 480: by_quality["480P"] += 1
        else: by_quality["SD/未知"] += 1
        total_kbps += e.bitrate_kbps

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_active": len(entries),
        "by_group": dict(sorted(by_group.items(), key=lambda kv: -kv[1])),
        "by_quality": dict(by_quality),
        "avg_bitrate_kbps": round(total_kbps / len(entries)) if entries else 0,
        "registry_stats": extra_stats or {},
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("已生成 %s", out_path.name)


def render_all(registry, cfg: dict, out_dir: Path, extra_stats: Dict | None = None) -> Dict[str, int]:
    """一次性渲染全部输出文件。"""
    ocfg = cfg.get("output", {}) or {}
    entries = collect_active(registry, cfg)

    m3u_path = out_dir / (ocfg.get("m3u_name") or "live.m3u")
    txt_path = out_dir / (ocfg.get("txt_name") or "live.txt")

    n_m3u = render_m3u(entries, m3u_path)
    n_txt = render_txt(entries, txt_path)
    render_meta(entries, out_dir / "meta.json", cfg, extra_stats)

    return {"m3u": n_m3u, "txt": n_txt, "total": len(entries)}