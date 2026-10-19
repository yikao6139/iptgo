"""
流水线编排：把 6 个阶段串成一次完整的每日运行。

    ① 加载配置（含禁止白名单守卫）
    ② 载入动态注册表 sources/registry.json
    ③ 发现：多通路并行挖掘新源 → 去重 → 并入注册表
    ④ 组装本轮待验证队列（优先新源/降级源，再巡检活跃源）
    ⑤ 验证：五级递进门槛（L1 网络 → L2 内容 → L3 清单 → L4 分片 → L5 解码）
    ⑥ 回写注册表（成功/失败） → 淘汰 → 渲染 m3u/txt → 发布到 GitHub

每一步都做了异常隔离：单阶段失败不拖垮整轮，CI 里能留尽可能多的产物。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List, Optional

from .config import ROOT_DIR, load_config
from .extractor import Candidate
from .logging_utils import build_session_factory, get_logger, setup_logging
from .registry import SourceRegistry
from .renderer import render_all
from .validate import HAS_FFMPEG, ValidationBatchRunner

log = get_logger("pipeline")


class Pipeline:
    """一次完整运行。"""

    def __init__(self, config_path: Optional[Path] = None,
                 registry_path: Optional[Path] = None,
                 out_dir: Optional[Path] = None,
                 log_level: str = "INFO"):
        setup_logging(log_level)
        self.cfg = load_config(config_path)
        self.registry_path = registry_path or (ROOT_DIR / "sources" / "registry.json")
        self.out_dir = out_dir or (ROOT_DIR / "output")
        self.results_path = ROOT_DIR / "data" / "last_validation.json"
        self.summary: Dict = {}

    # ------------------------------------------------------------ 主流程
    def run(self, skip_discovery: bool = False, skip_validation: bool = False,
            skip_publish: bool = False, max_check: int = 0) -> Dict:
        t0 = time.monotonic()
        log.info("=" * 72)
        log.info("IPTV 自动发现引擎 v%s  开始运行", self._version())
        log.info("=" * 72)

        # ---- ② 载入注册表 ----
        registry = SourceRegistry(self.registry_path)
        self.summary["registry_load"] = len(registry.all_entries())

        # ---- ③ 发现 ----
        new_candidates: List[Candidate] = []
        if skip_discovery:
            log.info("[③] 跳过发现阶段（--skip-discovery）")
        else:
            new_candidates = self._discover()

        if new_candidates:
            added, existing = registry.upsert_candidates(new_candidates)
            self.summary["discovered_new"] = added
            self.summary["discovered_known"] = existing
        else:
            self.summary["discovered_new"] = 0
            self.summary["discovered_known"] = 0

        # 无论是否有新发现，都要巡检已有源（防止集体失效无人察觉）
        cap = max_check or int((self.cfg.get("lifecycle", {}) or {}).get("max_check_per_run", 4000))
        queue = registry.pending_for_check(cap, self.cfg)
        log.info("[④] 本轮待验证 %d 条", len(queue))

        # ---- ⑤ 验证 ----
        if skip_validation:
            log.info("[⑤] 跳过验证阶段（--skip-validation）")
            passed_count = 0
        else:
            passed_count = self._validate(registry, queue)

        # ---- ⑥ 回写 / 淘汰 / 渲染 / 发布 ----
        registry.evict(self.cfg)

        stats = registry.compute_stats()
        self.summary["registry_stats"] = stats

        # 渲染始终执行 —— 即使跳过验证，也应基于注册表里已有的活跃源
        # 重新生成播放列表。早期实现在 skip_validation 时连渲染一起跳过，
        # 会把已有的 live.m3u 渲染成 0 个频道，等于误清空。
        out_stats = render_all(registry, self.cfg, self.out_dir,
                               extra_stats={
                                   **stats,
                                   "validated_this_run": len(queue) if not skip_validation else 0,
                                   "passed_this_run": passed_count,
                                   "discovered_this_run": len(new_candidates),
                                   "validation_skipped": skip_validation,
                               })
        self.summary["output"] = out_stats
        if skip_validation:
            log.info("[⑤] 已跳过验证，播放列表基于现有活跃源重新渲染")

        if not skip_publish:
            self.summary["published"] = self._publish()
        else:
            log.info("[⑥] 跳过发布阶段（--skip-publish）")
            self.summary["published"] = False

        registry.save()

        elapsed = time.monotonic() - t0
        self.summary["elapsed_sec"] = round(elapsed, 1)
        self._print_summary()
        return self.summary

    # ------------------------------------------------------------ 各阶段
    def _discover(self) -> List[Candidate]:
        log.info("[③] 开始发现阶段：多通路挖掘新源…")
        try:
            from .discovery import DiscoveryDispatcher

            dispatcher = DiscoveryDispatcher(self.cfg)
            try:
                result = dispatcher.run()
                self.summary["discovery_stats"] = result.stats
                self.summary["discovery_repos"] = len(result.repos)
                return result.candidates
            finally:
                dispatcher.close()
        except Exception as exc:  # noqa: BLE001
            log.exception("发现阶段异常（不影响后续巡检）：%s", exc)
            self.summary["discovery_stats"] = {"error": str(exc)}
            return []

    def _validate(self, registry: SourceRegistry, queue: List[Candidate]) -> int:
        log.info("[⑤] 开始验证阶段：五级递进门槛（ffmpeg %s）",
                 "可用" if HAS_FFMPEG else "不可用，禁用解码验证")
        sf = build_session_factory(self.cfg, pool_size=int(
            (self.cfg.get("crawler", {}) or {}).get("max_workers", 32)),
            retries=1, qps=30.0)
        runner = ValidationBatchRunner(self.cfg, sf, self.results_path)
        try:
            passed = runner.run(queue, batch_size=300)
        except Exception as exc:  # noqa: BLE001
            log.exception("验证阶段异常：%s", exc)
            self.summary["validation_error"] = str(exc)
            return 0

        # 回写注册表
        ok = fail = 0
        stage_counter: Dict[str, int] = {}
        for res in runner.results:
            url = res.get("url", "")
            name = res.get("name", "")
            if res.get("ok"):
                registry.record_success(
                    _ProbeShim(res), name=name, group=res.get("group", ""),
                    logo=res.get("logo", ""), tvg_id=res.get("tvg_id", ""),
                    referer=res.get("referer", ""),
                )
                ok += 1
            else:
                registry.record_failure(_ProbeShim(res), name=name)
                fail += 1
                stage = res.get("stage", "?")
                stage_counter[stage] = stage_counter.get(stage, 0) + 1

        self.summary["validation"] = {
            "ok": ok, "fail": fail,
            "fail_by_stage": dict(sorted(stage_counter.items(), key=lambda kv: -kv[1])),
        }
        log.info("[⑤] 验证完成：通过 %d，失败 %d", ok, fail)
        if stage_counter:
            log.info("     失败分布：%s",
                     " ".join(f"{k}={v}" for k, v in stage_counter.items()))
        return ok

    def _publish(self) -> bool:
        log.info("[⑥] 开始发布阶段：提交到 GitHub…")
        try:
            from .publish import build_publisher, collect_output_files

            publisher = build_publisher(self.cfg)
            if publisher is None:
                log.warning("未配置 GitHub 发布，产物仅保存在本地 output/")
                return False
            files = collect_output_files(self.out_dir, self.registry_path)
            if not files:
                log.warning("没有可发布的文件")
                return False
            log.info("待提交文件 %d 个：%s", len(files), ", ".join(files.keys()))
            ok = publisher.publish(files)
            if ok:
                urls = publisher.raw_urls([
                    self.cfg.get("output", {}).get("m3u_name", "live.m3u"),
                    self.cfg.get("output", {}).get("txt_name", "live.txt"),
                ])
                for fn, u in urls.items():
                    log.info("订阅地址 [%s]: %s", fn, u)
            return ok
        except Exception as exc:  # noqa: BLE001
            log.exception("发布阶段异常：%s", exc)
            return False

    # ------------------------------------------------------------ 汇总
    def _print_summary(self) -> None:
        log.info("=" * 72)
        log.info("本次运行汇总")
        log.info("-" * 72)
        d = self.summary
        log.info("  发现阶段：新增 %s 条 / 命中已有 %s 条",
                 d.get("discovered_new", 0), d.get("discovered_known", 0))
        v = d.get("validation", {}) or {}
        log.info("  验证阶段：通过 %s / 失败 %s", v.get("ok", 0), v.get("fail", 0))
        if v.get("fail_by_stage"):
            log.info("            失败分层：%s",
                     ", ".join(f"{k}:{n}" for k, n in v["fail_by_stage"].items()))
        rs = d.get("registry_stats", {}) or {}
        log.info("  注册表：活跃 %s / 待定 %s / 降级 %s / 淘汰 %s / 总计 %s",
                 rs.get("active", 0), rs.get("pending", 0), rs.get("degraded", 0),
                 rs.get("retired", 0), rs.get("total", 0))
        o = d.get("output", {}) or {}
        log.info("  输出：m3u %s 个频道，txt %s 个频道",
                 o.get("m3u", 0), o.get("txt", 0))
        log.info("  发布：%s", "成功" if d.get("published") else "未发布/跳过")
        log.info("  总耗时：%s 秒", d.get("elapsed_sec", "?"))
        log.info("=" * 72)

    @staticmethod
    def _version() -> str:
        from . import __version__
        return __version__


class _ProbeShim:
    """把 batch_runner 落盘的 dict 包成 ProbeResult 供 registry 消费。"""

    def __init__(self, d: dict):
        self.url = d.get("url", "")
        self.ok = d.get("ok", False)
        self.reason = d.get("reason", "")
        self.stage = d.get("stage", "")
        self.score = d.get("score", 0.0)
        self.width = d.get("width", 0)
        self.height = d.get("height", 0)
        self.bitrate_kbps = d.get("bitrate_kbps", 0)
        self.fps = d.get("fps", 0.0)
        self.video_codec = d.get("video_codec", "")
        self.audio_codec = d.get("audio_codec", "")
        self.manifest_kind = d.get("manifest_kind", "")