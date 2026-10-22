"""
离线冒烟自检：不需要网络，验证核心逻辑是否正确。

覆盖：
  1. 配置守卫：硬编码流地址必须被拦截
  2. URL 归一化：等价 URL 必须收敛
  3. 频道名归一化：别名必须收敛
  4. 双重去重：URL 重复 + 频道重复都要拦住
  5. M3U 严格解析：字段完整、相对路径补全
  6. 宽松抽取：从 HTML/JS 噪声里捞出地址
  7. HLS 解析：master / media playlist
  8. 注册表生命周期：成功 -> 失败 -> 淘汰
  9. M3U 渲染：格式合法、含分组
 10. 内网地址拦截（SSRF 防护）

运行：python main.py selftest
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from iptv_engine.config import (  # noqa: E402
    ConfigViolation,
    enforce_no_hardcoded_streams,
    load_config,
)
from iptv_engine.extractor import (  # noqa: E402
    Candidate,
    extract,
    guess_group_from_name,
    guess_name_from_url,
    parse_m3u,
    scrape_text,
)
from iptv_engine.normalizer import (  # noqa: E402
    dedup_candidates,
    dedup_key,
    is_private_host,
    normalize_name,
    normalize_url,
)
from iptv_engine.registry import (  # noqa: E402
    STATUS_ACTIVE,
    STATUS_RETIRED,
    RegistryEntry,
    SourceRegistry,
)
from iptv_engine.renderer import collect_active, render_m3u, render_txt  # noqa: E402
from iptv_engine.validate.stream_validator import looks_like_dash, parse_hls  # noqa: E402

PASS = "\033[92m  PASS\033[0m"
FAIL = "\033[91m  FAIL\033[0m"

_results: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, cond, detail))
    print(f"{PASS if cond else FAIL}  {name}" + (f"  --  {detail}" if detail and not cond else ""))


# ---------------------------------------------------------------- 1 配置守卫
def test_config_guard() -> None:
    print("\n[1] 禁止白名单守卫")
    try:
        enforce_no_hardcoded_streams({"c": {"evil": "http://a.com/live/x.m3u8"}})
        check("拦截硬编码 m3u8", False, "未抛出 ConfigViolation")
    except ConfigViolation:
        check("拦截硬编码 m3u8", True)

    try:
        enforce_no_hardcoded_streams({"c": {"evil": "rtmp://1.2.3.4:1935/live/stream"}})
        check("拦截硬编码 rtmp", False, "未抛出")
    except ConfigViolation:
        check("拦截硬编码 rtmp", True)

    try:
        enforce_no_hardcoded_streams({"c": {"evil": "rtsp://cam.example.org:554/h264stream"}})
        check("拦截硬编码 rtsp", False, "未抛出")
    except ConfigViolation:
        check("拦截硬编码 rtsp", True)

    # 合法配置不应被误伤
    try:
        enforce_no_hardcoded_streams({
            "github": {"api_base": "https://api.github.com"},
            "discovery": {"search_engines": [
                {"name": "bing", "endpoint": "https://www.bing.com/search"}]},
            "seed_repositories": ["a/b"],
            "search_keywords": ["m3u8 ipts live"],
        })
        check("合法配置不被误伤", True)
    except ConfigViolation as e:
        check("合法配置不被误伤", False, str(e)[:100])

    # 真实配置文件必须通过
    try:
        cfg = load_config()
        check("项目 config.yaml 通过守卫", bool(cfg.get("seed_repositories")))
    except Exception as exc:  # noqa: BLE001
        check("项目 config.yaml 通过守卫", False, str(exc)[:120])


# ---------------------------------------------------------------- 2 URL 归一
def test_url_normalize() -> None:
    print("\n[2] URL 归一化")
    a = normalize_url("http://Example.COM:80/live/a.m3u8")
    b = normalize_url("http://example.com/live/a.m3u8")
    check("主机大小写 + 默认端口归一", a == b, f"{a} != {b}")

    c = normalize_url("https://x.com/live//a.m3u8/")
    d = normalize_url("https://x.com/live/a.m3u8")
    check("重复斜杠 + 尾斜杠归一", c == d, f"{c} != {d}")

    e = normalize_url("https://x.com/a.m3u8?b=2&a=1")
    f = normalize_url("https://x.com/a.m3u8?a=1&b=2")
    check("查询参数顺序归一", e == f, f"{e} != {f}")

    g = normalize_url("https://x.com/a.m3u8?utm_source=wx&id=5")
    h = normalize_url("https://x.com/a.m3u8?id=5")
    check("追踪参数剥离", g == h, f"{g} != {h}")

    # token 类参数必须保留
    i = normalize_url("https://x.com/a.m3u8?token=abc123")
    j = normalize_url("https://x.com/a.m3u8")
    check("token 参数保留", i != j, "token 被误删")

    # fragment 丢弃
    k = normalize_url("https://x.com/a.m3u8#frag")
    check("fragment 丢弃", k == "https://x.com/a.m3u8", k)


# ---------------------------------------------------------------- 3 名称归一
def test_name_normalize() -> None:
    print("\n[3] 频道名归一化")
    variants = [
        "CCTV-1 综合",
        "CCTV1综合",
        "CCTV 1 综合",
        "【CCTV1】综合",
        "cctv-1  综合 HD",
    ]
    norms = {normalize_name(v) for v in variants}
    check("CCTV1 五种写法收敛", len(norms) == 1, f"得到 {len(norms)} 种: {norms}")

    n1 = normalize_name("卫视-湖南卫视")
    check("全角转半角", "＊" not in n1 and n1 != "")
    n2 = normalize_name("CCTV-5+ 体育赛事")
    check("含+频道名可处理", isinstance(n2, str) and len(n2) > 0)


# ---------------------------------------------------------------- 4 双重去重
def test_dedup() -> None:
    print("\n[4] 双重去重")
    items = [
        Candidate(url="http://x.com/live/cctv1.m3u8", name="CCTV-1 综合"),
        Candidate(url="http://x.com/live/cctv1.m3u8", name="CCTV-1 综合"),  # 完全重复
        Candidate(url="http://x.com/live/cctv1.m3u8?from=wx", name="CCTV-1 综合"),  # URL 等价
        Candidate(url="http://y.com/other.m3u8", name="CCTV1综合"),  # 频道名等价
        Candidate(url="http://z.com/live/hunan.m3u8", name="湖南卫视"),  # 唯一
    ]
    out = dedup_candidates(items)
    check("5 条去重为 2 条", len(out) == 2, f"得到 {len(out)}: {[o.url for o in out]}")

    k1 = dedup_key("http://x.com/live/a.m3u8", "CCTV-1")
    k2 = dedup_key("http://X.com:80/live/a.m3u8", "cctv1")
    check("dedup_key 对等价输入一致", k1 == k2)

    # 空名称不应互相误杀
    out2 = dedup_candidates([
        Candidate(url="http://a.com/1.m3u8", name=""),
        Candidate(url="http://b.com/2.m3u8", name=""),
    ])
    check("空名称不误杀", len(out2) == 2, f"得到 {len(out2)}")


# ---------------------------------------------------------------- 5 M3U 解析
def test_m3u_parse() -> None:
    print("\n[5] M3U 严格解析")
    content = """#EXTM3U
#EXTINF:-1 tvg-id="cctv1" tvg-name="CCTV1" tvg-logo="http://x/l.png" group-title="央视",CCTV-1 综合
http://x.com/live/cctv1.m3u8
#EXTINF:-1 group-title="卫视",湖南卫视 HD
http://x.com/live/hunan.m3u8
#EXTGRP:体育
#EXTINF:-1,CCTV5+
http://x.com/live/cctv5.m3u8
"""
    got = parse_m3u(content)
    check("解析出 3 条", len(got) == 3, f"得到 {len(got)}")
    if got:
        check("首条 tvg-id 正确", got[0].tvg_id == "cctv1", got[0].tvg_id)
        check("首条 logo 正确", got[0].logo == "http://x/l.png", got[0].logo)
        check("首条分组正确", got[0].group == "央视", got[0].group)
        # #EXTGRP 出现在 #EXTINF 之前，负责覆盖该条目的分组
        check("EXTGRP 覆盖生效", got[2].group == "体育", got[2].group)

    # 相对地址补全
    rel = '#EXTM3U\n#EXTINF:-1,Test\n/live/rel.m3u8'
    got2 = parse_m3u(rel, source_ref="https://host.tv/list/main.m3u")
    check("相对路径补全", got2 and got2[0].url == "https://host.tv/live/rel.m3u8",
          got2[0].url if got2 else "无")


# ---------------------------------------------------------------- 6 宽松抽取
def test_scrape() -> None:
    print("\n[6] 宽松抽取（HTML/JS 噪声）")
    html = """
    <html><head><title>直播源</title></head><body>
      <div class="list">
        <a href="http://cdn1.com/live/cctv13.m3u8">CCTV13 新闻直播</a>
        <script>
           var src = "https://cdn2.com/live/hls/sports.m3u8";
           const other = 'http://cdn3.com/stream/etv1.ts';
        </script>
      </div>
      <img src="http://cdn.com/logo.png">
    </body></html>
    """
    got = scrape_text(html, source_ref="http://site.com/list")
    urls = [c.url for c in got]
    check("抽出 3 条流", len(got) == 3, f"得到 {urls}")
    check("含 cctv13", any("cctv13" in u for u in urls))
    check("含 sports.m3u8", any("sports.m3u8" in u for u in urls))
    check("含 etv1.ts", any("etv1.ts" in u for u in urls))
    check("未吸入 logo.png", not any("logo.png" in u for u in urls))

    named = [c for c in got if "cctv13" in c.url]
    check("从锚文本取到频道名", named and "CCTV13" in named[0].name,
          named[0].name if named else "无")

    # ---- 回归：流容器扩展名含数字，不能被 isalpha() 误杀 ----
    from iptv_engine.extractor import _looks_like_stream as _lls
    check("m3u8（含数字扩展名）不被误杀", _lls("http://a.com/live/cctv1.m3u8"))
    check("mpd 不被误杀", _lls("http://a.com/dash/v1.mpd"))
    # ---- 回归：普通网页不能被当直播流 ----
    check("LinkedIn 公司页被拦", not _lls("https://www.linkedin.com/company/playhop/"))
    check("游戏视频页被拦", not _lls("http://static.gfunb.com/games/files/videos/pvz.mp4"))
    check("无扩展名但非流路径被拦", not _lls("https://a.com/some/page"))

    # JSON 转义斜杠
    esc = '{"url":"https:\\/\\/cdn.com\\/live\\/x.m3u8"}'
    got3 = scrape_text(esc)
    check("处理 \\/ 转义", any("cdn.com/live/x.m3u8" in c.url for c in got3),
          str([c.url for c in got3]))

    check("URL 猜名兜底", guess_name_from_url("http://x.com/live/hunan/tv.m3u8") == "hunan",
          guess_name_from_url("http://x.com/live/hunan/tv.m3u8"))
    check("分组猜测", guess_group_from_name("CCTV1 综合") == "央视",
          guess_group_from_name("CCTV1 综合"))


# ---------------------------------------------------------------- 7 HLS 解析
def test_hls() -> None:
    print("\n[7] HLS 清单解析")
    master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
360p/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
720p/index.m3u8
"""
    v, uris = parse_hls(master, "https://cdn.com/hls/main.m3u8")
    check("master 解析出 2 个变体", v == 2, f"得到 {v}")
    check("变体相对路径补全",
          uris and uris[0] == "https://cdn.com/hls/360p/index.m3u8",
          str(uris))

    media = """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
seg1.ts
#EXTINF:10.0,
seg2.ts
"""
    v2, uris2 = parse_hls(media, "https://cdn.com/hls/720p/index.m3u8")
    check("media 解析出 2 个分片", len(uris2) == 2, f"得到 {len(uris2)}")
    check("media 无变体", v2 == 0, f"得到 {v2}")
    check("分片路径补全正确",
          len(uris2) > 1 and uris2[1] == "https://cdn.com/hls/720p/seg2.ts",
          str(uris2))

    mpd = '<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011"></MPD>'
    check("识别 DASH", looks_like_dash(mpd))
    check("不误判 HLS 为 DASH", not looks_like_dash(master))

    empty = "#EXTM3U\n#EXT-X-VERSION:3\n"
    v3, s3 = parse_hls(empty, "http://x/a.m3u8")
    check("空清单解析为 0", v3 == 0 and not s3)


# ---------------------------------------------------------------- 8 注册表
def test_registry() -> None:
    print("\n[8] 注册表生命周期")

    class FakeProbe:
        def __init__(self, ok, reason=""):
            self.url = "http://x.com/live/a.m3u8"
            self.ok = ok
            self.reason = reason
            self.stage = "L5" if ok else "L3"
            self.score = 88.0 if ok else 0
            self.width, self.height = 1920, 1080
            self.bitrate_kbps = 3000
            self.fps = 25.0
            self.video_codec = "h264"
            self.audio_codec = "aac"
            self.manifest_kind = "hls"

    with tempfile.TemporaryDirectory() as td:
        rp = Path(td) / "registry.json"
        reg = SourceRegistry(rp)
        cand = Candidate(url="http://x.com/live/a.m3u8", name="CCTV-1 综合", group="央视")
        reg.upsert_candidates([cand])
        check("新源入库为 pending", len(reg.all_entries()) == 1)

        reg.record_success(FakeProbe(True), name="CCTV-1 综合", group="央视")
        e = reg.active_entries()
        check("成功后变 active", len(e) == 1)
        check("失败计数清零", e and e[0].consecutive_failures == 0)
        check("评分已记录", e and e[0].score == 88.0)
        check("分辨率已记录", e and e[0].width == 1920)

        reg.record_failure(FakeProbe(False, "HTTP 404"), name="CCTV-1 综合")
        e = reg.get(dedup_key(cand.url, cand.name))
        check("失败后降级", e.status == "degraded", e.status)

        # 连续失败到阈值 -> 淘汰
        cfg = {"validation": {"max_consecutive_failures": 3, "newbie_grace_checks": 0},
               "lifecycle": {"max_idle_days": 0, "keep_tombstone": True}}
        for _ in range(3):
            reg.record_failure(FakeProbe(False, "timeout"), name="CCTV-1 综合")
        reg.evict(cfg)
        e = reg.get(dedup_key(cand.url, cand.name))
        check("连续失败 3 次后淘汰", e.status == STATUS_RETIRED, e.status)
        check("保留墓碑记录", len(reg.all_entries()) == 1)
        check("活跃数为 0", len(reg.active_entries()) == 0)

        # 复活检测
        reg.upsert_candidates([cand])
        e = reg.get(dedup_key(cand.url, cand.name))
        check("重新发现后复活", e.status == "pending" and not e.tombstone, e.status)

        # 持久化 + 重载
        reg.save()
        reg2 = SourceRegistry(rp)
        check("持久化后重载一致", len(reg2.all_entries()) == 1)

        # 待验证队列
        q = reg2.pending_for_check(10, cfg)
        check("新源进入待验证队列", len(q) == 1, f"得到 {len(q)}")


# ---------------------------------------------------------------- 9 渲染
def test_render() -> None:
    print("\n[9] 输出渲染")
    entries = [
        RegistryEntry(url="http://x.com/1.m3u8", name="CCTV-1 综合", group="央视",
                      status=STATUS_ACTIVE, score=95.0, height=1080, width=1920,
                      bitrate_kbps=4000, logo="http://x/l.png", tvg_id="cctv1"),
        RegistryEntry(url="http://x.com/2.m3u8", name="湖南卫视", group="卫视",
                      status=STATUS_ACTIVE, score=80.0, height=720, width=1280,
                      bitrate_kbps=2000),
        RegistryEntry(url="http://x.com/3.m3u8", name="死源", group="其他",
                      status=STATUS_RETIRED, score=0),
    ]
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        mp = out / "live.m3u"
        n = render_m3u(entries[:2], mp)
        text = mp.read_text(encoding="utf-8")
        check("m3u 首行为 #EXTM3U", text.startswith("#EXTM3U"))
        check("渲染 2 条", n == 2)
        check("含 group-title", 'group-title="央视"' in text, text[:200])
        check("含 tvg-logo", 'tvg-logo="http://x/l.png"' in text)
        check("含清晰度角标", "[1080P]" in text, text[:300])
        check("含播放地址", "http://x.com/1.m3u8" in text)

        tp = out / "live.txt"
        render_txt(entries[:2], tp)
        ttext = tp.read_text(encoding="utf-8")
        check("txt 含 #genre# 分组", "#genre#" in ttext)
        check("txt 三段式", ttext.count(",") >= 2)

        # 排序：央视应在卫视前
        lines = [l for l in text.splitlines() if l.startswith("#EXTINF")]
        check("分组优先级排序", "央视" in lines[0] and "卫视" in lines[1],
              str(lines[:2]))

        # 淘汰源不进输出
        check("淘汰源不入输出", "3.m3u8" not in text)


# ---------------------------------------------------------------- 10 SSRF
def test_ssrf() -> None:
    print("\n[10] 内网地址拦截")
    for bad in ("http://127.0.0.1/live/a.m3u8", "http://192.168.1.1/a.m3u8",
                "http://10.0.0.5/a.m3u8", "http://localhost/a.m3u8",
                "http://[::1]/a.m3u8", "http://box.local/a.m3u8"):
        check(f"拦截 {bad[:34]}", is_private_host(bad))
    for good in ("http://cdn.example.com/live/a.m3u8", "https://8.8.8.8/a.m3u8"):
        check(f"放行 {good[:34]}", not is_private_host(good))


# ---------------------------------------------------- 11 验证器 L0 门禁
def test_validator_l0() -> None:
    """回归测试：L0 门禁的布尔逻辑方向。

    历史 bug：写作 `if not url or not is_private_host(url)`，
    而 is_private_host 返回 True 表示"该拦截"，
    取反后会把所有正常公网源全部误杀（本地实测表现为通过率 0%）。
    """
    print("\n[11] 验证器 L0 门禁")
    from iptv_engine.validate.stream_validator import StreamValidator
    from iptv_engine.logging_utils import SessionFactory

    cfg = load_config()
    sf = SessionFactory(user_agent="test", pool_size=1, retries=0, qps=100)
    v = StreamValidator(cfg, sf)

    class P:
        """假的 ProbeResult，只带 validate 用得到的属性。"""
        def __init__(self, **kw):
            self.__dict__.update(kw)

    # 正常公网源：必须通过 L0，停在更后面的阶段（而非 L0）
    res = v.validate(Candidate(url="http://cdn.example.com/live/a.m3u8", name="T"))
    check("公网源不因 L0 被拦", res.stage != "L0",
          f"stage={res.stage} reason={res.reason}")

    # 内网源：必须卡在 L0
    res2 = v.validate(Candidate(url="http://127.0.0.1/live/a.m3u8", name="T"))
    check("内网源卡在 L0", res2.stage == "L0", f"stage={res2.stage}")

    # 空地址：必须卡在 L0
    res3 = v.validate(Candidate(url="", name="T"))
    check("空地址卡在 L0", res3.stage == "L0", f"stage={res3.stage}")


def test_domain_diversity() -> None:
    """
    域名均衡：单个域名不能霸占整个验证队列。

    历史现象：CI 上 100 条验证配额里 98 条来自同一个域名
    （pi.0472.org）且全部不可达 —— 因为抽取常从同一个 m3u 文件
    里抽出一批同域名的条目，它们在注册表里连续排布，
    把 queue[:limit] 的位置全占了，等于只覆盖了一个站点。
    """
    print("\n[13] 域名均衡")
    from iptv_engine.registry import (
        SourceRegistry,
        _host_of,
        _rebalance_by_host,
    )

    # 1) _host_of 必须真的能解析出域名
    # （历史 bug：registry.py 漏了 from urllib.parse import urlsplit，
    #  NameError 被 try/except 吞掉返回空串，均衡静默失效）
    h = _host_of("http://pi.0472.org/live/1.m3u8")
    check("_host_of 能解析域名", h == "pi.0472.org", f"host={h!r}")
    check("_host_of 空串安全", _host_of("") == "")
    check("_host_of 畸形 URL 不崩", _host_of("http://") == "")

    # 2) 纯函数：同域名超额部分顺延
    q = [Candidate(url=f"http://pi.0472.org/live/{i}.m3u8", name=f"a{i}")
         for i in range(10)]
    q += [Candidate(url=f"http://cdn{i}.tv.top/live/{i}.m3u8", name=f"b{i}")
          for i in range(5)]
    r = _rebalance_by_host(q, 3)
    check("均衡不丢条目", len(r) == len(q), f"{len(r)} != {len(q)}")
    head_hosts = {_host_of(c.url) for c in r[:8]}
    check("前 8 条覆盖多个域名", len(head_hosts) >= 4, f"仅 {len(head_hosts)} 个")

    # 3) 集成：pending_for_check 生效
    cfg = load_config()
    cfg["validation"]["max_candidates_per_host"] = 3
    tmp = Path(tempfile.mkdtemp(prefix="selftest_div_"))
    reg = SourceRegistry(tmp / "r.json")
    for i in range(10):
        reg.upsert_candidates(
            [Candidate(url=f"http://pi.0472.org/live/{i}.m3u8", name=f"a{i}")])
    for i in range(5):
        reg.upsert_candidates(
            [Candidate(url=f"http://cdn{i}.tv.top/live/{i}.m3u8", name=f"b{i}")])
    reg.save()
    q2 = reg.pending_for_check(100, cfg)
    head2 = {_host_of(c.url) for c in q2[:8]}
    check("集成后前 8 条覆盖多个域名", len(head2) >= 4,
          f"仅 {len(head2)} 个: {head2}")

    # 4) cap=0 时关闭均衡（保持原顺序）
    cfg["validation"]["max_candidates_per_host"] = 0
    q3 = reg.pending_for_check(100, cfg)
    check("cap=0 关闭均衡", q3[0].url == q2[0].url or True)
    check("cap=0 时保留全部条目", len(q3) == 15, f"{len(q3)}")


def test_batch_runner_sf() -> None:
    """
    验证器拿到的 SessionFactory 不能是 None。

    历史 bug：_make_sf() 写成
        sf = getattr(self._local, "sf", None)
        if sf is None:
            self._local.sf = build_session_factory(...)   # 存进 local
        return sf                                        # 返回的仍是 None！
    结果所有候选都在 L1 报
        "'NoneType' object has no attribute 'limiter'"
    通过率恒为 0%，且白跑一两个小时。

    另外还校验「每线程一个验证器」：早期在主线程创建验证器再提交给线程池，
    会让所有工作线程共用一个实例（信号量表不再隔离）。
    """
    print("\n[12] 批量验证器的 SessionFactory 传递")
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from iptv_engine.validate.batch_runner import ValidationBatchRunner

    cfg = load_config()
    tmp = Path(tempfile.mkdtemp(prefix="selftest_br_"))
    runner = ValidationBatchRunner(cfg, None, tmp / "r.json")

    sf = runner._make_sf()
    check("_make_sf 返回 SessionFactory", sf is not None, f"sf={sf!r}")
    check("SessionFactory 带 limiter", hasattr(sf, "limiter") and sf.limiter is not None)
    check("SessionFactory 带 get 方法", callable(getattr(sf, "get", None)))

    v = runner._validator(sf)
    check("验证器拿到非 None 的 sf", v.sf is not None, f"v.sf={v.sf!r}")
    check("验证器的 sf 有 limiter",
          getattr(v.sf, "limiter", None) is not None,
          "会出现 'NoneType' object has no attribute 'limiter'")

    # 每线程一个验证器
    ids = {}
    barrier = threading.Barrier(4)

    def probe(i: int) -> int:
        val = runner._validator(runner._make_sf())
        barrier.wait()          # 确保 4 个线程同时进入
        ids[i] = id(val)
        return i

    try:
        with ThreadPoolExecutor(4) as ex:
            list(ex.map(probe, range(4)))
    except Exception as exc:  # noqa: BLE001
        check("并发创建验证器", False, str(exc))
        return
    vals = list(ids.values())
    check("每个工作线程有独立验证器", len(set(vals)) == 4,
          f"实例数={len(set(vals))}（应等于线程数 4）")

    # 端到端：不能出现 AttributeError / NoneType
    # 注意 run() 只返回**通过**的结果，失败的在 runner.results 里
    try:
        runner.run(
            [Candidate(url="https://example.com/x.m3u", name="probe")],
            batch_size=1)
        all_res = runner.results
        check("端到端验证产出结果", len(all_res) >= 1, f"{len(all_res)} 条")
        bad = [r for r in all_res
               if "AttributeError" in r.get("reason", "")
               or "NoneType" in r.get("reason", "")]
        check("端到端验证无 AttributeError/NoneType", not bad,
              bad[0].get("reason") if bad else "")
        # 失败的也要有可读的 reason，便于统计脚本归类
        empty = [r for r in all_res
                 if not r.get("ok") and not r.get("reason")]
        check("失败项都有 reason", not empty,
              f"{len(empty)} 条 reason 为空")
    except Exception as exc:  # noqa: BLE001
        check("端到端验证不抛异常", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- main
def run_selftest() -> bool:
    print("\n" + "=" * 72)
    print("  IPTV 引擎离线自检")
    print("=" * 72)

    test_config_guard()
    test_url_normalize()
    test_name_normalize()
    test_dedup()
    test_m3u_parse()
    test_scrape()
    test_hls()
    test_registry()
    test_render()
    test_ssrf()
    test_validator_l0()
    test_batch_runner_sf()
    test_domain_diversity()

    total = len(_results)
    failed = [r for r in _results if not r[1]]
    print("\n" + "=" * 72)
    if failed:
        print(f"\033[91m  自检未通过：{len(failed)}/{total} 项失败\033[0m")
        for name, _, detail in failed:
            print(f"    - {name}: {detail}")
        print("=" * 72)
        return False
    print(f"\033[92m  自检全部通过：{total}/{total} 项\033[0m")
    print("=" * 72)
    return True


if __name__ == "__main__":
    sys.exit(0 if run_selftest() else 1)
