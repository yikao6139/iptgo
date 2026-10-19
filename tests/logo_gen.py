"""
台标（logo）生成器：为演示频道生成 tvg-logo 图片。

为什么需要它：
  真实流程里 tvg-logo 来自上游 m3u 的 `tvg-logo` 属性，但演示模式
  生成的是全新频道，没有上游 logo。缺台标的频道在 VLC/TiviMate 里
  只能显示文字，观感很差。

实现：用 ffmpeg 的 drawtext 滤镜把频道名画成 PNG。
  - 只依赖 ffmpeg（项目已要求），不引入 Pillow 等第三方库
  - drawtext 基于 libfreetype，中文台标需要指定中文字体
  - Windows 下 ffmpeg 对非 ASCII 路径有编码坑，需先复制到临时目录
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

# 台标配色：(背景渐变上色, 背景渐变下色, 文字色)
LOGO_THEMES = {
    "央视": ((0xC8, 0x1E, 0x1E), (0x8B, 0x0A, 0x0A), (255, 255, 255)),
    "卫视": ((0x1E, 0x63, 0xB8), (0x0B, 0x3D, 0x7A), (255, 255, 255)),
    "体育": ((0x14, 0x8F, 0x3C), (0x08, 0x5C, 0x22), (255, 255, 255)),
    "影视": ((0x6A, 0x1B, 0x9A), (0x3F, 0x0D, 0x5C), (255, 255, 255)),
    "新闻": ((0x0F, 0x76, 0x6E), (0x08, 0x4A, 0x45), (255, 255, 255)),
    "少儿": ((0xF0, 0x8A, 0x24), (0xB8, 0x5E, 0x0C), (255, 255, 255)),
    "国际": ((0x37, 0x47, 0x4F), (0x21, 0x29, 0x30), (255, 255, 255)),
    "其他": ((0x54, 0x5A, 0x64), (0x32, 0x36, 0x3C), (255, 255, 255)),
}

# 中文字体候选（Windows 优先，Linux 兜底）
FONT_CANDIDATES_WIN = [
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
]
FONT_CANDIDATES_NIX = [
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def find_cjk_font() -> Optional[Path]:
    """找一个支持中文的字体，返回路径。"""
    cands = FONT_CANDIDATES_WIN + FONT_CANDIDATES_NIX
    for p in cands:
        if Path(p).exists():
            return Path(p)
    return None


def _hex(c: tuple) -> str:
    return "0x%02x%02x%02x" % c


def make_logo(ffmpeg_bin: str, name: str, group: str,
              out_dir: Path, slug: str,
              width: int = 200, height: int = 60) -> Optional[Path]:
    """
    生成单个台标 PNG。

    ffmpeg 的 drawtext 在 Windows 下无法直接读取含中文的绝对路径
    （libfreetype 用窄字符打开），所以先把字体复制到临时目录再用相对名引用。
    """
    font = find_cjk_font()
    if font is None:
        return None

    top, bottom, fg = LOGO_THEMES.get(group, LOGO_THEMES["其他"])

    # 台标文字：控制长度，过长会溢出（用频道名的前 6 个字符）
    label = (name or "TV").strip()
    label = label.replace(" ", "")
    if len(label) > 6:
        label = label[:6]
    # drawtext 里需要转义 : ' \
    label = label.replace("\\", "").replace(":", " ").replace("'", "").replace("%", "")

    fontsize = max(16, int(height * 0.42))
    # 用 sstep 做垂直渐变：上 60% 底色一，下 40% 底色二
    grad = f"0x{top[0]:02x}{top[1]:02x}{top[2]:02x}"

    vf = (
        # 底部渐变条
        f"drawbox=x=0:y={int(height*0.62):d}:w={width}:h={int(height*0.38):d}:"
        f"color={_hex(bottom)}:t=fill,"
        # 外描边，提升对比度
        f"drawtext=fontfile='FONT':text='{label}':fontcolor={_hex(fg)}:"
        f"fontsize={fontsize}:x=(w-text_w)/2:y=(h-text_h)/2-2:borderw=2:"
        f"bordercolor=0x000000@0.55"
    ).replace("FONT", font.name)

    # 字体复制到输出目录，ffmpeg 用相对名引用（规避 Windows 编码问题）
    font_dst = out_dir / font.name
    if not font_dst.exists():
        try:
            shutil.copy(font, font_dst)
        except Exception:  # noqa: BLE001
            return None

    out_png = out_dir / f"{slug}.png"
    try:
        subprocess.run([
            ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi",
            "-i", f"color=c={_hex(top)}:size={width}x{height}:d=1",
            "-vf", vf,
            "-frames:v", "1",
            str(out_png),
        ], check=True, capture_output=True, timeout=30, cwd=str(out_dir))
    except Exception:  # noqa: BLE001
        return None

    return out_png if out_png.exists() and out_png.stat().st_size > 0 else None


def make_all(ffmpeg_bin: str, channels: List[tuple], out_dir: Path) -> Dict[str, Path]:
    """
    批量生成台标。

    channels: [(slug, 频道名, 分组), ...]
    返回 {slug: png路径}
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    result: Dict[str, Path] = {}
    for slug, name, group in channels:
        p = make_logo(ffmpeg_bin, name, group, out_dir, slug)
        if p:
            result[slug] = p
    return result


if __name__ == "__main__":
    # 自测：生成几个样例
    import sys

    ffmpeg = sys.argv[1] if len(sys.argv) > 1 else "ffmpeg"
    tmp = Path(tempfile.mkdtemp(prefix="logo_test_"))
    got = make_all(ffmpeg, [
        ("a", "CCTV-1", "央视"),
        ("b", "湖南卫视", "卫视"),
        ("c", "CHC", "影视"),
    ], tmp)
    for slug, p in got.items():
        print(f"{slug}: {p} ({p.stat().st_size} bytes)")
    if not got:
        print("未生成任何台标（可能缺中文字体）")
    print(f"输出目录: {tmp}")