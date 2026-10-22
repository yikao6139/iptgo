"""
动态注册表：sources/registry.json 的唯一读写入口。

【核心设计】这张表完全由程序自动维护，禁止人工编辑。
每条记录的生命周期：

    新发现 -> 首次验证
      ├─ 通过 -> ACTIVE（进输出 m3u/txt）
      │          ├─ 后续失败 -> DEGRADED -> 连续失败 N 次 -> RETIRED（出输出，留墓碑）
      │          └─ 重新成功 -> ACTIVE（失败计数清零）
      │
      └─ 失败 -> PENDING（记失败次数）
                   └─ 连续失败 N 次 -> RETIRED

字段说明：
  - consecutive_failures  连续失败次数，核心淘汰依据
  - last_ok_at            最后一次成功时间，用于 max_idle_days 淘汰
  - score                 由验证器给出的质量分，用于输出排序
  - tombstone             已淘汰标记，保留以便"复活检测"（同 URL 重新可用时直接恢复）
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

from .extractor import Candidate
from .logging_utils import get_logger
from .normalizer import dedup_key, normalize_name, normalize_url

log = get_logger("registry")

STATUS_ACTIVE = "active"
STATUS_PENDING = "pending"
STATUS_DEGRADED = "degraded"
STATUS_RETIRED = "retired"


@dataclass
class RegistryEntry:
    """注册表中的一条记录。"""

    url: str
    key: str = ""                       # dedup_key
    name: str = ""
    name_norm: str = ""
    group: str = ""
    logo: str = ""
    tvg_id: str = ""
    status: str = STATUS_PENDING

    found_at: str = ""
    last_seen_at: str = ""               # 最后一次被任何通路发现
    last_checked_at: str = ""
    last_ok_at: str = ""

    consecutive_failures: int = 0
    total_checks: int = 0
    total_successes: int = 0

    score: float = 0.0
    width: int = 0
    height: int = 0
    bitrate_kbps: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    manifest_kind: str = ""

    found_via: str = ""
    source_ref: str = ""
    referer: str = ""

    last_error: str = ""
    last_stage: str = ""
    tombstone: bool = False

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "key": self.key,
            "name": self.name,
            "name_norm": self.name_norm,
            "group": self.group,
            "logo": self.logo,
            "tvg_id": self.tvg_id,
            "status": self.status,
            "found_at": self.found_at,
            "last_seen_at": self.last_seen_at,
            "last_checked_at": self.last_checked_at,
            "last_ok_at": self.last_ok_at,
            "consecutive_failures": self.consecutive_failures,
            "total_checks": self.total_checks,
            "total_successes": self.total_successes,
            "score": self.score,
            "width": self.width,
            "height": self.height,
            "bitrate_kbps": self.bitrate_kbps,
            "fps": self.fps,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "manifest_kind": self.manifest_kind,
            "found_via": self.found_via,
            "source_ref": self.source_ref,
            "referer": self.referer,
            "last_error": self.last_error,
            "last_stage": self.last_stage,
            "tombstone": self.tombstone,
        }

    @staticmethod
    def from_dict(d: dict) -> "RegistryEntry":
        return RegistryEntry(
            url=d.get("url", ""),
            key=d.get("key", ""),
            name=d.get("name", ""),
            name_norm=d.get("name_norm", ""),
            group=d.get("group", ""),
            logo=d.get("logo", ""),
            tvg_id=d.get("tvg_id", ""),
            status=d.get("status", STATUS_PENDING),
            found_at=d.get("found_at", ""),
            last_seen_at=d.get("last_seen_at", ""),
            last_checked_at=d.get("last_checked_at", ""),
            last_ok_at=d.get("last_ok_at", ""),
            consecutive_failures=int(d.get("consecutive_failures", 0) or 0),
            total_checks=int(d.get("total_checks", 0) or 0),
            total_successes=int(d.get("total_successes", 0) or 0),
            score=float(d.get("score", 0) or 0),
            width=int(d.get("width", 0) or 0),
            height=int(d.get("height", 0) or 0),
            bitrate_kbps=int(d.get("bitrate_kbps", 0) or 0),
            fps=float(d.get("fps", 0) or 0),
            video_codec=d.get("video_codec", ""),
            audio_codec=d.get("audio_codec", ""),
            manifest_kind=d.get("manifest_kind", ""),
            found_via=d.get("found_via", ""),
            source_ref=d.get("source_ref", ""),
            referer=d.get("referer", ""),
            last_error=d.get("last_error", ""),
            last_stage=d.get("last_stage", ""),
            tombstone=bool(d.get("tombstone", False)),
        )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _days_since(ts: str) -> float:
    if not ts:
        return 1e9
    try:
        t = time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S"))
        return (time.time() - t) / 86400.0
    except ValueError:
        return 1e9


def _host_of(url: str) -> str:
    """
    取 URL 的 host。

    注意：这里**不要**用 try/except 把一切异常都吞掉返回空串 ——
    那样一旦漏了导入（如 NameError）或写错参数，会静默返回空串，
    让域名均衡悄悄失效，表现为"配了却没效果"，极难排查。
    这里只对真正的 URL 解析失败兜底。
    """
    if not url:
        return ""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        # ValueError: 畸形 URL（如端口非法），是数据问题不是代码问题
        return ""


def _rebalance_by_host(queue: List[Candidate], cap: int) -> List[Candidate]:
    """
    域名均衡：每个域名最多保留 cap 条，超出部分按原顺序顺延到队尾。

    保持原有优先级顺序不变（新源仍在降级源之前），只是把
    同一域名的超额部分挪到后面，让前面的位置让给别的域名。

    为什么需要：抽取阶段常从同一个 m3u 文件里抽出一批同域名的条目
    （实测 100 条验证配额里 98 条来自同一个域名且全部不可达），
    结果"看起来挖了很多源"，实际只覆盖了一个站点。
    """
    kept: List[Candidate] = []
    overflow: List[Candidate] = []
    seen: Dict[str, int] = {}
    for c in queue:
        h = _host_of(c.url) or "(unknown)"
        n = seen.get(h, 0)
        if n < cap:
            seen[h] = n + 1
            kept.append(c)
        else:
            overflow.append(c)
    return kept + overflow


class SourceRegistry:
    """sources/registry.json 的读写与生命周期管理。"""

    SCHEMA_VERSION = 2

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._by_key: Dict[str, RegistryEntry] = {}
        self._by_name: Dict[str, str] = {}     # name_norm -> key
        self.meta: Dict = {"schema_version": self.SCHEMA_VERSION,
                           "updated_at": "", "stats": {}}
        self.load()

    # ------------------------------------------------------------ 持久化
    def load(self) -> None:
        if not self.path.exists():
            log.info("注册表不存在，将新建: %s", self.path)
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            backup = self.path.with_suffix(".corrupt.json")
            self.path.rename(backup)
            log.error("注册表损坏，已备份至 %s：%s", backup.name, exc)
            return

        self.meta = raw.get("meta", {}) or {}
        entries = raw.get("sources", []) or []
        for d in entries:
            e = RegistryEntry.from_dict(d)
            if not e.key:
                e.key = dedup_key(e.url, e.name)
            self._by_key[e.key] = e
            if e.name_norm:
                self._by_name.setdefault(e.name_norm, e.key)
        log.info("已载入注册表：%d 条记录（%d 条活跃）", len(self._by_key), len(self.active_entries()))

    def save(self) -> None:
        with self.lock:
            entries = list(self._by_key.values())
            self.meta["schema_version"] = self.SCHEMA_VERSION
            self.meta["updated_at"] = _now()
            self.meta["count"] = len(entries)
            self.meta["stats"] = self.compute_stats()
            payload = {"meta": self.meta, "sources": [e.to_dict() for e in entries]}
            # 原子写：先写临时文件再替换，避免 CI 中断产生半截 JSON
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        log.info("注册表已保存：%d 条 → %s", len(entries), self.path.name)

    # ------------------------------------------------------------ 查询
    def get(self, key: str) -> Optional[RegistryEntry]:
        return self._by_key.get(key)

    def active_entries(self) -> List[RegistryEntry]:
        return [e for e in self._by_key.values() if e.status == STATUS_ACTIVE]

    def all_entries(self) -> List[RegistryEntry]:
        return list(self._by_key.values())

    def compute_stats(self) -> Dict:
        stats = {STATUS_ACTIVE: 0, STATUS_PENDING: 0, STATUS_DEGRADED: 0, STATUS_RETIRED: 0}
        for e in self._by_key.values():
            stats[e.status] = stats.get(e.status, 0) + 1
        stats["total"] = len(self._by_key)
        return stats

    # ------------------------------------------------------------ 合并新发现
    def upsert_candidates(self, candidates: Iterable[Candidate]) -> Tuple[int, int]:
        """
        把新发现合并进注册表。
        返回 (新增数, 命中已有数)。

        已 RETIRED 的源若被再次发现，视为"复活"：清空失败计数，重新进入 PENDING 队列。
        """
        added = existing = revived = 0
        now = _now()
        with self.lock:
            for c in candidates:
                if not c.url:
                    continue
                key = dedup_key(c.url, c.name)
                entry = self._by_key.get(key)

                if entry is None:
                    entry = RegistryEntry(
                        url=c.url,
                        key=key,
                        name=c.name,
                        name_norm=normalize_name(c.name),
                        group=c.group or "其他",
                        logo=c.logo,
                        tvg_id=c.tvg_id,
                        status=STATUS_PENDING,
                        found_at=now,
                        last_seen_at=now,
                        found_via=c.found_via,
                        source_ref=c.source_ref,
                        referer=c.referer,
                    )
                    self._by_key[key] = entry
                    if entry.name_norm:
                        self._by_name.setdefault(entry.name_norm, key)
                    added += 1
                    continue

                existing += 1
                # 刷新元数据（名称/分组可能被上游改过）
                if c.name:
                    entry.name = c.name
                    entry.name_norm = normalize_name(c.name)
                    if entry.name_norm:
                        self._by_name.setdefault(entry.name_norm, key)
                if c.group:
                    entry.group = c.group
                if c.logo:
                    entry.logo = c.logo
                if c.tvg_id:
                    entry.tvg_id = c.tvg_id
                if c.referer and not entry.referer:
                    entry.referer = c.referer
                entry.last_seen_at = now

                # 复活检测
                if entry.status == STATUS_RETIRED:
                    entry.status = STATUS_PENDING
                    entry.consecutive_failures = 0
                    entry.tombstone = False
                    revived += 1

        if added or existing or revived:
            log.info("注册表合并：新增 %d，命中已有 %d（其中复活 %d），当前总量 %d",
                     added, existing, revived, len(self._by_key))
        return added, existing

    # ------------------------------------------------------------ 记录验证结果
    def record_success(self, probe_result, name: str = "", group: str = "",
                       logo: str = "", tvg_id: str = "", referer: str = "") -> Optional[RegistryEntry]:
        """标记一次成功验证。"""
        key = dedup_key(probe_result.url, name or probe_result.url)
        now = _now()
        with self.lock:
            entry = self._by_key.get(key)
            if entry is None:
                # 兜底：源由其它途径进来的
                entry = RegistryEntry(
                    url=probe_result.url, key=key, name=name or "",
                    name_norm=normalize_name(name or ""),
                    found_at=now,
                )
                self._by_key[key] = entry

            entry.name = name or entry.name
            entry.name_norm = entry.name_norm or normalize_name(entry.name)
            entry.group = group or entry.group or "其他"
            entry.logo = logo or entry.logo
            entry.tvg_id = tvg_id or entry.tvg_id
            entry.referer = referer or entry.referer

            entry.status = STATUS_ACTIVE
            entry.consecutive_failures = 0
            entry.total_checks += 1
            entry.total_successes += 1
            entry.last_checked_at = now
            entry.last_ok_at = now
            entry.last_seen_at = now
            entry.tombstone = False

            # 写回质量指标
            entry.score = probe_result.score
            entry.width = probe_result.width
            entry.height = probe_result.height
            entry.bitrate_kbps = probe_result.bitrate_kbps
            entry.fps = probe_result.fps
            entry.video_codec = probe_result.video_codec
            entry.audio_codec = probe_result.audio_codec
            entry.manifest_kind = probe_result.manifest_kind
            entry.last_error = ""
            entry.last_stage = "L5"

            if entry.name_norm:
                self._by_name.setdefault(entry.name_norm, key)
            return entry

    def record_failure(self, probe_result, name: str = "") -> Optional[RegistryEntry]:
        """标记一次失败验证，累计连续失败次数。"""
        key = dedup_key(probe_result.url, name or probe_result.url)
        now = _now()
        with self.lock:
            entry = self._by_key.get(key)
            if entry is None:
                entry = RegistryEntry(
                    url=probe_result.url, key=key,
                    name=name or "", name_norm=normalize_name(name or ""),
                    found_at=now,
                )
                self._by_key[key] = entry

            entry.total_checks += 1
            entry.consecutive_failures += 1
            entry.last_checked_at = now
            entry.last_error = probe_result.reason[:200]
            entry.last_stage = probe_result.stage

            # 状态流转
            if entry.status == STATUS_ACTIVE:
                entry.status = STATUS_DEGRADED
            elif entry.status in (STATUS_PENDING, STATUS_DEGRADED):
                if entry.total_successes > 0:
                    entry.status = STATUS_DEGRADED
                # 新源保持 pending，等淘汰逻辑处理
            return entry

    # ------------------------------------------------------------ 淘汰
    def evict(self, cfg: dict) -> Dict[str, int]:
        """
        执行淘汰：
          1. 连续失败超阈值 -> RETIRED
          2. 长期未成功（max_idle_days）-> RETIRED
        返回淘汰统计。
        """
        lc = cfg.get("lifecycle", {}) or {}
        vcfg = cfg.get("validation", {}) or {}
        max_fail = int(vcfg.get("max_consecutive_failures", 3))
        newbie_grace = int(vcfg.get("newbie_grace_checks", 2))
        max_idle_days = int(lc.get("max_idle_days", 7))
        keep_tombstone = bool(lc.get("keep_tombstone", True))

        stats = {"by_failure": 0, "by_idle": 0, "degraded": 0}
        now = _now()
        to_remove: List[str] = []

        with self.lock:
            for key, e in self._by_key.items():
                # --- 规则 1：连续失败 ---
                if e.consecutive_failures >= max_fail:
                    # 新源宽限：刚发现不久的、失败次数还在宽限额度内的，保留观察
                    is_newbie = (e.total_successes == 0
                                 and e.consecutive_failures < newbie_grace + max_fail)
                    if not is_newbie:
                        if e.status != STATUS_RETIRED:
                            stats["by_failure"] += 1
                            log.debug("淘汰（连续失败 %d）：%s", e.consecutive_failures, e.name or e.url)
                        e.status = STATUS_RETIRED
                        e.tombstone = keep_tombstone
                        to_remove.append(key)

                # --- 规则 2：长期未成功 ---
                if e.status in (STATUS_ACTIVE, STATUS_DEGRADED) and max_idle_days > 0:
                    idle = _days_since(e.last_ok_at)
                    if idle > max_idle_days:
                        stats["by_idle"] += 1
                        e.status = STATUS_RETIRED
                        e.tombstone = keep_tombstone
                        to_remove.append(key)

                if e.status == STATUS_DEGRADED:
                    stats["degraded"] += 1

            # 清理 name 索引里的失效项
            for key in to_remove:
                e = self._by_key.get(key)
                if e and e.name_norm and self._by_name.get(e.name_norm) == key:
                    # 若有同名的新条目则改指向
                    alt = None
                    for k2, e2 in self._by_key.items():
                        if k2 != key and e2.name_norm == e2.name_norm == e.name_norm:
                            alt = k2
                            break
                    if alt:
                        self._by_name[e.name_norm] = alt
                    else:
                        self._by_name.pop(e.name_norm, None)

            # 按配置清理墓碑
            if not keep_tombstone:
                for key in to_remove:
                    self._by_key.pop(key, None)
                stats["purged"] = len(to_remove)

        if stats["by_failure"] or stats["by_idle"]:
            log.info("淘汰完成：连续失败 %d，长期未成功 %d，降级中 %d",
                     stats["by_failure"], stats["by_idle"], stats["degraded"])
        else:
            log.info("淘汰检查完成：无源需要淘汰（降级中 %d）", stats["degraded"])
        return stats

    # ------------------------------------------------------------ 待验证队列
    def pending_for_check(self, limit: int, cfg: dict, skip_known_ok_hours: int = 20) -> List[Candidate]:
        """
        生成本轮待验证队列。

        策略（按优先级）：
          1. 新源（status=pending，从未成功）      —— 优先，扩充数量
          2. 降级源（status=degraded）            —— 抢救
          3. 活跃源且距上次验证超过 N 小时        —— 巡检
        """
        vcfg = cfg.get("validation", {}) or {}
        max_fail = int(vcfg.get("max_consecutive_failures", 3))

        with self.lock:
            fresh, degraded, routine = [], [], []
            for e in self._by_key.values():
                if e.status == STATUS_PENDING:
                    # 从未成功的新源，或被淘汰后复活重新入列的源，都要验证
                    fresh.append(e)
                elif e.status == STATUS_DEGRADED:
                    degraded.append(e)
                elif e.status == STATUS_ACTIVE:
                    hours = _days_since(e.last_checked_at) * 24
                    if hours >= skip_known_ok_hours:
                        routine.append(e)
                # RETIRED 不参与本轮（除非被重新发现而复活）

            def to_cand(e: RegistryEntry) -> Candidate:
                return Candidate(
                    url=e.url, name=e.name, logo=e.logo, group=e.group,
                    tvg_id=e.tvg_id, found_via=e.found_via,
                    source_ref=e.source_ref, referer=e.referer,
                )

            queue: List[Candidate] = []
            queue += [to_cand(e) for e in fresh]
            queue += [to_cand(e) for e in degraded]
            queue += [to_cand(e) for e in routine]

            # ---- 域名均衡（domain diversity）----
            # 为什么需要：抽取阶段常从同一个 m3u 文件里抽出一批同域名的条目。
            # 若不限制，这些条目在注册表里连续排布，会把本轮验证配额全占掉
            # （实测 100 条里 98 条来自同一个域名 pi.0472.org，且全部不可达）。
            # 结果是"看起来挖了很多源"，实际只覆盖了一个站点。
            # 这里给每个域名设上限，超出部分顺延到后续轮次。
            cap = int(vcfg.get("max_candidates_per_host", 3) or 0)
            if cap > 0:
                queue = _rebalance_by_host(queue, cap)

            return queue[:limit]

    # ------------------------------------------------------------ 清理
    def purge_inactive(self, cfg: dict) -> int:
        """物理删除 RETIRED 记录（仅在 keep_tombstone=false 或手动 --gc 时调用）。"""
        lc = cfg.get("lifecycle", {}) or {}
        keep = bool(lc.get("keep_tombstone", True))
        if keep:
            return 0
        with self.lock:
            keys = [k for k, e in self._by_key.items() if e.status == STATUS_RETIRED]
            for k in keys:
                self._by_key.pop(k, None)
        return len(keys)
