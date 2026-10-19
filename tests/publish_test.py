"""发布链路验证：配置注入、raw 链接、文件收集、降级逻辑。"""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# --- 1. 模拟 Actions 环境：GITHUB_REPOSITORY + GITHUB_TOKEN 自动注入 ---
os.environ["GITHUB_REPOSITORY"] = "myuser/iptv-live"
os.environ["GITHUB_TOKEN"] = "ghp_faketoken123"
os.environ.pop("GITHUB_REPO", None)
os.environ.pop("GH_TOKEN", None)

from iptv_engine.config import load_config
from iptv_engine.publish import build_publisher, collect_output_files
from iptv_engine.publish.github import GitHubPublisher

cfg = load_config()
pub_cfg = cfg["publish"]["github_repo"]
print("1. 配置注入")
print(f"   repo  = {pub_cfg['repo']!r}   (来自 GITHUB_REPOSITORY)")
print(f"   token = {pub_cfg['token'][:12]}...  (来自 GITHUB_TOKEN)")
print(f"   branch= {pub_cfg['branch']!r}")
assert pub_cfg["repo"] == "myuser/iptv-live"
assert pub_cfg["token"] == "ghp_faketoken123"
assert "gitee" not in cfg["publish"], "publish 段不应再有 gitee 键"

# --- 2. 构建发布器 ---
pub = build_publisher(cfg)
print("\n2. 构建发布器")
assert pub is not None, "应能构建出发布器"
print(f"   {type(pub).__name__}(repo={pub.repo}, branch={pub.branch})")
print(f"   api_base = {pub.api_base}")

# --- 3. raw 链接 ---
print("\n3. raw 直链生成")
urls = pub.raw_urls(["live.m3u", "live.txt"])
for fn, u in urls.items():
    print(f"   {fn:10s} -> {u}")
assert urls["live.m3u"] == "https://github.com/myuser/iptv-live/raw/main/output/live.m3u"
assert "gitee" not in str(urls)

# --- 4. 文件收集 ---
print("\n4. 待提交文件收集")
tmp = Path(tempfile.mkdtemp())
out = tmp / "output"; out.mkdir()
reg = tmp / "sources"; reg.mkdir()
(out / "live.m3u").write_text("#EXTM3U\n", encoding="utf-8")
(out / "live.txt").write_text("央视,#genre#\n", encoding="utf-8")
(out / "meta.json").write_text("{}", encoding="utf-8")
(out / "should_skip.log").write_text("x", encoding="utf-8")   # 不该被收
(reg / "registry.json").write_text('{"sources":[]}', encoding="utf-8")

files = collect_output_files(out, reg / "registry.json")
for k in sorted(files):
    print(f"   {k:28s} {len(files[k])} bytes")
assert set(files) == {"output/live.m3u", "output/live.txt",
                      "output/meta.json", "sources/registry.json"}
assert "output/should_skip.log" not in files

# --- 5. 降级：无 token 时应安全返回 None ---
print("\n5. 无凭据时的降级行为")
cfg2 = load_config()
cfg2["publish"]["github_repo"]["token"] = ""
assert build_publisher(cfg2) is None
print("   token 为空 -> build_publisher 返回 None（跳过发布，不崩溃）")

# --- 6. 禁用开关 ---
cfg3 = load_config()
cfg3["publish"]["github_repo"]["enabled"] = False
assert build_publisher(cfg3) is None
print("   enabled=false -> 返回 None")

# --- 7. 仓库坐标格式校验 ---
print("\n7. 非法仓库坐标校验")
try:
    GitHubPublisher(repo="bad-format-no-slash", token="t")
    print("   ✗ 未拦截")
except ValueError as e:
    print(f"   ✓ 拦截: {e}")

# --- 8. 本地手工发布变量回退 ---
print("\n8. 本地手工发布的变量回退")
for k in ("GITHUB_REPOSITORY", "GITHUB_TOKEN"):
    os.environ.pop(k, None)
os.environ["GITHUB_REPO"] = "local/repo"
os.environ["GH_TOKEN"] = "gh_local"
cfg4 = load_config()
print(f"   GITHUB_REPO  -> {cfg4['publish']['github_repo']['repo']}")
print(f"   GH_TOKEN     -> {cfg4['publish']['github_repo']['token']}")
assert cfg4["publish"]["github_repo"]["repo"] == "local/repo"
assert cfg4["publish"]["github_repo"]["token"] == "gh_local"

print("\n✓ 发布链路全部验证通过")