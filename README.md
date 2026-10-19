# 📺 IPTV 自动发现引擎

一套**具备自主发现能力**的 IPTV 直播源整合系统。每天凌晨 2:00（北京时间）自动跑，
全网搜索新出现的直播源 → 真实可播放性验证 → 生成播放列表 → 提交回仓库，
播放器订阅同一个 raw 直链即可永远拿到最新内容。

---

## 一、它和"手工维护播放列表"的本质区别

绝大多数 IPTV 项目是**静态清单**：作者手写几百条地址，半年后 90% 已失效。
本引擎是**自持的采集系统**：

| 维度 | 传统清单 | 本引擎 |
|------|---------|--------|
| 源从哪来 | 人工抄写 | 每天从 GitHub / 元列表 / 搜索引擎动态发现 |
| 源有多少 | 固定上限 | 不设上限，能进多少进多少 |
| 失效处理 | 无人管 | 连续失败自动淘汰出播放列表 |
| 可用性判定 | 无 | ffprobe 真实解码出画面才算数 |
| 重复控制 | 无 | URL + 频道名双重归一化去重 |
| 维护成本 | 持续人工 | 零人工 |

---

## 二、四条铁律（贯穿全部代码）

### 1. 禁止白名单 —— 没有任何具体源地址写在代码里

除了冷启动种子（**awesome-iptv 类元列表仓库坐标**，它们本身不含流地址），
所有直播地址一律由 `discovery` 模块在运行时通过网络获得。

这条规则有**机器化守卫**：`config.py` 的 `enforce_no_hardcoded_streams()` 会扫描
配置里每一个字符串值，出现任何形如 `http(s)://...m3u8` / `rtmp://.../live/xxx`
的内容就直接抛 `ConfigViolation` 让程序崩掉。

```python
# 往 config.yaml 里手滑写一条地址试试 —— 会立刻炸
enforce_no_hardcoded_streams({"evil": "http://a.com/live/x.m3u8"})
# ConfigViolation: 检测到配置中硬编码了具体 IPTV 流地址，违反「禁止白名单」原则
```

### 2. 代码即猎手 —— 凌晨跑的是 Python，不是 AI

`discovery/` 下的模块在 CI 里自主调用 GitHub REST API、解析搜索引擎 HTML、
遍历仓库目录树。全过程无人参与，也没有任何"预先列好的源清单"。

### 3. 数量不设上限，质量设硬门槛

新源发现后必须连过**五级递进验证**才能入库，任何一级不过立即淘汰。

### 4. 动态注册表

`source/registry.json` 记录每条源的全部状态（发现时间 / 验证状态 /
连续失败次数 / 画质指标），**每日增删全部由程序自动完成，禁止人工维护**。

---

## 三、系统架构

```
                    ┌──────────────────────────────────────────┐
                    │       GitHub Actions（cron 0 18 * * *） │
                    │           = 北京时间 02:00                │
                    └────────────────┬─────────────────────────┘
                                     │
        ┌────────────────────────────▼─────────────────────────────┐
        │                    ① 发现阶段                            │
        │                                                          │
        │   ┌────────────┐  ┌─────────────┐  ┌───────────────┐     │
        │   │  meta      │→ │  github     │→ │  search       │     │
        │   │ 元列表递归 │  │ REST API 搜 │  │ 搜索引擎 HTML │     │
        │   │ 爬取(引路) │  │ 仓库+目录树 │  │ 结果页解析    │     │
        │   └────────────┘  └─────────────┘  └───────────────┘     │
        │         │ 产出新仓库坐标      │                │           │
        │         └───────► 回灌 ──────┘                │           │
        │                            ▼                   ▼           │
        │                    ┌───────────────────────────────┐       │
        │                    │  extractor  抽取器             │       │
        │                    │  M3U严格解析 + 宽松正则挖掘    │       │
        │                    │  跳转链还原 / 转义还原 / 净化   │       │
        │                    └──────────────┬────────────────┘       │
        └───────────────────────────────────│──────────────────────┘
                                            ▼
        ┌────────────────────────────────────────────────────────────┐
        │              ② 去重（normalizer.py）                        │
        │   URL 归一化：小写主机 / 去默认端口 / 排查询参数 / 剥追踪参数  │
        │   名称归一化：全角转半角 / 去装饰符 / 抹平 "CCTV-1"="CCTV1"   │
        │   dedup_key = sha1(url_norm | name_norm)                    │
        └────────────────────────────┬───────────────────────────────┘
                                     ▼
        ┌────────────────────────────────────────────────────────────┐
        │              ③ 五级递进验证（validate/）                   │
        │                                                            │
        │   L1 网络可达    TCP 连接 + HTTP 状态码                     │
        │   L2 内容真实    Content-Type 合理 + 体积非零                │
        │   L3 清单解析    #EXTM3U / 变体 / 分片（相对路径自动补全）   │
        │   L4 分片可达    真实拉取首个分片，确认 200/206 且非空       │
        │   L5 真实解码    ffprobe 读轨道 + ffmpeg 抽帧 ★核心门槛★    │
        │                                                            │
        │   → 输出：码率 / 分辨率 / 帧率 / 编码 / 音频声道 / 质量评分   │
        └────────────────────────────┬───────────────────────────────┘
                                     ▼
        ┌────────────────────────────────────────────────────────────┐
        │              ④ 注册表（registry.py）                        │
        │                                                            │
        │   PENDING ──通过──► ACTIVE ──失败──► DEGRADED ──连续失败N──► │
        │      ▲                              │                      │
        │      └────────── 复活检测 ◄──────────┘                      │
        │                                        RETIRED（留墓碑）     │
        │   淘汰：连续失败 ≥ N  或  长期未成功 > max_idle_days         │
        └────────────────────────────┬───────────────────────────────┘
                                     ▼
        ┌────────────────────────────────────────────────────────────┐
        │              ⑤ 渲染 + 发布                                  │
        │   output/live.m3u   标准 M3U（tvg-id/logo/group + 清晰度角标）│
        │   output/live.txt   分组三段式（DIYP 等国产播放器）          │
        │   output/meta.json  快照统计                                 │
        │                          ↓                                   │
        │   推送 GitHub 本仓（git 推送，失败自动降级 REST API）          │
        │   播放器订阅 raw 直链，URL 永久不变                          │
        └────────────────────────────────────────────────────────────┘
```

---

## 四、目录结构

```
.
├── main.py                      CLI 入口
├── config/config.yaml           全部配置（零硬编码源地址）
├── iptv_engine/
│   ├── config.py                配置加载 + 禁止白名单守卫
│   ├── extractor.py             抽取器（M3U解析 / 宽松挖掘 / 净化）
│   ├── normalizer.py            归一化 + 双重去重 + SSRF 防护
│   ├── registry.py              动态注册表 + 生命周期 + 淘汰
│   ├── renderer.py              m3u / txt / meta 渲染
│   ├── pipeline.py              六阶段编排
│   ├── ui/
│   │   ├── server.py            本地 Web 服务（仅标准库）
│   │   └── static/index.html    控制台前端（单文件，零依赖）
│   ├── logging_utils.py         会话工厂 / 限流 / 退避
│   ├── discovery/
│   │   ├── __init__.py          通路调度
│   │   ├── github.py            GitHub REST：搜索 + 目录树 + Contents
│   │   ├── meta_crawler.py      元列表递归爬取（冷启动引路）
│   │   ├── search_engine.py     搜索引擎 HTML 解析
│   │   └── base.py              抓取器（并发 / robots / 截断 / 限流）
│   ├── validate/
│   │   ├── stream_validator.py  五级验证器 + HLS 解析
│   │   └── batch_runner.py      并发批次调度 + 分批落盘
│   └── publish/
│       └── github.py            GitHub 发布（git 优先，REST API 兜底）
├── scripts/collect_stats.py     CI 统计脚本
├── tests/
│   ├── selftest.py              离线自检（78 项，无需网络）
│   ├── e2e_test.py              全流程演练（模拟数据）
│   ├── ui_test.py               Web 后端自动化测试（42 项）
│   ├── l5_decode_test.py        L5 解码专项（本地造真实流）
│   ├── logo_pipeline_test.py    台标链路验证
│   ├── logo_gen.py              台标合成（ffmpeg drawtext）
│   ├── demo_local.py            演示模式（生成可播流）
│   └── network_check.py         网络通路体检
├── sources/registry.json        ★ 动态注册表（程序自动维护）
└── .github/workflows/daily.yml  ★ 每日 2:00 定时任务
```

---

## 五、快速开始

### Web 控制台（推荐日常使用）

```bash
python main.py ui
```

浏览器会自动打开 `http://127.0.0.1:8765/`，提供五个面板：

| 面板 | 功能 |
|------|------|
| **总览** | 活跃源数、平均码率、分组/画质分布、产物文件、ffmpeg 状态 |
| **频道管理** | 表格浏览全部源，支持搜索、按状态/分组筛选、六种排序、分页、一键复制 |
| **运行控制** | 勾选跳过发现/验证/发布、设置验证上限，**实时日志流** |
| **产物预览** | 直接查看 live.m3u / live.txt / meta.json 内容，可下载 |
| **配置查看** | 只读展示当前生效的关键配置 |

界面还兼作**播放器订阅服务**：`http://127.0.0.1:8765/output/live.m3u`
可直接填进 VLC / TiviMate 本地测试。

> 零第三方依赖，只用 Python 标准库。默认仅监听 127.0.0.1，不对外网暴露。
> 端口被占用会自动往后找。`--no-browser` 可不自动开浏览器。

### 命令行测试

```bash
python main.py test --offline    # 一键跑完 6 个测试套件
python main.py netcheck          # 网络通路体检：判断本机能跑哪些功能
```

> 详细测试说明见 [TESTING.md](TESTING.md)

### 本地运行

```bash
pip install -r requirements.txt

python main.py selftest        # 离线自检（70 项，秒级）
python main.py discover        # 只做发现，看能捞到多少
python main.py run             # 完整跑一轮
python main.py stats           # 查看注册表统计 + TOP20
python main.py render          # 只重新渲染输出
```

### 部署到 GitHub（每日 2:00 自动运行）

1. 新建 GitHub 仓库并推送本项目
2. **无需配置任何 secret** —— GitHub Actions 会自动注入 `GITHUB_TOKEN`
   与 `GITHUB_REPOSITORY`，发布器直接可用
3. Actions 页签可**手动触发**补跑

> 唯一前提：workflow 已在文件头声明 `permissions: contents: write`（已配好），
> 这是允许内置令牌推送本仓库的必要权限。

<details>
<summary>本地手动发布（可选）</summary>

本地跑 `python main.py publish` 时需要自行提供两个环境变量：

```bash
export GITHUB_REPOSITORY=你的用户名/iptv-live   # 或 GITHUB_REPO
export GITHUB_TOKEN=ghp_xxxxxxxxxxxx             # 或 GH_TOKEN，需 contents:write 权限
python main.py publish
```

</details>

### 播放器订阅

推送成功后，订阅地址固定不变（每天仓库更新，播放器拉同一 URL 即得最新内容）：

```
https://github.com/你的用户名/iptv-live/raw/main/output/live.m3u
https://github.com/你的用户名/iptv-live/raw/main/output/live.txt
```

- **VLC**：媒体 → 打开网络串流
- **TiviMate / DIYP / APTV**：设置 → 播放列表 → 添加 URL
- 每天仓库更新，播放器重新拉取即得最新内容

---

## 六、关键设计说明

### 为什么"HTTP 200"不算数

僵尸源（返回 200 但内容为空、播放器黑屏）在 IPTV 生态里非常普遍。
所以 L5 用 `ffprobe` 读取音视频轨道，再用 `ffmpeg` 真实抽帧 —— 
**解不出画面就淘汰**，这是本系统区别于其它清单项目的核心。

### 相对路径的坑

HLS 播放列表里 URI 绝大多数是**相对路径**（`360p/index.m3u8`）。
只收绝对地址会导致所有 master/media 清单解析出 0 条 —— 这是实��开发中
已修复的 bug，`parse_hls()` 现在统一用 `urljoin` 解析。

### 跳转链还原

国内技术站（掘金/知乎/CSDN）大量用 `?target=<urlencoded>` 包装外链。
不还原的话入库全是跳转链，播放器直接失败。`unwrap_redirect()` 处理
`target/url/u/link/redirect/to/jump` 七种参数名。

### Demo 流过滤

技术博客里的 `big_buck_bunny.mp4`、`test-streams.mux.dev` 这些演示流
能播但对用户毫无价值。`LIVE_EXTENSIONS` 白名单 + `DEMO_HOSTS` 黑名单
把它们挡在入库之前。

### 搜索引擎与 robots

Bing 的 `robots.txt` 明确 `Disallow: /search`，若严格执行 robots 检查，
搜索引擎通路会**完全失效**。`ROBOTS_EXEMPT_HOSTS` 对搜索引擎入口做了
豁免（低频抓取公开检索页，不影响其业务），其余站点仍严格遵守 robots。

### 新手宽限

新发现的源可能因瞬时抖动失败。`newbie_grace_checks` 给新源额外的失败
容忍次数，避免刚发现就误杀。

### 复活检测

源被淘汰后（墓碑保留），若某天再次被发现，会自动清零失败计数并重新
进入验证队列 —— 很多源是"临时下线后恢复"。

### 并发与限流

- 全局 QPS 限流器，避免被目标封禁
- 每主机信号量（默认 3 并发），防止打爆单站点
- 分批落盘（每 300 条写一次），CI 中途被杀也不丢结果
- 注册表用临时文件 + `os.replace` 原子写，杜绝半截 JSON

---

## 七、可调参数速查

全部在 `config/config.yaml`：

| 参数 | 默认 | 含义 |
|------|------|------|
| `seed_repositories` | 3 个元列表 | 冷启动种子（唯一允许的硬编码） |
| `discovery.search_keywords` | 6 个 | 搜索关键词（不是地址） |
| `discovery.meta_list_depth` | 2 | 元列表递归深度 |
| `validation.max_consecutive_failures` | 3 | 连续失败几次淘汰 |
| `validation.newbie_grace_checks` | 2 | 新源额外失败容忍 |
| `validation.require_decode` | true | 是否强制 ffmpeg 解码验证 |
| `validation.min_video_kbps` | 120 | 最低码率门槛 |
| `lifecycle.max_idle_days` | 7 | 多久没成功就淘汰 |
| `lifecycle.max_check_per_run` | 4000 | 单轮验证上限（控制 CI 时长） |
| `output.group_priority` | 央视优先 | 播放列表排序 |

调高 `max_check_per_run` 能验证更多源，但 CI 时长也会增加（Actions 单任务上限 6 小时）。

---

## 八、许可

本项目仅用于技术学习与个人订阅。直播源版权归原权利人所有，
使用者需自行承担相应责任。请遵守各源站点的服务条款。