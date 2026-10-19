"""
GitHub 发布器：把 output/ 与 sources/registry.json 提交回 GitHub 仓库，
让播放器通过 raw 直链订阅到每日更新的内容。

两种推送方式（按可靠性排序，自动降级）：
  1. Git over HTTPS —— 克隆/初始化 → 写入 → commit → push。最稳，能带大文件历史。
  2. GitHub REST API —— 用 Contents API 逐文件 PUT。需要 contents:write 权限。

【相比外站发布的关键优势】
在 GitHub Actions 内部运行时，以下两者都是自动就绪的，无需任何额外配置：
  - GITHUB_TOKEN       内置令牌，只要 workflow 声明 permissions: contents: write
  - GITHUB_REPOSITORY  形如 owner/repo 的仓库坐标，由 Actions 注入
所以整条发布链路是零 secret 配置的。

密钥全部来自环境变量，绝不写入日志或仓库。
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests

from ..logging_utils import get_logger

log = get_logger("publish.github")


class GitHubPublisher:
    """把产物提交到 GitHub 仓库。"""

    def __init__(self, repo: str, token: str, branch: str = "main",
                 commit_message: Optional[str] = None,
                 api_base: str = "https://api.github.com"):
        if not repo or "/" not in repo:
            raise ValueError("GitHub 仓库格式应为 'owner/repo'")
        self.repo = repo.strip("/")
        self.token = token
        self.branch = branch
        self.commit_message = commit_message or "chore: 自动更新直播源"
        self.api_base = api_base.rstrip("/")

    # ------------------------------------------------------------ Git 方式
    def _git_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "echo"
        return env

    def _run(self, args: List[str], cwd: Optional[Path] = None, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(
            args, cwd=str(cwd) if cwd else None, env=self._git_env(),
            capture_output=True, text=True, timeout=timeout, shell=False,
        )

    def publish_git(self, files: Dict[str, bytes], repo_dir: Optional[Path] = None) -> bool:
        """
        用 git 推送。files: {仓库内相对路径: 内容bytes}
        """
        if not self.token:
            log.error("缺少 GITHUB_TOKEN，无法推送")
            return False

        tmp = Path(repo_dir) if repo_dir else Path(tempfile.mkdtemp(prefix="gh_publish_"))
        created_tmp = repo_dir is None
        remote = f"https://{self.token}@github.com/{self.repo}.git"

        try:
            if created_tmp:
                tmp.mkdir(parents=True, exist_ok=True)

            # 已存在本地克隆 -> pull；否则 clone 或 init
            if (tmp / ".git").exists():
                r = self._run(["git", "pull", "--rebase", "origin", self.branch], cwd=tmp)
                if r.returncode != 0:
                    log.warning("pull 失败（可能远端无此分支）：%s", r.stderr.strip()[:200])
            else:
                if tmp.exists() and any(tmp.iterdir()):
                    shutil.rmtree(tmp, ignore_errors=True)
                r = self._run(["git", "clone", "--depth", "1", "--branch", self.branch,
                                remote, str(tmp)], timeout=180)
                if r.returncode != 0:
                    # 远端还没有该分支 -> 全新初始化并推送
                    log.info("clone 失败（%s），改为本地初始化并推送新分支",
                             r.stderr.strip()[:120])
                    tmp.mkdir(parents=True, exist_ok=True)
                    self._run(["git", "init", "-b", self.branch], cwd=tmp)
                    self._run(["git", "remote", "add", "origin", remote], cwd=tmp)

            # 写文件
            for rel, content in files.items():
                dest = tmp / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)

            # git 配置（容器里没有全局身份）
            self._run(["git", "config", "user.name", "IPTV-Bot"], cwd=tmp)
            self._run(["git", "config", "user.email",
                       "iptv-bot@users.noreply.github.com"], cwd=tmp)
            self._run(["git", "add", "-A"], cwd=tmp)

            msg = self.commit_message.format(date=time.strftime("%Y-%m-%d %H:%M:%S"))
            r = self._run(["git", "commit", "-m", msg], cwd=tmp)
            if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr).lower():
                log.warning("commit 异常: %s", r.stderr.strip()[:200])

            r = self._run(["git", "push", "-u", "origin", self.branch], cwd=tmp, timeout=180)
            if r.returncode != 0:
                log.error("push 失败: %s", (r.stderr or r.stdout).strip()[:400])
                return False

            log.info("已推送到 GitHub：%s@%s", self.repo, self.branch)
            return True

        except subprocess.TimeoutExpired:
            log.error("git 操作超时")
            return False
        except Exception as exc:  # noqa: BLE001
            log.error("git 推送异常: %s", exc)
            return False
        finally:
            if created_tmp and tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------ API 方式
    def publish_api(self, files: Dict[str, bytes], commit_message: Optional[str] = None) -> bool:
        """用 GitHub Contents API 逐文件提交（git 方式失败时的回退）。"""
        if not self.token:
            log.error("缺少 GITHUB_TOKEN，无法调用 API")
            return False

        s = requests.Session()
        s.headers.update({
            "User-Agent": "IPTV-Bot",
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        msg = (commit_message or self.commit_message).format(
            date=time.strftime("%Y-%m-%d %H:%M:%S"))
        ok_count = 0
        last_err = ""

        for rel, content in files.items():
            url = f"{self.api_base}/repos/{self.repo}/contents/{rel}"
            b64 = base64.b64encode(content).decode()

            # 先查是否已存在，拿 sha 用于更新
            sha = None
            try:
                r = s.get(url, params={"ref": self.branch}, timeout=20)
                if r.status_code == 200:
                    sha = r.json().get("sha")
            except Exception:  # noqa: BLE001
                pass

            body = {
                "content": b64,
                "message": msg,
                "branch": self.branch,
            }
            if sha:
                body["sha"] = sha
            try:
                r = s.put(url, json=body, timeout=45)
                if r.status_code in (200, 201):
                    ok_count += 1
                    continue
                last_err = f"PUT {rel} -> {r.status_code} {r.text[:150]}"
                log.debug("%s", last_err)
            except Exception as exc:  # noqa: BLE001
                last_err = f"PUT {rel} -> {exc}"

        if ok_count == len(files):
            log.info("API 方式推送完成：%d 个文件 → GitHub %s@%s",
                     ok_count, self.repo, self.branch)
            return True
        log.error("API 推送部分失败（%d/%d），最后错误：%s", ok_count, len(files), last_err)
        return False

    # ------------------------------------------------------------ 统一入口
    def publish(self, files: Dict[str, bytes]) -> bool:
        """先尝试 git，失败再走 REST API。"""
        if not self.token:
            log.error("未配置 GITHUB_TOKEN，跳过发布（产物已本地生成）")
            return False
        if self.publish_git(files):
            return True
        log.warning("git 推送失败，尝试 GitHub REST API 回退方案")
        return self.publish_api(files)

    # ------------------------------------------------------------ raw 链接
    def raw_urls(self, filenames: List[str]) -> Dict[str, str]:
        """生成播放器可直接订阅的 raw 直链。

        GitHub 的 raw 域名历史上用过 raw.githubusercontent.com（现重定向）
        与 github.com/{owner}/{repo}/raw/{branch}/{path} 两种形式，
        这里输出后者 —— 兼容性最好，VLC/TiviMate 都能直接拉。
        """
        return {
            fn: f"https://github.com/{self.repo}/raw/{self.branch}/output/{fn}"
            for fn in filenames
        }


def collect_output_files(out_dir: Path, registry_path: Path) -> Dict[str, bytes]:
    """收集要推送的文件内容。"""
    files: Dict[str, bytes] = {}
    for p in sorted(Path(out_dir).glob("*")):
        if p.is_file() and p.suffix in (".m3u", ".txt", ".json"):
            files[f"output/{p.name}"] = p.read_bytes()
    if Path(registry_path).exists():
        files["sources/registry.json"] = Path(registry_path).read_bytes()
    return files


def build_publisher(cfg: dict) -> Optional[GitHubPublisher]:
    """
    从配置 + 环境变量构造发布器，未配置则返回 None。

    在 GitHub Actions 内部运行时，GITHUB_TOKEN 与 GITHUB_REPOSITORY
    由平台自动注入，因此通常无需任何额外 secret 配置即可发布。
    """
    p = (cfg.get("publish", {}) or {}).get("github_repo", {}) or {}
    if not p.get("enabled", True):
        return None
    repo = p.get("repo", "")
    token = p.get("token", "")
    if not repo or not token:
        log.warning("GitHub 未配置完整（repo=%s token=%s），跳过发布",
                    "有" if repo else "无", "有" if token else "无")
        log.warning("本地运行时请设置：GITHUB_REPO=owner/repo 与 GITHUB_TOKEN=<pat>")
        return None
    return GitHubPublisher(
        repo=repo,
        token=token,
        branch=p.get("branch", "main"),
        commit_message=p.get("commit_message", "chore: 自动更新直播源 ({date})"),
        api_base=(cfg.get("github", {}) or {}).get("api_base", "https://api.github.com"),
    )