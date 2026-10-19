"""
本地 Web 界面 —— 后端服务。

设计约束：**零第三方依赖**，只用 Python 标准库
（http.server / json / threading / subprocess）。
这样用户 `python main.py ui` 就能直接打开，不需要先装 Flask/FastAPI。

安全边界：仅监听 127.0.0.1，不对外暴露。界面提供的是「只读浏览 +
本地触发运行」，不做配置写入，避免误改导致线上状态损坏。
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from ..config import ROOT_DIR, load_config
from ..logging_utils import get_logger, setup_logging
from ..registry import STATUS_ACTIVE, SourceRegistry

log = get_logger("ui.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = 8765


# ---------------------------------------------------------------- 运行任务

class RunTask:
    """一次后台运行的实时状态与日志缓冲。"""

    def __init__(self, task_id: str, kind: str):
        self.task_id = task_id
        self.kind = kind
        self.status = "running"      # running / done / failed
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.finished_at = ""
        self.logs: List[str] = []
        self.summary: Dict[str, Any] = {}
        self.error = ""
        self._cursor = 0
        # logging 会在多个线程里 emit，必须加锁
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        with self._lock:
            self.logs.append(line)
            # 只保留最近 5000 行，避免长时间运行撑爆内存
            if len(self.logs) > 5000:
                del self.logs[:1000]

    def snapshot(self, cursor: int) -> Dict[str, Any]:
        """取增量日志（加锁，避免读到写入中间态）。"""
        with self._lock:
            chunk = list(self.logs[cursor:])
            end = len(self.logs)
            status = self.status
            finished = self.finished_at
            error = self.error
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "status": status,
            "started_at": self.started_at,
            "finished_at": finished,
            "cursor": end,
            "logs": chunk,
            "summary": dict(self.summary) if self.summary else {},
            "error": error,
        }


class TaskManager:
    """管理后台运行任务（同一时刻只允许一个运行任务）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: Dict[str, RunTask] = {}
        self._current: Optional[RunTask] = None
        self._seq = 0
        self._thread: Optional[threading.Thread] = None

    @property
    def current(self) -> Optional[RunTask]:
        with self._lock:
            return self._current

    def is_busy(self) -> bool:
        with self._lock:
            return self._current is not None and self._current.status == "running"

    def start(self, kind: str, worker) -> RunTask:
        """启动一个后台任务。worker(task) 负责实际执行。"""
        with self._lock:
            if self._current and self._current.status == "running":
                return self._current
            self._seq += 1
            task = RunTask(f"t{self._seq}_{int(time.time())}", kind)
            self._tasks[task.task_id] = task
            # 只保留最近 20 个任务
            if len(self._tasks) > 20:
                oldest = sorted(self._tasks.values(), key=lambda t: t.task_id)[0]
                self._tasks.pop(oldest.task_id, None)
            self._current = task

        def run():
            try:
                worker(task)
                task.status = "done"
            except Exception as exc:  # noqa: BLE001
                task.status = "failed"
                task.error = f"{type(exc).__name__}: {exc}"
                task.append(f"\n[错误] {task.error}")
            finally:
                task.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
                log.info("任务 %s 结束，状态=%s", task.task_id, task.status)

        th = threading.Thread(target=run, daemon=True, name=f"task-{task.task_id}")
        self._thread = th
        th.start()
        return task

    def get(self, task_id: str) -> Optional[RunTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def latest(self) -> Optional[RunTask]:
        with self._lock:
            return self._current


# ---------------------------------------------------------------- 日志桥接

class ListHandler(logging.Handler):
    """把 logging 输出转发到 RunTask 缓冲，供前端轮询。"""

    def __init__(self, task: RunTask):
        super().__init__()
        self.task = task
        # 与 StreamHandler 的格式对齐，方便前端按 [TAG] 着色
        self.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s",
            datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001
            return
        lvl = record.levelname
        tag = {"ERROR": "ERR", "WARNING": "WARN", "DEBUG": "DBG"}.get(lvl, "INFO")
        # 格式化成 "HH:MM:SS [TAG] logger | message"，前端按 [TAG] 上色
        parts = line.split(" | ", 2)
        if len(parts) == 3:
            ts = parts[0].split(" ", 1)[-1] if " " in parts[0] else parts[0]
            self.task.append(f"{ts} [{tag}] {parts[2]}")
        else:
            self.task.append(f"[{tag}] {line}")


# ---------------------------------------------------------------- 业务逻辑

class UIService:
    """UI 后端的数据与操作接口。"""

    def __init__(self, registry_path: Optional[Path] = None):
        self.registry_path = registry_path or (ROOT_DIR / "sources" / "registry.json")
        self.out_dir = ROOT_DIR / "output"
        self.tasks = TaskManager()

    # ---------------------------------------------------------- 概览
    def overview(self) -> Dict[str, Any]:
        reg = SourceRegistry(self.registry_path)
        entries = reg.all_entries()
        stats = reg.compute_stats()

        actives = [e for e in entries if e.status == STATUS_ACTIVE]
        by_group: Dict[str, int] = {}
        by_quality: Dict[str, int] = {}
        total_kbps = 0
        for e in actives:
            by_group[e.group or "其他"] = by_group.get(e.group or "其他", 0) + 1
            h = e.height or 0
            if h >= 2160: q = "4K"
            elif h >= 1080: q = "1080P"
            elif h >= 720: q = "720P"
            elif h >= 480: q = "480P"
            elif h > 0: q = "SD"
            else: q = "未知"
            by_quality[q] = by_quality.get(q, 0) + 1
            total_kbps += e.bitrate_kbps

        # 产物文件信息
        files = {}
        for name in ("live.m3u", "live.txt", "meta.json"):
            p = self.out_dir / name
            if p.exists():
                st = p.stat()
                files[name] = {
                    "size": st.st_size,
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                }
            else:
                files[name] = None

        task = self.tasks.latest()
        last_run = None
        if task:
            last_run = {
                "task_id": task.task_id,
                "kind": task.kind,
                "status": task.status,
                "started_at": task.started_at,
                "finished_at": task.finished_at,
                "summary": task.summary,
                "error": task.error,
            }

        return {
            "registry": {
                "path": str(self.registry_path),
                "exists": self.registry_path.exists(),
                "total": len(entries),
                "stats": stats,
            },
            "active_count": len(actives),
            "by_group": dict(sorted(by_group.items(), key=lambda kv: -kv[1])),
            "by_quality": dict(sorted(by_quality.items(),
                                       key=lambda kv: -kv[1])),
            "avg_bitrate_kbps": round(total_kbps / len(actives)) if actives else 0,
            "files": files,
            "last_run": last_run,
            "busy": self.tasks.is_busy(),
            "ffmpeg": self._ffmpeg_status(),
        }

    def _ffmpeg_status(self) -> Dict[str, Any]:
        try:
            from ..validate.stream_validator import (
                FFMPEG_BIN,
                FFPROBE_BIN,
                HAS_FFMPEG,
            )
            return {"available": HAS_FFMPEG, "ffmpeg": FFMPEG_BIN,
                    "ffprobe": FFPROBE_BIN}
        except Exception:  # noqa: BLE001
            return {"available": False, "ffmpeg": None, "ffprobe": None}

    # ---------------------------------------------------------- 频道列表
    def channels(self, query: Dict[str, List[str]]) -> Dict[str, Any]:
        reg = SourceRegistry(self.registry_path)
        entries = reg.all_entries()

        kw = (query.get("q") or [""])[0].strip().lower()
        status = (query.get("status") or ["all"])[0]
        group = (query.get("group") or ["all"])[0]
        sort = (query.get("sort") or ["score"])[0]
        page = max(1, int((query.get("page") or ["1"])[0]))
        # size 下限必须是 1（原来写 max(10,...) 会让 ?size=3 被静默抬到 10，
        # 导致分页参数失效：第 2 页取不到数据）
        size = min(500, max(1, int((query.get("size") or ["50"])[0])))

        if status != "all":
            entries = [e for e in entries if e.status == status]
        if group != "all":
            entries = [e for e in entries if (e.group or "其他") == group]
        if kw:
            entries = [
                e for e in entries
                if kw in (e.name or "").lower()
                or kw in (e.url or "").lower()
                or kw in (e.tvg_id or "").lower()
            ]

        sorters = {
            "score": lambda e: -e.score,
            "name": lambda e: (e.name or "").lower(),
            "bitrate": lambda e: -e.bitrate_kbps,
            "height": lambda e: -(e.height or 0),
            "recent": lambda e: (e.last_checked_at or ""),
            "found": lambda e: (e.found_at or ""),
        }
        entries.sort(key=sorters.get(sort, sorters["score"]))

        total = len(entries)
        start = (page - 1) * size
        page_items = entries[start:start + size]

        return {
            "total": total,
            "page": page,
            "size": size,
            "pages": max(1, (total + size - 1) // size),
            "items": [
                {
                    "key": e.key,
                    "name": e.name,
                    "url": e.url,
                    "group": e.group or "其他",
                    "logo": e.logo,
                    "tvg_id": e.tvg_id,
                    "status": e.status,
                    "score": e.score,
                    "width": e.width,
                    "height": e.height,
                    "bitrate_kbps": e.bitrate_kbps,
                    "fps": e.fps,
                    "video_codec": e.video_codec,
                    "audio_codec": e.audio_codec,
                    "found_at": e.found_at,
                    "last_checked_at": e.last_checked_at,
                    "last_ok_at": e.last_ok_at,
                    "consecutive_failures": e.consecutive_failures,
                    "total_checks": e.total_checks,
                    "total_successes": e.total_successes,
                    "last_error": e.last_error,
                    "last_stage": e.last_stage,
                    "found_via": e.found_via,
                }
                for e in page_items
            ],
        }

    def groups(self) -> List[str]:
        reg = SourceRegistry(self.registry_path)
        gs = {e.group or "其他" for e in reg.all_entries()}
        return sorted(gs)

    # ---------------------------------------------------------- 运行
    def start_run(self, options: Dict[str, Any]) -> Dict[str, Any]:
        """启动一次完整流程（发现→验证→渲染）。"""
        if self.tasks.is_busy():
            return {"ok": False, "message": "已有任务在运行中", "busy": True}

        skip_discovery = bool(options.get("skip_discovery"))
        skip_validation = bool(options.get("skip_validation"))
        skip_publish = bool(options.get("skip_publish"))
        max_check = int(options.get("max_check") or 0)

        def worker(task: RunTask):
            from ..pipeline import Pipeline
            import logging as _logging

            handler = ListHandler(task)
            root = _logging.getLogger()
            root.addHandler(handler)
            old_level = root.level
            root.setLevel(_logging.INFO)
            try:
                task.append("=== 开始运行 IPTV 引擎 ===")
                pipe = Pipeline(registry_path=self.registry_path,
                                log_level="INFO")
                summary = pipe.run(
                    skip_discovery=skip_discovery,
                    skip_validation=skip_validation,
                    skip_publish=skip_publish,
                    max_check=max_check,
                )
                task.summary = summary
            finally:
                root.removeHandler(handler)
                root.setLevel(old_level)

        task = self.tasks.start("run", worker)
        return {"ok": True, "task_id": task.task_id}

    def start_demo(self) -> Dict[str, Any]:
        """启动演示模式（生成本地可播流与播放列表）。"""
        if self.tasks.is_busy():
            return {"ok": False, "message": "已有任务在运行中", "busy": True}

        def worker(task: RunTask):
            import subprocess
            import sys
            script = ROOT_DIR / "tests" / "demo_local.py"
            task.append("=== 演示模式：生成真实可播的 HLS 流 ===")
            proc = subprocess.Popen(
                [sys.executable, "-B", str(script)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
            for line in proc.stdout:            # type: ignore[union-attr]
                task.append(line.rstrip())
            rc = proc.wait()
            task.summary = {"returncode": rc}
            if rc != 0:
                raise RuntimeError(f"演示模式退出码 {rc}")

        task = self.tasks.start("demo", worker)
        return {"ok": True, "task_id": task.task_id}

    def task_status(self, task_id: str, cursor: int) -> Dict[str, Any]:
        task = self.tasks.get(task_id) or self.tasks.latest()
        if task is None:
            return {"error": "没有任务"}
        return task.snapshot(cursor)

    # ---------------------------------------------------------- 配置只读
    def config_view(self) -> Dict[str, Any]:
        """返回配置摘要（只读，不提供写入）。

        刻意不提供写接口：配置错误会导致线上跑空，
        而 config.yaml 里有大量注释，纯程序改写会破坏可读性。
        需要调整请直接编辑文件。
        """
        try:
            cfg = load_config()
        except Exception as exc:  # noqa: BLE001
            return {"error": f"配置加载失败: {exc}"}

        def g(path, default=None):
            node: Any = cfg
            for p in path.split("."):
                if not isinstance(node, dict) or p not in node:
                    return default
                node = node[p]
            return node

        engines = g("discovery.search_engines", []) or []
        return {
            "seed_repositories": cfg.get("seed_repositories", []),
            "search_keywords": g("discovery.search_keywords", []),
            "engines": [
                {"name": e.get("name"), "enabled": e.get("enabled")}
                for e in engines
            ],
            "validation": {
                "require_decode": g("validation.require_decode"),
                "min_video_kbps": g("validation.min_video_kbps"),
                "min_resolution_width": g("validation.min_resolution_width"),
                "max_consecutive_failures": g("validation.max_consecutive_failures"),
            },
            "lifecycle": {
                "max_idle_days": g("lifecycle.max_idle_days"),
                "max_check_per_run": g("lifecycle.max_check_per_run"),
            },
            "output": {
                "m3u_name": g("output.m3u_name"),
                "txt_name": g("output.txt_name"),
                "sort_by": g("output.sort_by"),
            },
            "publish_enabled": g("publish.github_repo.enabled"),
            "config_path": str(ROOT_DIR / "config" / "config.yaml"),
        }

    # ---------------------------------------------------------- 产物预览
    def preview(self, name: str, limit: int = 300) -> Dict[str, Any]:
        allowed = {"live.m3u", "live.txt", "meta.json"}
        if name not in allowed:
            return {"error": "不允许的文件"}
        p = self.out_dir / name
        if not p.exists():
            return {"error": "文件不存在"}
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        return {
            "name": name,
            "size": p.stat().st_size,
            "total_lines": len(lines),
            "truncated": len(lines) > limit,
            "lines": lines[:limit],
        }


# ---------------------------------------------------------------- HTTP

class UIHandler(BaseHTTPRequestHandler):
    service: UIService = None       # type: ignore[assignment]
    server_version = "IPTV-UI/1.0"

    def log_message(self, fmt, *args):     # 静默默认访问日志
        return

    # -------------------------------------------------------- 工具
    def _json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n <= 0:
                return {}
            raw = self.rfile.read(n)
            return json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _file(self, path: Path, download: bool = False) -> None:
        if not path.exists() or not path.is_file():
            self._json({"error": "文件不存在"}, 404)
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or path.suffix in (".m3u", ".txt", ".json"):
            ctype = f"{ctype}; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if download:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(data)

    # -------------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        p = u.path
        # 显式指定 UTF-8 解码：parse_qs 默认按 latin-1，
        # 中文查询参数（如 ?group=卫视）会解出乱码导致筛选失效。
        try:
            q = parse_qs(u.query, encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            q = {}

        # ---- 静态资源 ----
        if p in ("/", "/index.html"):
            return self._file(STATIC_DIR / "index.html")
        if p.startswith("/static/"):
            name = p[len("/static/"):]
            if "/" in name or ".." in name:      # 防目录穿越
                return self._json({"error": "非法路径"}, 400)
            return self._file(STATIC_DIR / name)

        # ---- 产物直链（供播放器订阅/下载）----
        if p.startswith("/output/"):
            name = p[len("/output/"):]
            dl = (q.get("download", ["0"])[0] == "1")
            if "/" in name or ".." in name:
                return self._json({"error": "非法路径"}, 400)
            return self._file(self.service.out_dir / name, download=dl)

        # ---- API ----
        if p == "/api/overview":
            return self._json(self.service.overview())
        if p == "/api/channels":
            return self._json(self.service.channels(q))
        if p == "/api/groups":
            return self._json({"groups": self.service.groups()})
        if p == "/api/config":
            return self._json(self.service.config_view())
        if p == "/api/preview":
            name = (q.get("name") or ["live.m3u"])[0]
            return self._json(self.service.preview(name))
        if p == "/api/task":
            tid = (q.get("id") or [""])[0]
            cur = int((q.get("cursor") or ["0"])[0])
            return self._json(self.service.task_status(tid, cur))
        if p == "/api/health":
            return self._json({"ok": True, "service": "iptv-ui"})

        return self._json({"error": "not found"}, 404)

    # -------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        p = u.path
        body = self._read_json()

        if p == "/api/run":
            return self._json(self.service.start_run(body))
        if p == "/api/demo":
            return self._json(self.service.start_demo())
        return self._json({"error": "not found"}, 404)


# ---------------------------------------------------------------- 启动

def serve(host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          open_browser: bool = True, registry_path: Optional[Path] = None) -> None:
    """启动本地 Web 界面。阻塞直到 Ctrl+C。"""
    setup_logging("INFO")
    service = UIService(registry_path=registry_path)
    UIHandler.service = service

    # 端口被占用则自动 +1，最多试 20 个
    httpd = None
    for i in range(20):
        try:
            httpd = ThreadingHTTPServer((host, port + i), UIHandler)
            break
        except OSError:
            continue
    if httpd is None:
        log.error("无法绑定端口 %d-%d", port, port + 19)
        return

    actual = httpd.server_address[1]
    url = f"http://{host}:{actual}/"
    log.info("=" * 60)
    log.info("  IPTV 引擎控制台已启动")
    log.info("  地址: %s", url)
    log.info("  注册表: %s", service.registry_path)
    log.info("  按 Ctrl+C 停止")
    log.info("=" * 60)

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("\n正在关闭…")
    finally:
        httpd.shutdown()
        httpd.server_close()
        log.info("已停止")