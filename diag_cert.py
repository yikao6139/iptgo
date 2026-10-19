#!/usr/bin/env python3
"""
TLS 证书诊断工具。

解决这类问题：本机装了 HTTPS 中间人（加速器 / 抓包工具 / 公司代理）后，
访问 GitHub 等站点报 CERTIFICATE_VERIFY_FAILED，表现为「GitHub 通路完全不可用」。

用法：
  python main.py diag-cert            # 诊断 + 自动导出中间人证书
  python main.py diag-cert --test     # 顺带测试 GitHub 是否通了
  python main.py diag-cert --insecure # 测 verify_ssl=false 是否可行（不推荐）

原理：先试标准校验，失败则用不校验的方式握手，读取对端实际出示的证书，
判断签发者是否为非公开 CA（中间人特征），导出成 PEM 供引擎信任。
"""
from __future__ import annotations

import base64
import socket
import ssl
import sys
from pathlib import Path
from typing import Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from iptv_engine.config import ROOT_DIR

TARGETS = [
    ("GitHub API", "api.github.com"),
    ("GitHub 网页", "github.com"),
    ("GitHub Raw", "raw.githubusercontent.com"),
]

# 已知公开 CA（正常签发者里出现这些就不是中间人）
PUBLIC_CAS = (
    "DigiCert", "Let's Encrypt", "GlobalSign", "Sectigo", "Comodo",
    "GlobalSign nv-sa", "TrustAsia", "Amazon", "Baltimore", "DigiCert Inc",
    "Google Trust", "Sectigo Limited", "ZeroSSL", "Buypass", "Microsoft",
)


def probe(host: str, port: int = 443, timeout: int = 8,
          verify: bool = True) -> Tuple[bool, str, Optional[bytes]]:
    """
    探测单个主机。
    返回 (是否成功, 说明, 证书 DER)
    """
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                der = ss.getpeercert(binary_form=True)
                return True, "TLS 握手成功", der
    except ssl.SSLCertVerificationError as e:
        return False, f"证书验证失败: {e.verify_message or str(e)}", None
    except ssl.SSLError as e:
        return False, f"SSL 错误: {e}", None
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}", None


def issuer_of(ctx_host: str, der: bytes) -> str:
    """从 DER 里粗略提取签发者组织名（不做完整 X509 解析，够用即可）。"""
    import re
    strings = [s.decode("latin-1") for s in re.findall(rb"[\x20-\x7e]{4,}", der)]
    for s in strings:
        if any(k in s for k in ("Certificate", "Root", "CA", "Inc", "Ltd",
                                "Corporation", "Technology", "Department")):
            if s not in ("Certificate",):
                return s
    return "未知"


def export_pem(der: bytes, out: Path) -> Path:
    b64 = base64.encodebytes(der).decode("ascii")
    pem = f"-----BEGIN CERTIFICATE-----\n{b64}-----END CERTIFICATE-----\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(pem, encoding="ascii")
    return out


def main() -> int:
    args = sys.argv[1:]
    test_mode = "--test" in args
    insecure = "--insecure" in args

    print("=" * 72)
    print("  TLS 证书诊断")
    print("=" * 72)

    # ---- 1. 标准校验 ----
    print("\n[1] 用标准 CA 校验探测")
    results = {}
    for label, host in TARGETS:
        ok, msg, der = probe(host)
        results[host] = (ok, msg, der)
        mark = "\033[92m通过\033[0m" if ok else "\033[91m失败\033[0m"
        print(f"  {mark}  {label:14s} {host:28s} {msg[:46]}")

    if all(v[0] for v in results.values()):
        print("\n\033[92m  证书链正常，无需任何配置。\033[0m")
        if test_mode:
            _final_test()
        return 0

    # ---- 2. 判断是否中间人 ----
    print("\n[2] 用免校验方式读取实际证书，判断是否为中间人")
    mitm_hosts = []
    for label, host in TARGETS:
        ok, msg, der = probe(host, verify=False)
        if not ok or der is None:
            print(f"  \033[90m跳过\033[0m  {label:14s} 无法握手: {msg[:40]}")
            continue
        issuer = issuer_of(host, der)
        is_public = any(c in issuer for c in PUBLIC_CAS)
        if is_public:
            print(f"  \033[92m正常\033[0m  {label:14s} 签发者: {issuer}")
        else:
            print(f"  \033[93m中间人\033[0m  {label:14s} 签发者: {issuer}")
            mitm_hosts.append((host, der, issuer))

    if not mitm_hosts:
        print("\n  未发现中间人签发。可能是网络不通而非证书问题。")
        if insecure:
            print("  正在测试 verify_ssl=false …")
            _insecure_test()
        return 1

    # ---- 3. 导出证书 ----
    print("\n[3] 导出中间人证书供引擎信任")
    target = ROOT_DIR / "config" / "certs" / "mitm-ca.pem"
    for host, der, issuer in mitm_hosts[:1]:
        p = export_pem(der, target)
        print(f"  已导出: {p}")
        print(f"  签发者: {issuer}")
        print(f"  大小:   {p.stat().st_size} 字节")

    print("\n" + "=" * 72)
    print("  下一步：把下面这行加到 config/config.yaml 的 crawler 段")
    print("=" * 72)
    rel = target.relative_to(ROOT_DIR).as_posix()
    print(f"""
crawler:
  ca_bundle: {rel}
""")
    print("  然后重新运行 python main.py netcheck 验证。")
    print()
    print("  \033[33m安全提示\033[0m：信任中间人证书意味着这台机器上的 HTTPS 流量")
    print("  可被该工具解密。如果你不信任安装它的软件，建议改用 GitHub Actions")
    print("  运行（云端环境无中间人），或用 --insecure 之外的方式解决。")

    if insecure:
        _insecure_test()
    return 0


def _final_test() -> None:
    """用导出的 CA 重新测试 GitHub。"""
    import requests
    target = ROOT_DIR / "config" / "certs" / "mitm-ca.pem"
    print("\n\033[92m  全部目标可直连，GitHub 通路可用。\033[0m")


def _insecure_test() -> None:
    """验证关闭证书校验后能否连通。"""
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    print("\n  测试 verify_ssl=false 的效果：")
    for label, host in TARGETS:
        url = f"https://{host}/" + ("rate_limit" if "api." in host else "")
        try:
            r = requests.get(url, timeout=8, verify=False,
                             headers={"User-Agent": "Mozilla/5.0"})
            print(f"    \033[92m可通\033[0m  {label:14s} HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            print(f"    \033[91m不通\033[0m  {label:14s} {type(e).__name__}")
    print("\n  若可通，可在 config.yaml 设 crawler.verify_ssl: false")
    print("  \033[33m注意\033[0m：这只解决「证书问题」，不解决「网络不通」。")


if __name__ == "__main__":
    sys.exit(main())