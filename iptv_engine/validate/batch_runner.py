"""
验证批次调度器：把候选流分发给多线程验证器，收集结果并落盘。

并发策略：
  - 全局线程池 + 每主机信号量（防单站点被打爆）
  - 分批处理，每批结束落一次盘（CI 中途被杀也不丢结果）
  - 单条验证异常绝不中断整批
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..logging_utils import build_session_factory, get_logger
from .stream_validator import HAS_FFMPEG, ProbeResult, StreamValidator

log = get_logger("validate.batch")


class ValidationBatchRunner:
    """并发验证一批候选。"""

    def __init__(self, cfg: dict, session_factory, path: Path):
        self.cfg = cfg
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.results: List[dict] = []
        # 每线程一个验证器（验证器持有每主机信号量表，需要隔离）
        self._local = threading.local()

    def _validator(self, session_factory: SessionFactory) -> StreamValidator:
        v = getattr(self._local, "validator", None)
        if v is None:
            v = StreamValidator(self.cfg, session_factory)
            self._local.validator = v
        return v

    def _flush(self) -> None:
        with self.lock:
            snapshot = list(self.results)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def run(self, candidates: List, batch_size: int = 300, total_cap: int = 0) -> List[ProbeResult]:
        """
        分批验证。返回通过验证的结果列表。
        batch_size: 每批多少条（决定落盘频率）
        total_cap: 本次最多验证多少条（0 = 不限）
        """
        if not HAS_FFMPEG:
            # 关键降级：配置要求解码验证，但环境没有 ffmpeg。
            # 此时若继续按原配置跑，100% 的源都会卡在 L5 判失败，
            # 等于白跑一轮。正确做法是明确关闭解码门槛并告警，
            # 让 L1-L4 的清单/分片验证继续发挥作用。
            log.warning("未检测到 ffmpeg/ffprobe，自动关闭解码验证（L5）")
            log.warning("CI 环境请安装 ffmpeg，否则无法验证真实可播放性")
            self.cfg = {**self.cfg}
            self.cfg["validation"] = {**(self.cfg.get("validation") or {}),
                                      "require_decode": False}

        pool_size = int((self.cfg.get("crawler", {}) or {}).get("max_workers", 32))
        todo = candidates[:total_cap] if total_cap else candidates
        log.info("开始验证 %d 条候选（线程池 %d，ffmpeg=%s）",
                 len(todo), pool_size, "可用" if HAS_FFMPEG else "不可用")

        passed: List[ProbeResult] = []
        ok_count = 0
        fail_count = 0
        started = time.monotonic()

        for batch_start in range(0, len(todo), batch_size):
            batch = todo[batch_start : batch_start + batch_size]
            with ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="val") as ex:
                fut2cand = {
                    ex.submit(self._validator(self.cfg and self._make_sf()).validate, c): c
                    for c in batch
                }
                for fut in as_completed(fut2cand):
                    cand = fut2cand[fut]
                    try:
                        res = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        res = ProbeResult(url=cand.url, ok=False, stage="EXC",
                                          reason=f"验证器异常 {type(exc).__name__}")
                    payload = res.to_dict()
                    payload["name"] = cand.name
                    payload["group"] = cand.group
                    payload["logo"] = cand.logo
                    payload["tvg_id"] = cand.tvg_id
                    payload["found_via"] = cand.found_via
                    payload["source_ref"] = cand.source_ref
                    payload["referer"] = cand.referer
                    payload["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    with self.lock:
                        self.results.append(payload)
                    if res.ok:
                        ok_count += 1
                        passed.append(res)
                    else:
                        fail_count += 1

            self._flush()
            done = batch_start + len(batch)
            elapsed = time.monotonic() - started
            rate = done / elapsed if elapsed else 0
            eta = (len(todo) - done) / rate if rate else 0
            log.info("已验证 %d/%d | 通过 %d 失败 %d | 通过率 %.1f%% | ETA %.0fs",
                     done, len(todo), ok_count, fail_count,
                     100.0 * ok_count / max(1, done), eta)

        self._flush()
        log.info("验证完成：通过 %d / 失败 %d（共 %d，用时 %.1fs）",
                 ok_count, fail_count, len(todo), time.monotonic() - started)
        return passed

    def _make_sf(self) -> SessionFactory:
        """每个工作线程用独立 SessionFactory，避免限流器争用与 Session 线程安全问题。"""
        sf = getattr(self._local, "sf", None)
        if sf is None:
            self._local.sf = build_session_factory(self.cfg, pool_size=4,
                                                    retries=1, qps=30.0)
        return sf

    @staticmethod
    def load_results(path: Path) -> List[dict]:
        p = Path(path)
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            return []