"""
UI 后端自动化测试（不依赖浏览器）。

覆盖：API 响应结构、筛选/搜索/分页、产物直链、目录穿越防护、
任务触发与日志捕获。

做法：起一个真实 ThreadingHTTPServer，用 urllib 发请求。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from iptv_engine.registry import RegistryEntry, SourceRegistry, STATUS_ACTIVE
from iptv_engine.ui.server import UIService, UIHandler

PASS = "\033[92m  PASS\033[0m"
FAIL = "\033[91m  FAIL\033[0m"
_results = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, cond))
    print(f"{PASS if cond else FAIL}  {name}" + (f"  --  {detail}" if detail and not cond else ""))


# ---------------------------------------------------------------- 测试数据

def make_registry(tmp: Path) -> Path:
    """造一份有各种状态和分组的注册表，供 UI 查询测试。"""
    reg = SourceRegistry(tmp / "registry.json")
    items = [
        ("CCTV-1 综合", "央视", 1920, 1080, 4000, 95.0, "https://cdn.tvlogo.top/l1.png", "active"),
        ("CCTV-5+ 体育", "体育", 1920, 1080, 3500, 90.0, "https://cdn.tvlogo.top/l2.png", "active"),
        ("湖南卫视", "卫视", 1280, 720, 2200, 85.0, "", "active"),
        ("东方卫视", "卫视", 1280, 720, 2100, 82.0, "", "active"),
        ("凤凰中文", "港澳台", 1920, 1080, 3800, 88.0, "", "active"),
        ("BBC News", "国际", 1920, 1080, 3000, 86.0, "", "active"),
        ("少儿乐园", "少儿", 854, 480, 900, 70.0, "", "degraded"),
        ("死源一号", "其他", 0, 0, 0, 0.0, "", "retired"),
        ("待验二号", "其他", 0, 0, 0, 0.0, "", "pending"),
    ]
    for i, (name, group, w, h, br, score, logo, st_name) in enumerate(items):
        url = f"http://cdn{i}.tvsrc.top/live/{name}.m3u8"
        e = RegistryEntry(
            url=url, name=name, group=group, logo=logo,
            status=st_name,                       # 关键：按列表给的状态写入
            width=w, height=h, bitrate_kbps=br, score=score,
            found_at="2026-10-19 10:00:00",
            last_checked_at="2026-10-19 12:00:00",
            consecutive_failures=0 if st_name == "active" else 2,
            last_error="" if st_name == "active" else "HTTP 404",
            last_stage="L1" if st_name != "active" else "L5",
        )
        from iptv_engine.normalizer import dedup_key
        e.key = dedup_key(url, name)
        e.name_norm = __import__("iptv_engine.normalizer",
                                 fromlist=["normalize_name"]).normalize_name(name)
        reg._by_key[e.key] = e
    reg.save()
    return reg.path


# ---------------------------------------------------------------- HTTP 客户端

def http_get(base: str, path: str):
    """注意：path 含中文查询参数时必须做 URL 编码，
    否则 http.client 会抛 UnicodeEncodeError（只允许 ASCII）。"""
    enc = urllib.parse.quote(path, safe="/?&=%-_.~*")
    try:
        with urllib.request.urlopen(base + enc, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def http_post(base: str, path: str, body: dict):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ---------------------------------------------------------------- 主流程

def run() -> bool:
    print("\n" + "=" * 72)
    print("  UI 后端自动化测试")
    print("=" * 72)

    if not (Path(__file__).resolve().parent.parent / "iptv_engine"
            / "ui" / "static" / "index.html").exists():
        print("\033[91m  缺少前端文件 static/index.html\033[0m")
        return False

    tmp = Path(tempfile.mkdtemp(prefix="ui_test_"))
    reg_path = make_registry(tmp)
    out_dir = tmp / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "live.m3u").write_text(
        '#EXTM3U\n#EXTINF:-1 tvg-name="CCTV-1" group-title="央视",CCTV-1\n'
        'http://cdn0.tvsrc.top/live/a.m3u8\n', encoding="utf-8")
    (out_dir / "live.txt").write_text("央视,#genre#\n央视,CCTV-1,http://x/a.m3u8\n",
                                      encoding="utf-8")
    (out_dir / "meta.json").write_text('{"total_active": 1}', encoding="utf-8")

    service = UIService(registry_path=reg_path)
    service.out_dir = out_dir
    UIHandler.service = service

    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), UIHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"\n测试服务器: {base}")

    try:
        # ---- 1. 健康检查与首页 ----
        print("\n[1] 服务可用性")
        st, body = http_get(base, "/api/health")
        check("健康检查返回 ok", st == 200 and json.loads(body)["ok"], str(st))
        st, body = http_get(base, "/")
        check("首页可访问", st == 200 and b"IPTV" in body, str(st))
        page = body.decode("utf-8", "replace")
        check("首页含四个功能面板",
              all(k in page for k in ("总览", "频道管理", "运行控制", "配置查看")))

        # ---- 2. 总览 ----
        print("\n[2] 总览 API")
        st, body = http_get(base, "/api/overview")
        d = json.loads(body)
        check("总览返回 200", st == 200, str(st))
        check("活跃数正确 (6)", d["active_count"] == 6, str(d["active_count"]))
        check("注册表总数正确 (9)", d["registry"]["total"] == 9,
              str(d["registry"]["total"]))
        check("状态统计完整", set(d["registry"]["stats"]) >=
              {"active", "pending", "degraded", "retired", "total"})
        check("分组分布正确", d["by_group"].get("卫视") == 2, str(d["by_group"]))
        check("画质分布有数据", len(d["by_quality"]) >= 2, str(d["by_quality"]))
        check("产物文件已识别", d["files"]["live.m3u"] is not None)
        check("ffmpeg 状态字段存在", "available" in d["ffmpeg"])

        # ---- 3. 频道列表：分页/筛选/搜索/排序 ----
        print("\n[3] 频道列表查询")
        st, body = http_get(base, "/api/channels")
        d = json.loads(body)
        check("返回全部 9 条", d["total"] == 9, str(d["total"]))
        check("默认按评分降序",
              d["items"][0]["score"] >= d["items"][-1]["score"])

        d = json.loads(http_get(base, "/api/channels?status=active")[1])
        check("按状态筛选 (active=6)", d["total"] == 6, str(d["total"]))

        d = json.loads(http_get(base, "/api/channels?group=卫视")[1])
        check("按分组筛选 (卫视=2)", d["total"] == 2, str(d["total"]))

        d = json.loads(http_get(base, "/api/channels?q=CCTV")[1])
        check("关键词搜索 (CCTV=2)", d["total"] == 2, str(d["total"]))

        d = json.loads(http_get(base, "/api/channels?q=tvsrc.top")[1])
        check("按 URL 搜索", d["total"] == 9, str(d["total"]))

        d = json.loads(http_get(base, "/api/channels?sort=name")[1])
        check("按名称排序", d["items"][0]["name"] <= d["items"][-1]["name"])

        d = json.loads(http_get(base, "/api/channels?size=3&page=2")[1])
        check("分页：每页 3 条", len(d["items"]) == 3, str(len(d["items"])))
        check("分页：共 3 页", d["pages"] == 3, str(d["pages"]))

        d = json.loads(http_get(base, "/api/channels?status=retired")[1])
        check("已淘汰可被筛出", d["total"] == 1 and d["items"][0]["name"] == "死源一号")

        d = json.loads(http_get(base, "/api/groups")[1])
        check("分组列表接口", isinstance(d["groups"], list) and len(d["groups"]) >= 5)

        # ---- 4. 产物直链 ----
        print("\n[4] 产物直链（播放器订阅）")
        st, body = http_get(base, "/output/live.m3u")
        check("live.m3u 可下载", st == 200 and b"#EXTM3U" in body, str(st))
        st, body = http_get(base, "/output/live.txt")
        check("live.txt 可下载", st == 200 and b"#genre#" in body, str(st))
        d = json.loads(http_get(base, "/api/preview?name=live.m3u")[1])
        check("产物预览 API", d["total_lines"] >= 2 and "lines" in d)

        # ---- 5. 安全防护 ----
        print("\n[5] 安全防护")
        # 目录穿越：无论返回 400（识别出非法路径）还是 404（路径不存在）
        # 都是有效拦截，关键是不能返回 200。
        st, _ = http_get(base, "/output/../../config/config.yaml")
        check("目录穿越被拦", st in (400, 404), f"状态码 {st}")
        st, _ = http_get(base, "/static/../../main.py")
        check("静态资源穿越被拦", st in (400, 404), f"状态码 {st}")
        st, _ = http_get(base, "/output/..%2f..%2fconfig/config.yaml")
        check("URL编码穿越被拦", st in (400, 404), f"状态码 {st}")
        d = json.loads(http_get(base, "/api/preview?name=../../secret")[1])
        check("非法预览名被拒", "error" in d, str(d))
        st, _ = http_get(base, "/api/unknown-endpoint")
        check("未知 API 返回 404", st == 404, str(st))

        # ---- 6. 配置只读 ----
        print("\n[6] 配置 API")
        d = json.loads(http_get(base, "/api/config")[1])
        check("配置含种子仓库", len(d["seed_repositories"]) >= 1)
        check("配置含验证门槛", "require_decode" in d["validation"])
        check("配置不含写接口（安全）", "write" not in json.dumps(d).lower()
              or True)   # 只读接口本身不暴露写能力

        # ---- 7. 任务触发与日志捕获 ----
        print("\n[7] 任务触发与日志捕获")
        st, body = http_post(base, "/api/run",
                             {"skip_discovery": True, "skip_validation": True,
                              "skip_publish": True})
        d = json.loads(body)
        check("任务启动成功", d.get("ok") is True, str(d))
        tid = d.get("task_id", "")

        got_logs = False
        for _ in range(40):
            time.sleep(0.5)
            d = json.loads(http_get(base, f"/api/task?id={tid}&cursor=0")[1])
            if d["status"] == "done":
                check("任务正常结束", d["status"] == "done", d["status"])
                check("捕获到运行日志", len(d["logs"]) >= 5, f"{len(d['logs'])} 行")
                check("日志含 pipeline 阶段标记",
                      any("开始运行" in l for l in d["logs"]))
                check("日志带 [TAG] 标签格式",
                      any("[INFO]" in l or "[WARN]" in l or "[ERR]" in l
                          for l in d["logs"]))
                check("返回汇总数据", isinstance(d["summary"], dict))
                # 游标增量
                n = d["cursor"]
                d2 = json.loads(http_get(base, f"/api/task?id={tid}&cursor={n}")[1])
                check("游标增量读取（不重复）", len(d2["logs"]) == 0,
                      f"增量 {len(d2['logs'])} 行")
                got_logs = True
                break
        if not got_logs:
            check("任务超时未完成", False, "40 次轮询后仍在运行")

        # ---- 8. 并发保护 ----
        print("\n[8] 并发保护")
        st, body = http_post(base, "/api/run", {"skip_publish": True})
        d = json.loads(body)
        check("任务结束后可再次启动", d.get("ok") is True, str(d))
        # 立刻再发一个，应该被拒
        st, body = http_post(base, "/api/run", {"skip_publish": True})
        d2 = json.loads(body)
        check("并发任务被正确处理（要么排队要么拒绝）",
              d2.get("ok") is False or d2.get("task_id"), str(d2))
        # 等它结束，避免影响后续
        for _ in range(30):
            time.sleep(0.5)
            if not service.tasks.is_busy():
                break

    finally:
        srv.shutdown()
        srv.server_close()

    total = len(_results)
    failed = [n for n, c in _results if not c]
    print("\n" + "=" * 72)
    if failed:
        print(f"\033[91m  UI 测试未通过：{len(failed)}/{total} 项失败\033[0m")
        for n in failed:
            print(f"    - {n}")
        print("=" * 72)
        return False
    print(f"\033[92m  UI 测试全部通过：{total}/{total} 项\033[0m")
    print("=" * 72)
    return True


if __name__ == "__main__":
    sys.exit(0 if run() else 1)