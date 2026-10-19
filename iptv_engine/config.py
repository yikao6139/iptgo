"""
配置加载器。

设计要点：
1. 加载 config/config.yaml，做深合并（支持 EXTRA_CONFIG 环境变量指向的覆盖文件）；
2. 做【白名单守卫】校验 —— 扫描配置中的所有字符串值，
   任何形如 http(s)://...m3u8 / .mpd / rtmp:// 的"疑似具体流地址"直接抛异常。
   这条守卫是"禁止白名单"原则的机器化落地，防止有人手滑把地址写进配置。
3. 从环境变量注入密钥（GITHUB_TOKEN / GITHUB_REPOSITORY），
   密钥永不落盘到配置或日志。
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any, Dict, List

import yaml

# 根目录：项目根 = 本文件的上上级
ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config" / "config.yaml"

# 疑似"具体流地址"的模式。命中即视为违反禁止白名单原则。
# 覆盖三类：
#   1) 带流容器后缀的地址（.m3u8/.mpd/.flv/.ts ...）
#   2) 典型直播路径（/live/ /hls/ /playlist 等）
#   3) rtsp:// 与 rtmp:// 一律视为流地址（这类协议几乎只用于直播流，
#      且通常没有文件后缀，无法靠后缀判断）
STREAM_ADDR_RE = re.compile(
    r"(?:"
    r"(?:rtsp|rtmp|rtmps)://[^\s\"'<>]+"
    r"|(?:https?)://[^\s\"'<>]+?"
    r"(?:\.(?:m3u8|m3u|mpd|flv|ts|aac|mp4)(?:\?[^\s\"'<>]*)?"
    r"|/(?:live|hls|playlist|stream|manifest|play)(?:/|\.[a-z0-9]{1,5}(?:\?|$))[^s\"'<>]*)"
    r")",
    re.IGNORECASE,
)

# 明确豁免的键（这些值是 API 端点/搜索入口，不是直播源）
EXEMPT_KEYS = {
    "endpoint",
    "api_base",
    "search_endpoint",
    "page_endpoint",
}

ALLOWED_EXEMPT_PREFIXES = (
    "https://api.github.com",
    "https://html.duckduckgo.com",
    "https://www.bing.com",
    "https://www.sogou.com",
)


class ConfigViolation(RuntimeError):
    """配置中出现了被禁止的硬编码流地址。"""


def _walk_strings(node: Any, path: str = "") -> List[tuple]:
    """深度遍历，产出 (键名, 字符串值, 路径) 三元组。"""
    out: List[tuple] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out.extend(_walk_strings(v, f"{path}.{k}" if path else str(k)))
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            out.extend(_walk_strings(v, f"{path}[{i}]"))
    elif isinstance(node, str):
        # 取最后一段作为键名（兼容 list 场景）
        key = path.rsplit(".", 1)[-1].split("[")[0]
        out.append((key, node, path))
    return out


def enforce_no_hardcoded_streams(cfg: Dict[str, Any]) -> None:
    """
    白名单守卫：禁止在配置里硬编码具体流地址。

    豁免：显式标记为 endpoint/api_base 的搜索与 API 入口。
    """
    violations: List[str] = []
    for key, value, path in _walk_strings(cfg):
        if key in EXEMPT_KEYS and value.startswith(ALLOWED_EXEMPT_PREFIXES):
            continue
        if STREAM_ADDR_RE.match(value.strip()):
            violations.append(f"  - {path}: {value}")
    if violations:
        raise ConfigViolation(
            "检测到配置中硬编码了具体 IPTV 流地址，违反「禁止白名单」原则：\n"
            + "\n".join(violations)
            + "\n请改为由 discovery 模块运行时动态发现。"
        )


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def load_config(path: str | os.PathLike | None = None) -> Dict[str, Any]:
    """
    加载配置。优先级：内置默认 < config.yaml < EXTRA_CONFIG 指向的覆盖文件 < 环境变量。
    """
    cfg_path = Path(path) if path else CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {cfg_path}")

    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    extra = os.environ.get("EXTRA_CONFIG")
    if extra and Path(extra).exists():
        with open(extra, "r", encoding="utf-8") as f:
            cfg = _deep_merge(cfg, yaml.safe_load(f) or {})

    # ---- 环境变量注入（密钥不落盘）----
    # 说明：GITHUB_TOKEN 服务两处用途 —— 发现模块的 API 限流配额，
    # 以及发布模块的推送权限，是同一个令牌。
    cfg["github"] = cfg.get("github", {})
    cfg["github"]["token"] = os.environ.get(cfg["github"].get("token_from_env", "GITHUB_TOKEN"), "")

    cfg["publish"] = cfg.get("publish", {})
    gh_pub = cfg["publish"].setdefault("github_repo", {})
    # Actions 中自动注入 GITHUB_REPOSITORY（形如 owner/repo）；
    # 本地运行时可退回读 GITHUB_REPO 以便手工指定目标仓库。
    gh_pub["repo"] = (os.environ.get(gh_pub.get("repo_from_env", "GITHUB_REPOSITORY"), "")
                      or os.environ.get("GITHUB_REPO", ""))
    gh_pub["token"] = (os.environ.get(gh_pub.get("token_from_env", "GITHUB_TOKEN"), "")
                       or os.environ.get("GH_TOKEN", ""))

    enforce_no_hardcoded_streams(cfg)
    return cfg


def get(cfg: Dict[str, Any], dotted: str, default: Any = None) -> Any:
    """按点号路径读取配置，如 get(cfg, 'validation.ffmpeg_timeout')。"""
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node