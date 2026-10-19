"""IPTV 自动发现引擎 —— 顶层包。"""

__version__ = "1.0.0"

from .config import load_config
from .extractor import Candidate, extract
from .logging_utils import get_logger, setup_logging
from .normalizer import dedup_candidates, dedup_key, normalize_name, normalize_url
from .registry import SourceRegistry

__all__ = [
    "load_config",
    "Candidate",
    "extract",
    "SourceRegistry",
    "dedup_candidates",
    "dedup_key",
    "normalize_url",
    "normalize_name",
    "get_logger",
    "setup_logging",
    "__version__",
]