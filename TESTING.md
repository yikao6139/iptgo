# 本地测试指南

本文档说明如何在本地开发机上测试这套 IPTV 引擎，以及各环节的预期结果。

---

## 一、四层测试结构

| 层次 | 命令 | 需要网络 | 耗时 | 用途 |
|------|------|---------|------|------|
| **L1 离线自检** | `python main.py selftest` | 否 | < 5 秒 | 改代码后立刻跑，秒级反馈 |
| **L2 解码专项** | `python main.py l5test` | 否 | ~30 秒 | 用本地造的流验证 L5 门槛精准性 |
| **L3 网络体检** | `python main.py netcheck` | 是 | 30-60 秒 | 判断本机能跑哪些功能 |
| **L4 真实流程** | `python main.py run` | 是 | 2-10 分钟 | 端到端验证 |

想直接看产物效果用 `python main.py demo`（需 ffmpeg，不依赖外网）。

从 L1 开始，逐层往上。

一键跑完所有不需要网络的：

```bash
python main.py test --offline   # 4 个套件全跑
```

---

## 二、L1：离线自检（必跑）

```bash
pip install -r requirements.txt
python main.py selftest
```

**预期输出**：

```
========================================================================
  自检全部通过：78/78 项
========================================================================
```

覆盖范围：配置守卫、URL 归一化、频道名归一化、双重去重、M3U 解析、宽松抽取、
HLS 解析、注册表生命周期、输出渲染、内网拦截、验证器 L0 门禁。

这层**不碰网络**，所以 CI 上也会作为第一道关卡执行（`daily.yml` 的
"Offline self-test" 步骤）。改完代码先跑它，78 项全绿再往下走。

### 其余离线测试

```bash
python tests/e2e_test.py          # 全流程演练（模拟数据，验证模块间契约）
python tests/publish_test.py      # 发布链路（配置注入、raw 链接、文件收集）
python tests/l5_decode_test.py    # L5 解码专项（见下节，最有说服力）
```

三个都通过，说明代码本身没问题，剩下的差异只可能来自网络。

---

## 三、L2：解码专项测试（验证 L5 门槛是否精准）

```bash
python main.py l5test
```

这是**最有说服力的测试**：它不依赖外网，用 ffmpeg 现场生成一个真实的
6 秒 HLS 流（640x480 / 25fps 的彩条画面），起本地 HTTP 服务器，
然后把这几种流喂给验证器，检查判定是否准确：

| 用例 | 期望 | 验证的能力 |
|------|------|-----------|
| 真实可播 HLS 流 | **通过**，识别出 640x480 / fps / 编码 | L5 不误杀好源 |
| 僵尸流（清单合法、分片是垃圾字节） | **卡在 L5** | L5 能抓僵尸源 |
| 空清单（无任何 URI） | **卡在 L3** | L3 能抓空壳源 |
| 404 路径 | **卡在 L1** | 分级判定正确 |
| 内网地址 | **卡在 L0** | SSRF 防护未被测试绕过污染 |

**预期输出**：

```
[1] 真实可播 HLS 流 → 必须走到 L5 并通过
    stage=L5 ok=True 640x480 360kbps fps=25.0 codec=h264
  PASS  真实流通过 L5 解码验证
  PASS  识别出分辨率 640x480
...
  L5 专项测试全部通过：11/11 项
```

关键在于第一项 —— 它证明 L5 **不是"全部拦截"**，而是真的能解码出画面才放行。
如果这里 `ok=False`，说明验证逻辑坏了。

---

## 四、L3：网络体检（跑真实流程前必看）

本地网络环境和 GitHub Actions runner 差别很大 —— CI 上四通路全通，
本地可能 GitHub 被墙、搜索引擎超时。**先体检再决定怎么跑**：

```bash
python main.py netcheck
```

**预期输出**：

```
==========================================================================
  本地网络通路体检
==========================================================================
  可通  GitHub API       发现·github       1.48s
  可通  GitHub 网页      发现·meta         2.17s
  可通  GitHub Raw       发现·github       0.65s
  可通  Bing 搜索        发现·search       0.52s
  不通  DuckDuckGo       发现·search      32.36s
  ...
  汇总：5/6 个外部服务可达
==========================================================================
```

脚本会同时检查 ffmpeg，并给出对应建议。

### 常见问题与解法

**A. 报 `CERTIFICATE_VERIFY_FAILED`（GitHub 等站点全挂）**

原因：requests 默认用 certifi 的证书库，**不读操作系统证书库**。
本机装了 HTTPS 中间人（加速器/抓包工具）时会中招。

```bash
pip install truststore     # 自动改用系统证书库
python main.py netcheck    # 复测
```

已加入 `requirements.txt`，装依赖时自动带上。

**B. `ProxyError` / GitHub 频繁 502、403**

原因：本机装了 Steam++ / Watt Toolkit 之类工具，会注入
`HTTP_PROXY=http://127.0.0.1:xxxx` 指向本地加速代理，该代理对 GitHub
很不稳定。

解法：让引擎绕过它直连，在 `config/config.local.yaml` 的 `crawler` 段加：

```yaml
crawler:
  bypass_env_proxy: true
```

**C. GitHub API 返回 403 且提示 rate limit**

匿名访问配额只有 **60 次/小时**，且按 IP 计算——用加速代理的话所有用户
共享同一个 IP，配额很快耗尽。

解法（二选一）：
- 等配额重置（`rate_limit` 接口的 `reset` 字段会给出时间）
- 配置 `GITHUB_TOKEN` 环境变量，认证后配额升到 5000 次/小时

**D. 本机网络实在跑不动**

直接推上 GitHub Actions。云端 runner 有独立 IP、稳定网络、
5000 次/小时配额，四通路全通：

```bash
git push    # Actions 每天 02:00（北京时间）自动运行
```

本地跑不了的常见原因是加速代理与 GitHub 的兼容问题，这在
CI 环境不存在。

---

## 五、ffmpeg：验证质量的关键

### 为什么必须有

L5 是本引擎的核心门槛 —— 用 `ffprobe` 读音视频轨道、`ffmpeg` 真实抽帧，
专门过滤"HTTP 200 但播放器黑屏"的僵尸源。**没有 ffmpeg，验证止步于 L4**，
僵尸源会大量混入播放列表。

程序会自动降级并告警（不会崩），但你要清楚降级后验证强度变弱了：

```
WARNING | 未检测到 ffmpeg/ffprobe，自动关闭解码验证（L5）
```

### 查找优先级

引擎按以下顺序定位 ffmpeg，**便携部署优先于系统安装**：

1. 环境变量 `FFMPEG_BIN` / `FFPROBE_BIN`
2. **项目内便携目录**（零配置，推荐）
   - `ffmpeg/bin/ffmpeg.exe`
   - `ffmpeg/ffmpeg.exe`
   - `bin/ffmpeg.exe`
3. 系统 `PATH`
4. Windows 常见安装路径

### 便携部署（推荐，无需配 PATH）

把下载的 ffmpeg 解压到**项目根目录**下，形成这个结构：

```
项目根/
├── ffmpeg/
│   └── bin/
│       ├── ffmpeg.exe
│       └── ffprobe.exe
├── main.py
└── ...
```

引擎会自动发现它，无需任何配置：

```
HAS_FFMPEG = True
FFMPEG_BIN = G:\path\to\项目\ffmpeg\bin\ffmpeg.exe
```

> 注意：放进 `output/` 或 `sources/` 等目录会被发现模块当播放列表扫描，
> 所以请放在项目根的 `ffmpeg/` 子目录。

### 其它安装方式

```bash
winget install Gyan.FFmpeg     # Windows（装到 PATH）
choco install ffmpeg
# 或从 https://www.gyan.dev/ffmpeg/builds/ 下载
```

装到系统后**必须新开终端**，再跑 `python main.py netcheck` 确认。

也可以临时指定路径，不改任何文件：

```bash
FFMPEG_BIN=/path/to/ffmpeg FFPROBE_BIN=/path/to/ffprobe python main.py run
```

### 不要提交到 git

`ffmpeg/` 目录体积大（完整版 ~200MB），已在 `.gitignore` 中排除。
CI 上用 `apt install ffmpeg` 装，不需要把它传上去。

---

## 六、按体检结果裁剪配置

`config/config.local.yaml` 是**本地专用覆盖文件**，按本机网络裁剪通路：

```yaml
discovery_channels:
  meta: false      # GitHub 被拦时关掉
  github: false
  search: true     # 只留能通的
```

用它跑：

```bash
EXTRA_CONFIG=config/config.local.yaml \
  python main.py run --max-check 30 --skip-publish --registry /tmp/test.json
```

**按需修改这个文件**：
- GitHub 能通 → 把 `meta`/`github` 改回 `true`
- 想测完整解码验证 → `require_decode: true` 且先装好 ffmpeg
- 想测发布 → `publish.github_repo.enabled: true` 并设置环境变量

---

## 七、L4：真实流程测试

### 想看效果？用演示模式

如果外网挖不到活源（本地网络受限、或搜索到的源恰好都失效），
可以用演示模式验证渲染逻辑并拿到一份**格式规范、真实可播**的播放列表：

```bash
python main.py demo            # 生成 12 条真实 HLS 流并渲染到 output/
python main.py demo --serve    # 同上，且常驻服务器供本机 VLC 直接播放
```

它做的事：用 ffmpeg 现场生成 12 条带真实画面的 HLS 流（CCTV/卫视/体育等），
起本地 HTTP 服务器，然后走**完整的「注册 → 五级验证 → 渲染」链路**。

```
[2/5] 用 ffmpeg 生成 12 条真实 HLS 流…
      1/12  CCTV-1 综合        1280x720 @25fps
[4/5] 执行五级真实验证（ffprobe + ffmpeg 抽帧）…
      ✓ CCTV-1 综合        1280x720    81kbps h264 评分 80.0
[5/5] 渲染播放列表…
  已生成 live.m3u：12 个频道
  已生成 live.txt：12 个频道，7 个分组
```

产物格式与真实产出完全一致，可直接喂给播放器验证客户端兼容性。
注意这些流只在本机可播（端口是临时的），正式使用请跑真实流程。

### 命令模板

```bash
# 本机（自动裁剪通路、小批量、不推送）
EXTRA_CONFIG=config/config.local.yaml \
  python main.py run --max-check 30 --skip-publish --registry /tmp/test.json

# CI 等价（走线上全配置）
python main.py run --skip-publish

# 完整含发布（本地需先设环境变量）
export GITHUB_REPOSITORY=yourname/iptv-live
export GITHUB_TOKEN=ghp_xxxxx
python main.py run
```

### 关键参数

| 参数 | 作用 |
|------|------|
| `--max-check N` | 本轮最多验证 N 条，控制耗时 |
| `--skip-publish` | 不推送远端，只生成本地产物 |
| `--skip-discovery` | 只巡检已有源，不挖新源 |
| `--skip-validation` | 只发现不验证 |
| `--registry PATH` | 指定注册表路径（**测试时务必用它隔离**） |
| `--log-level DEBUG` | 详细日志 |

> ⚠️ **测试时务必用 `--registry` 指向临时路径**，否则会污染
> `sources/registry.json`（那是线上累积状态的核心）。

### 怎么读运行日志

```
[③] 发现阶段 → [④] 组队 → [⑤] 验证 → [⑥] 渲染/发布
```

**发现阶段**
```
启用的发现通路：search
[bing] 「免费直播源 m3u8 列表」p1 → 9 条结果
搜索通路共收集 36 个候选落地页
发现完成：原始 29 → 去内网 29 → 去重后 8 条候选
```
去重比例高（29→8）是正常的，同一篇文章里常有多个重复地址。

**验证阶段（最关键）**
```
已验证 8/8 | 通过 1 失败 7 | 通过率 12.5%
失败分布：L1=7
```
`失败分布` 告诉你源死在哪一级，可据此判断问题：

| 停留阶段 | 含义 | 排查方向 |
|---------|------|---------|
| `L0` | 地址非法/内网 | 正常，内网源被 SSRF 防护拦掉 |
| `L1` | 网络不通 | 源真挂了，或本机网络限制 |
| `L2` | 内容为空 | 源返回空响应 |
| `L3` | 清单为空 | 空 m3u8，典型僵尸源 |
| `L4` | 分片不可达 | 清单在但内容拉不到 |
| `L5` | 解码失败 | **真·僵尸源**，解码不出画面 |

**如果全是 `L0`** → 遇到过一个已修复的 bug（门禁布尔逻辑反转会拦截所有公网源），
现在应该看到 `L1`/`L3` 之类。

**通过率个位数是正常的** —— 搜索挖到的地址里本来就大部分是死的，
这正是五级门槛存在的意义。

---

## 八、验证产物

```bash
cat output/live.m3u      # 标准 M3U，检查 EXTINF 与 tvg 属性
cat output/live.txt      # 分组三段式
cat output/meta.json     # 数量、分组、画质分布
python main.py stats     # 注册表统计 + TOP20 频道
```

合格产物长这样：

```
#EXTM3U
#EXTINF:-1 tvg-id="31e5f63e1d8e" tvg-name="湖南卫视" group-title="卫视",湖南卫视 [1080P]
#EXTVLCOPT:http-referrer=https://example.com/page.html
http://cdn.example.com/live/hunan.m3u8
```

注意 `tvg-id` / `tvg-name` / `group-title` 齐全、`#EXTVLCOPT:http-referrer`
透传（部分源必须带 Referer 才能播）。

---

## 九、常见问题

**Q：本地跑要等好久？**
A：先用 `tests/network_check.py` 体检。GitHub 不通就用
`config/config.local.yaml` 关掉相关通路，别干等超时重试。

**Q：`输出 m3u 0 个频道`？**
A：本轮全验证失败，这是正常的（搜索挖到的源大多已失效）。
看日志的「失败分布」：`L1` 多说明网络限制，`L3/L4/L5` 多说明源质量差。
想看渲染效果用 `python main.py demo`。
另外**通过率个位数很正常** —— 严门槛本就是为了滤掉僵尸源。

**Q：码率低的源会被淘汰吗？**
A：不会。码率只影响排序评分，不作淘汰。实测发现 ffprobe 读 HLS 清单时
`bit_rate` 恒为 0（清单本身无此信息），只有落到具体分片才读得到，
而分片仅 2 秒左右读数剧烈波动 —— 拿它当硬门槛会误杀大量真实低码率源。
真正作硬门槛的是**分辨率**（读数稳定）和**能否解码出画面**。

**Q：报错 `ConfigViolation`？**
A：往 `config.yaml` 里硬编码了具体流地址。这是「禁止白名单」铁律的守卫，
删掉硬编码，改用 `discovery` 模块动态发现。

**Q：警告「未检测到 ffmpeg/ffprobe」，但我明明装了？**
A：跑 `python main.py netcheck` 看它搜到的是哪个路径。如果显示「缺失」，
检查：一是要放在**项目根的 `ffmpeg/bin/`** 下（不是 `output/` 里），
二是装到系统的要**新开终端**再跑（PATH 未刷新）。

**Q：能只测解码这一块吗？**
A：`python main.py l5test`。它用 ffmpeg 现场造真实 HLS 流，
不需要外网，20 秒内验证 L5 门槛是否精准。

**Q：测试会不会污染线上状态？**
A：会，所以务必加 `--registry /tmp/xxx/registry.json`。

**Q：想只测某一块？**

```bash
python main.py discover --export /tmp/c.json   # 只测发现
python main.py render                          # 只测渲染
python main.py stats                           # 只看注册表
python main.py validate --skip-publish         # 只测验证
```

---

## 十、CI 上跑什么

GitHub Actions 每日自动执行，与本地测试对应：

| CI 步骤 | 对应本地命令 |
|---------|-------------|
| Offline self-test | `python main.py selftest` |
| Run pipeline | `python main.py run` |
| Collect artifacts | `cat output/*` + `python scripts/collect_stats.py` |

CI 走 `config/config.yaml` 全配置、四通路全开、有 ffmpeg，
比本地环境宽松，所以本地测过基本不用再担心。

首次上线建议在 Actions 页签**手动触发一次**，观察完整日志再等定时。