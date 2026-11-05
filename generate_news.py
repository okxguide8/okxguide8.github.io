#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
币圈快讯聚合生成器（重构版）

用法:
    python build_news.py                       # 用脚本同目录的 config.json
    python build_news.py --config my.json      # 指定配置
    python build_news.py --out ../site         # 指定输出目录（网站根目录）
    python build_news.py --latest-only         # 只更新 news.html，不生成当日归档
    python build_news.py --dry-run             # 只抓取+渲染，不写任何文件
    python build_news.py -v                    # 调试日志

设计要点:
    * 全部内容经 Jinja2 autoescape 渲染，杜绝 HTML 注入
    * 每条快讯生成站内详情页 article/<hash>.html，读者不离开本站；正文经白名单清洗
    * 单一数据源失败不影响整体；全部失败时优先复用上次成功快照，绝不编造内容
    * 同日多次运行按“合并去重”而非覆盖，抓取失败不会冲掉已有数据
    * 所有时间显式使用东八区，服务器时区无关
    * 写入为原子操作（tmp + os.replace），并跳过内容未变化的文件

退出码: 0 正常 / 1 无可用数据 / 2 配置错误
"""

from __future__ import annotations

import argparse
import base64
import gzip
import calendar
import hashlib
import html
import io
import json
import logging
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from html.parser import HTMLParser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover - 理论上不会发生
    Retry = None

from jinja2 import (ChoiceLoader, DictLoader, Environment, FileSystemLoader,
                    select_autoescape)

try:
    import feedparser
except ImportError:  # feedparser 缺失时降级：不抓 RSS 源，但不崩溃
    feedparser = None

try:
    from PIL import Image
except ImportError:  # 无 Pillow 时退化为存原图（体积更大）
    Image = None


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
VERSION = "2.3.0"

# 中国无夏令时，用固定偏移避免 Windows 缺少 tzdata 时 ZoneInfo 报错
TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 NewsBot/" + VERSION
)

DEFAULT_NAV = [
    {"label": "🏠 首页", "href": "./index.html"},
    {"label": "📈 美股教程", "href": "./article-usstock.html"},
    {"label": "🔥 币圈快讯", "href": "./news.html"},
    {"label": "📚 往期归档", "href": "./news-archive.html"},
]

DEFAULTS: Dict[str, Any] = {
    "site": {
        "name": "Web3 Exchange Guide",
        "title_suffix": "Web3 导航",
        "base_url": "",
        "description": "精选全网加密货币行业核心动态、政策风向与项目进展，7x24h 多源聚合。",
        # full = 详情页展示采集到的全文；excerpt = 只展示摘要并引导回原文（更稳妥的版权策略）
        "content_mode": "full",
        # excerpt 模式下正文最多保留多少字
        "excerpt_chars": 220,
    },
    "exchanges": [],
    "rss_sources": [
        # 实测可用（2026-11）：PANews 中文快讯，单次约 100 条，带配图与正文
        {"name": "PANews", "url": "https://www.panewslab.com/rss.xml?lang=zh&type=NEWS"},
        # 英文源，可与中文源互补；不想要英文内容就删掉这一行
        {"name": "Cointelegraph", "url": "https://cointelegraph.com/rss"},
    ],
    "cryptocompare": {
        # 该接口自 2025 起要求 API Key；enabled 为 null 时自动判断：
        # 环境变量 CRYPTOCOMPARE_API_KEY 存在才启用
        "enabled": None,
        "url": "https://min-api.cryptocompare.com/data/v2/news/?lang=ZH",
    },
    "fetch": {
        "per_source_limit": 10,
        "max_items": 12,
        "max_items_per_day": 60,
        "timeout": (4, 10),
        "retries": 3,
        "workers": 4,
        # 相似度阈值：标题归一化后相似度 >= 该值视为同一条
        "dedup_ratio": 0.92,
    },
    "archive": {"max_entries": 90},
    "nav": DEFAULT_NAV,
    "output_dir": None,
    # 站点内资源目录（相对输出目录）。放在独立子目录，避免覆盖站点原有的 assets/style.css
    "paths": {"assets": "assets/newsbot"},
    "images": {
        "enabled": True,
        "max_width": 480,
        "max_height": 320,
        "quality": 82,
        "max_bytes": 3145728,      # 原图超过 3MB 直接放弃
        "workers": 6,
        "retention_days": 180,     # 0 = 永不清理
        "max_repeat": 2,           # 同一张图在本页最多显示几次
        # 识别源站“通用水印图/品牌图”：同一张图同时出现在多条快讯里，直接全部不展示
        "drop_shared": True,
        "shared_min_count": 3,     # 至少出现这么多次才可能是通用图
        "shared_ratio": 0.4,       # 且占“有图条目”的比例不低于该值
    },
}

log = logging.getLogger("newsbot")


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def setup_logging(verbose: bool = False) -> None:
    # Windows 控制台可能是 GBK，强制 UTF-8 输出，避免中文 emoji 触发 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    level = logging.DEBUG if verbose else logging.INFO
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(fmt)
    log.addHandler(console)

    try:
        log_dir = BASE_DIR / "logs"
        log_dir.mkdir(exist_ok=True)
        fh = RotatingFileHandler(
            log_dir / "build.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception as exc:  # 日志落盘失败不应阻断主流程
        log.warning("无法写入日志文件: %s", exc)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
KNOWN_KEYS = set(DEFAULTS.keys())


class ConfigError(RuntimeError):
    pass


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _is_safe_url(url: Any) -> bool:
    """只放行 http/https，挡住 javascript: / data: / file: 等伪协议注入。"""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _normalize_exchanges(raw: Any) -> List[Dict[str, str]]:
    """兼容两种写法：
       旧版 {"binance": {"name":..,"link":..,"badge":..}}
       新版 [{"name":..,"link":..,"badge":..}]
    """
    if isinstance(raw, dict):
        raw_list = [{"id": k, **v} for k, v in raw.items() if isinstance(v, dict)]
    elif isinstance(raw, list):
        raw_list = raw
    else:
        raw_list = []

    out: List[Dict[str, str]] = []
    for idx, item in enumerate(raw_list):
        if not isinstance(item, dict):
            log.warning("交易所配置第 %d 项格式非法，已跳过", idx + 1)
            continue
        name = str(item.get("name") or item.get("id") or "").strip()
        link = str(item.get("link") or "").strip()
        badge = str(item.get("badge") or "").strip()
        if not name or not link:
            log.warning("交易所配置第 %d 项缺少 name/link，已跳过: %s", idx + 1, item)
            continue
        if not _is_safe_url(link):
            log.warning("交易所「%s」的链接协议不合法，已跳过: %r", name, link)
            continue
        out.append({"name": name, "link": link, "badge": badge})
    return out


def load_config(path: Path) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"配置文件不是合法 JSON: {exc}") from exc
        except Exception as exc:
            raise ConfigError(f"读取配置文件失败: {exc}") from exc
        if not isinstance(cfg, dict):
            raise ConfigError("配置文件顶层必须是 JSON 对象")
        log.info("已加载配置: %s", path)
    else:
        log.warning("未找到配置文件 %s，使用内置默认配置（无返佣按钮）", path)

    # 兼容旧版：顶层直接是交易所映射
    if cfg and not (set(cfg.keys()) & KNOWN_KEYS):
        log.info("检测到旧版配置格式，自动按「交易所映射」解析")
        cfg = {"exchanges": cfg}

    merged = _deep_merge(DEFAULTS, cfg)
    merged["exchanges"] = _normalize_exchanges(merged.get("exchanges"))

    sources = []
    for s in merged.get("rss_sources") or []:
        url = s.get("url") if isinstance(s, dict) else None
        if _is_safe_url(url):
            sources.append({"name": str(s.get("name") or url), "url": url.strip()})
        else:
            log.warning("RSS 源地址不合法，已跳过: %r", url)
    merged["rss_sources"] = sources

    if not isinstance(merged.get("nav"), list) or not merged["nav"]:
        merged["nav"] = DEFAULT_NAV

    # requests 只认 (connect, read) 元组；JSON 里写 [4, 10] 是 list，必须转换
    merged["fetch"]["timeout"] = _normalize_timeout(merged["fetch"].get("timeout"))

    return merged


def _normalize_timeout(value: Any) -> Any:
    """把配置里的超时统一成 requests 可接受的 int 或 (connect, read) 元组。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, (list, tuple)):
        nums = []
        for v in value:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                nums.append(float(v))
        if len(nums) >= 2:
            return (nums[0], nums[1])
        if nums:
            return nums[0]
    return (4.0, 10.0)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class NewsItem:
    title: str
    url: str = ""
    source: str = ""
    published: datetime = field(default_factory=lambda: datetime.now(TZ))
    summary: str = ""
    image: str = ""          # 源站图片地址
    image_local: str = ""    # 本地化后的相对路径（如 assets/newsbot/img/ab12.jpg）
    content: str = ""        # 清洗后的正文 HTML（图片已替换为本地地址）
    show_image: bool = True  # 渲染时计算，不持久化
    shared_image: bool = False  # 是否被判定为源站通用图（水印/品牌图），不持久化

    def __post_init__(self) -> None:
        if self.published.tzinfo is None:
            self.published = self.published.replace(tzinfo=TZ)

    # --- 展示层 ------------------------------------------------------------
    @property
    def time_text(self) -> str:
        return self.published.astimezone(TZ).strftime("%Y-%m-%d %H:%M")

    @property
    def date_text(self) -> str:
        return self.published.astimezone(TZ).strftime("%Y-%m-%d")

    @property
    def time_hm(self) -> str:
        return self.published.astimezone(TZ).strftime("%H:%M")

    @property
    def word_count(self) -> int:
        return len(_TAG_RE.sub("", self.content or "").strip())

    @property
    def safe_url(self) -> str:
        return self.url if _is_safe_url(self.url) else ""

    @property
    def image_src(self) -> str:
        """优先本地化图片；没有本地文件时回落到源站地址；都没有则返回空串。"""
        if self.image_local:
            return "./" + self.image_local.lstrip("./")
        return self.image if _is_safe_url(self.image) else ""

    def image_src_up(self, up: str = "../") -> str:
        """供 article/ 子目录页面使用：把资源路径前移一层。"""
        if self.image_local:
            return up + self.image_local.lstrip("./")
        return self.image if _is_safe_url(self.image) else ""

    @property
    def key(self) -> str:
        return normalize_title(self.title)

    @property
    def slug(self) -> str:
        """稳定且可复现的详情页文件名（同一条新闻每次构建得到同一个地址）。"""
        return hashlib.sha1(self.key.encode("utf-8")).hexdigest()[:12]

    @property
    def href(self) -> str:
        """站内详情页相对路径。"""
        return f"article/{self.slug}.html"

    # --- 序列化 ------------------------------------------------------------
    def to_dict(self) -> Dict[str, str]:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "published": self.published.astimezone(TZ).isoformat(),
            "summary": self.summary,
            "image": self.image,
            "image_local": self.image_local,
            "content": self.content,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["NewsItem"]:
        title = str(data.get("title") or "").strip()
        if not title:
            return None
        try:
            published = datetime.fromisoformat(str(data.get("published")))
        except Exception:
            published = datetime.now(TZ)
        return cls(
            title=title,
            url=str(data.get("url") or ""),
            source=str(data.get("source") or ""),
            published=published,
            summary=str(data.get("summary") or ""),
            image=str(data.get("image") or ""),
            image_local=str(data.get("image_local") or ""),
            content=str(data.get("content") or ""),
        )


# --------------------------------------------------------------------------- #
# 文本处理
# --------------------------------------------------------------------------- #
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# \W 在 Unicode 模式下保留中日韩文字与字母数字，无需手写 \u4e00-\u9fa5 区间
_NORM_RE = re.compile(r"[\W_]+", re.UNICODE)


def normalize_title(title: str) -> str:
    return _NORM_RE.sub("", (title or "")).lower()


def clean_text(raw: Any, limit: Optional[int] = None) -> str:
    """剥标签 -> 还原 HTML 实体 -> 收敛空白 -> 按需截断。"""
    if raw is None:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", str(raw)))
    text = _WS_RE.sub(" ", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rstrip(" ，,。.、;；") + "…"
    return text


def is_similar(a: str, b: str, threshold: float) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    # 长度差异过大直接判不同，省掉不必要的比对
    if min(len(a), len(b)) / max(len(a), len(b)) < threshold - 0.15:
        return False
    return SequenceMatcher(None, a, b).ratio() >= threshold


# --------------------------------------------------------------------------- #
# 正文清洗（白名单）
# --------------------------------------------------------------------------- #
# 允许保留的标签：只放行结构化文本与段落，脚本/样式/表单/内联事件一律丢弃
_ALLOWED_TAGS = {
    "p", "br", "strong", "b", "em", "i", "u", "s",
    "blockquote", "ul", "ol", "li", "h3", "h4", "code", "pre", "img", "a",
}
# 这些标签连内部文字一起丢掉（否则会把 JS 源码当正文渲染出来）
_DROP_CONTENT_TAGS = {"script", "style", "iframe", "noscript", "svg", "form", "object", "embed"}
_VOID_TAGS = {"br", "img", "hr"}


class _BodySanitizer(HTMLParser):
    """把源站正文清洗成可安全内嵌的 HTML 片段。

    只保留白名单标签与属性：
      * <a> 仅保留 http/https 链接，强制 rel="noopener noreferrer nofollow"
      * <img> 仅保留 http/https 的 src，其余属性丢弃
      * 其他标签保留文字、丢弃标签外壳
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self._drop_depth = 0
        self._open: List[str] = []
        self.images: List[str] = []   # 正文里出现过的图片原始地址，供后续本地化

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS:
            self._drop_depth += 1
            return
        if self._drop_depth:
            return

        if tag == "img":
            src = ""
            for k, v in attrs:
                if k.lower() in ("src", "data-src", "data-original") and v:
                    src = v.strip()
                    if k.lower() == "src":
                        break
            if _is_safe_url(src):
                self.out.append(f'<img src="{html.escape(src, quote=True)}" alt="" loading="lazy" decoding="async">')
                self.images.append(src)
            return

        if tag == "a":
            href = ""
            for k, v in attrs:
                if k.lower() == "href" and v:
                    href = v.strip()
                    break
            if _is_safe_url(href):
                self.out.append(
                    f'<a href="{html.escape(href, quote=True)}" '
                    'target="_blank" rel="noopener noreferrer nofollow">'
                )
                self._open.append("a")
            return

        if tag in _VOID_TAGS:
            self.out.append(f"<{tag}>")
            return

        if tag in _ALLOWED_TAGS:
            self.out.append(f"<{tag}>")
            self._open.append(tag)

    def handle_startendtag(self, tag: str, attrs: List[tuple]) -> None:
        tag = tag.lower()
        if tag == "img":
            self.handle_starttag(tag, attrs)
        elif tag == "br":
            self.out.append("<br>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _DROP_CONTENT_TAGS:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if self._drop_depth:
            return
        if tag in _VOID_TAGS:
            return
        if tag in _ALLOWED_TAGS and tag in self._open:
            # 只闭合真正打开过的标签，避免源站标签不闭合导致结构错乱
            while self._open:
                top = self._open.pop()
                self.out.append(f"</{top}>")
                if top == tag:
                    break

    def handle_data(self, data: str) -> None:
        if self._drop_depth:
            return
        if not data.strip():
            # 段内空白保留一个空格，避免英文单词粘连
            if self.out and data:
                self.out.append(" ")
            return
        self.out.append(html.escape(data, quote=False))


def sanitize_body(raw: Any, max_chars: int = 6000) -> str:
    """清洗源站正文 HTML。返回可安全内嵌的片段；失败时回退为纯文本段落。"""
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""

    parser = _BodySanitizer()
    try:
        parser.feed(text)
        parser.close()
        # 源站常有未闭合标签，收尾时补齐，否则会吞掉后面所有内容
        while parser._open:
            parser.out.append(f"</{parser._open.pop()}>")
    except Exception as exc:  # 源站 HTML 畸形时不至于整篇丢失
        log.debug("正文解析失败，回退纯文本: %s", exc)
        plain = clean_text(text, limit=max_chars)
        return f"<p>{html.escape(plain)}</p>" if plain else ""

    fragment = "".join(parser.out)
    # 合并连续空行、去掉空段落
    fragment = re.sub(r"(?:<p>\s*</p>)+", "", fragment)
    fragment = re.sub(r"(?:<br>\s*){3,}", "<br><br>", fragment)
    fragment = fragment.strip()

    if not fragment:
        plain = clean_text(text, limit=max_chars)
        return f"<p>{html.escape(plain)}</p>" if plain else ""

    plain_len = len(re.sub(r"<[^>]+>", "", fragment))
    if plain_len > max_chars:
        # 超长正文截断，保留段落结构
        cut = clean_text(fragment, limit=max_chars)
        return f"<p>{html.escape(cut)}</p>"
    return fragment



# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def build_session(retries: int = 3) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept": "application/json, application/rss+xml, application/xml;q=0.9, */*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
    )
    if Retry is not None:
        retry = Retry(
            total=max(1, retries),
            connect=max(1, retries),
            read=max(1, retries),
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    return session


def fetch_json(session: requests.Session, url: str, timeout, extra_headers=None) -> Any:
    headers = dict(extra_headers or {})
    resp = session.get(url, timeout=timeout, headers=headers)
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "")
    if "json" not in ctype.lower() and not resp.text.lstrip().startswith(("{", "[")):
        raise ValueError(f"响应不是 JSON (Content-Type={ctype!r})")
    return resp.json()


# --------------------------------------------------------------------------- #
# 图片本地化
# --------------------------------------------------------------------------- #
_EXT_BY_CT = {
    "image/jpeg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _sniff_ext(data: bytes, ctype: str) -> str:
    """按文件头判断真实格式，比 Content-Type 可靠。"""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:3] == b"GIF":
        return ".gif"
    return _EXT_BY_CT.get((ctype or "").split(";")[0].strip().lower(), "")


def _make_thumb(data: bytes, img_cfg: Dict[str, Any]) -> Optional[bytes]:
    """生成统一尺寸的 JPEG 缩略图；无 Pillow 或解码失败时返回 None。"""
    if Image is None:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            im.thumbnail(
                (int(img_cfg["max_width"]), int(img_cfg["max_height"])), Image.LANCZOS
            )
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=int(img_cfg["quality"]), optimize=True)
            return buf.getvalue()
    except Exception as exc:
        log.debug("缩略图生成失败: %s", exc)
        return None


def _download_image(session: requests.Session, url: str, cfg: Dict[str, Any]) -> tuple[bytes, str]:
    resp = session.get(url, timeout=cfg["fetch"]["timeout"])
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "")
    if not ctype.lower().startswith("image/"):
        raise ValueError(f"非图片响应 ({ctype or 'unknown'})")
    data = resp.content
    if len(data) < 512:
        raise ValueError(f"图片过小 ({len(data)} 字节)，疑似无效图")
    if len(data) > int(cfg["images"]["max_bytes"]):
        raise ValueError(f"图片过大 ({len(data) // 1024} KB)")
    return data, ctype


_IMG_TAG_RE = re.compile(r"<img[^>]*>", re.I)
_IMG_SRC_ATTR_RE = re.compile(r'src="([^"]*)"', re.I)


def localize_images(
    items: Sequence[NewsItem],
    out_dir: Path,
    assets_rel: str,
    cfg: Dict[str, Any],
    session: requests.Session,
) -> tuple[int, int, Dict[str, str]]:
    """把源站图片（封面 + 正文配图）下载到本地，按内容哈希命名（同图只存一份）。

    返回 (新增数, 失败数, 源地址 -> 本地相对路径 映射)。
    """
    img_cfg = cfg["images"]
    if not img_cfg.get("enabled"):
        return 0, 0, {}

    img_rel = f"{assets_rel}/img"
    img_dir = out_dir / img_rel
    try:
        img_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        log.warning("无法创建图片目录 %s: %s", img_dir, exc)
        return 0, 0, {}

    mapping: Dict[str, str] = {}
    # URL -> 引用它的条目；已本地化但文件被清掉的，重新下载
    pending: Dict[str, List[NewsItem]] = {}
    for item in items:
        if item.image_local and (out_dir / item.image_local).exists():
            if _is_safe_url(item.image):
                mapping[item.image] = item.image_local
            continue
        item.image_local = ""
        if _is_safe_url(item.image):
            pending.setdefault(item.image, []).append(item)

    # 正文里的配图同样要落地，否则 Pages 上要么裂图要么被防盗链挡掉
    for item in items:
        for tag in _IMG_TAG_RE.findall(item.content or ""):
            m = _IMG_SRC_ATTR_RE.search(tag)
            url = (m.group(1).strip() if m else "")
            if _is_safe_url(url) and url not in mapping:
                pending.setdefault(url, [])

    if not pending:
        return 0, 0, mapping

    saved = failed = 0
    workers = max(1, min(int(img_cfg["workers"]), len(pending)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_download_image, session, url, cfg): url for url in pending}
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                data, ctype = fut.result()
            except Exception as exc:
                failed += 1
                log.debug("图片下载失败 %s: %s", url[:70], exc)
                continue

            thumb = _make_thumb(data, img_cfg)
            content = thumb if thumb is not None else data
            ext = ".jpg" if thumb is not None else (_sniff_ext(data, ctype) or ".jpg")
            name = f"{hashlib.sha1(content).hexdigest()[:16]}{ext}"
            path = img_dir / name

            if not path.exists():
                try:
                    path.write_bytes(content)
                    saved += 1
                except Exception as exc:
                    failed += 1
                    log.warning("图片写入失败 %s: %s", name, exc)
                    continue
            mapping[url] = f"{img_rel}/{name}"
            for item in pending[url]:
                item.image_local = f"{img_rel}/{name}"

    log.info("图片本地化: 新增 %d 张，失败 %d 张（共 %d 个唯一地址）", saved, failed, len(pending))
    return saved, failed, mapping


def rewrite_content_images(
    items: Sequence[NewsItem],
    mapping: Dict[str, str],
    up: str = "../",
    drop: Optional[set] = None,
) -> int:
    """把正文里的外链图片换成站内本地地址；下载失败或被判定为通用图的整段移除。"""
    replaced = dropped = 0
    drop = drop or set()

    for item in items:
        if not item.content:
            continue
        cover = item.image_local  # 顶部封面图已单独展示，正文里同一张不重复出

        def _repl(match: "re.Match[str]") -> str:
            nonlocal replaced, dropped
            src_match = _IMG_SRC_ATTR_RE.search(match.group(0))
            url = src_match.group(1).strip() if src_match else ""
            # 已经是站内相对地址（上次构建改写过的），原样保留；但通用图要清掉
            if url.startswith(("../", "./")):
                local_path = url.lstrip("./")
                if local_path in drop or url in drop or (cover and local_path == cover):
                    dropped += 1
                    return ""
                return match.group(0)
            local = mapping.get(url)
            if not local or local in drop or (cover and local == cover):
                dropped += 1
                return ""
            replaced += 1
            return (
                f'<img src="{html.escape(up + local.lstrip("./"), quote=True)}" '
                'alt="" loading="lazy" decoding="async">'
            )

        item.content = _IMG_TAG_RE.sub(_repl, item.content)

    if replaced or dropped:
        log.debug("正文配图: 本地化 %d 处，移除 %d 处", replaced, dropped)
    return replaced


def detect_shared_images(
    items: Sequence[NewsItem], min_count: int, ratio: float
) -> set:
    """识别源站的“通用图”——同一张图被多条快讯共用（通常是品牌水印或占位图）。

    判定条件：出现次数 >= min_count，且占「有图条目」的比例 >= ratio。
    命中后整页都不展示这张图，不是只压次数。
    """
    counts: Dict[str, int] = {}
    for item in items:
        key = item.image_local or item.image
        if key:
            counts[key] = counts.get(key, 0) + 1

    with_img = sum(counts.values())
    if with_img < 2 or not counts:
        return set()

    threshold = max(int(min_count), int(with_img * float(ratio)))
    shared = {key for key, num in counts.items() if num >= threshold}

    for item in items:
        item.shared_image = (item.image_local or item.image) in shared

    if shared:
        log.info(
            "识别到 %d 张源站通用图（最高复用 %d 次），已全部不展示",
            len(shared),
            max(counts[k] for k in shared),
        )
    return shared


def apply_image_repeat_limit(items: Sequence[NewsItem], limit: int) -> int:
    """同一张图（如源站通用占位图）在一页里最多显示 limit 次，其余改为无图卡片。"""
    if limit <= 0:
        limit = 10 ** 6
    seen: Dict[str, int] = {}
    hidden = 0
    for item in items:
        if item.shared_image:
            item.show_image = False
            continue
        src = item.image_src
        if not src:
            item.show_image = False
            continue
        seen[src] = seen.get(src, 0) + 1
        item.show_image = seen[src] <= limit
        if not item.show_image:
            hidden += 1
    if hidden:
        log.info("有 %d 条因配图重复改为无图卡片", hidden)
    return hidden


def prune_images(
    data_dir: Path, out_dir: Path, assets_rel: str, retention_days: int, today: datetime
) -> int:
    """删除不再被保留快照引用的旧图片；retention_days<=0 时跳过。"""
    if retention_days <= 0:
        return 0
    img_dir = out_dir / assets_rel / "img"
    if not img_dir.is_dir():
        return 0

    # 统计每张图最新被引用的日期
    latest: Dict[str, Any] = {}
    for snap in (data_dir / "snapshots").glob("*.json") if (data_dir / "snapshots").is_dir() else []:
        try:
            day = datetime.strptime(snap.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        try:
            with open(snap, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            continue
        for d in raw.get("items", []):
            local = str(d.get("image_local") or "")
            if not local:
                continue
            name = Path(local).name
            if name not in latest or day > latest[name]:
                latest[name] = day

    cutoff = (today - timedelta(days=retention_days)).date()
    removed = 0
    for path in img_dir.iterdir():
        if not path.is_file():
            continue
        ref = latest.get(path.name)
        if ref is None or ref < cutoff:
            try:
                path.unlink()
                removed += 1
            except Exception:
                pass
    if removed:
        log.info("清理过期图片 %d 张（保留 %d 天）", removed, retention_days)
    return removed


# --------------------------------------------------------------------------- #
# 数据源
# --------------------------------------------------------------------------- #
def fetch_cryptocompare(session: requests.Session, cfg: Dict[str, Any]) -> List[NewsItem]:
    cc_cfg = cfg.get("cryptocompare") or {}
    url = str(cc_cfg.get("url") or "")
    if not _is_safe_url(url):
        raise ValueError("CryptoCompare 接口地址不合法")
    limit = int(cfg["fetch"]["per_source_limit"])
    headers = {}
    api_key = os.environ.get("CRYPTOCOMPARE_API_KEY", "").strip()
    if api_key:
        headers["authorization"] = f"Apikey {api_key}"

    try:
        data = fetch_json(session, url, cfg["fetch"]["timeout"], headers)
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        if status in (401, 403):
            raise RuntimeError(
                "该接口现已要求 API Key（到 developers.coindesk.com 免费申请后"
                "设置环境变量 CRYPTOCOMPARE_API_KEY）"
            ) from exc
        raise

    if not isinstance(data, dict) or data.get("Response") == "Error":
        raise ValueError(f"接口返回异常: {str(data)[:120]}")

    items: List[NewsItem] = []
    for article in (data.get("Data") or [])[:limit]:
        if not isinstance(article, dict):
            continue
        title = clean_text(article.get("title"), limit=200)
        if not title:
            continue
        ts = article.get("published_on")
        try:
            published = datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None
        except (TypeError, ValueError, OSError):
            published = None
        if not published:  # 缺时间戳宁可跳过，也不要渲染成 1970 年
            log.debug("跳过无发布时间的条目: %s", title[:30])
            continue

        source_info = article.get("source_info") or {}
        body = str(article.get("body") or "")
        items.append(
            NewsItem(
                title=title,
                url=str(article.get("url") or ""),
                source=clean_text(source_info.get("name") or "CryptoCompare", limit=40),
                published=published,
                summary=clean_text(body, limit=120),
                image=str(article.get("imageurl") or ""),
                content=sanitize_body(body),
            )
        )
    return items


def _struct_to_dt(struct_time) -> Optional[datetime]:
    """feedparser 的 published_parsed 是 UTC struct_time。"""
    if not struct_time:
        return None
    try:
        stamp = calendar.timegm(struct_time)
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
    except Exception:
        return None


_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S %Z",
    "%Y-%m-%d",
)


def parse_date(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    text = str(raw).strip()
    # 去掉毫秒/多余时区后缀
    candidate = text.split(".")[0] if "." in text and "T" not in text else text
    for fmt in _DATE_FORMATS:
        for value in {text, candidate}:
            try:
                dt = datetime.strptime(value, fmt)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _rss_image(entry: Any) -> str:
    """按 media -> enclosure -> summary 内 <img> 的顺序找配图，优先大图。"""
    candidates: List[str] = []
    for key in ("media_content", "media_thumbnail"):
        value = entry.get(key)
        if isinstance(value, list):
            for m in value:
                if isinstance(m, dict):
                    candidates.append(str(m.get("url") or ""))
    for enc in entry.get("enclosures") or []:
        if isinstance(enc, dict) and str(enc.get("type", "")).startswith("image/"):
            candidates.append(str(enc.get("href") or enc.get("url") or ""))
    raw_summary = str(entry.get("summary") or "")
    if "<img" in raw_summary:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', raw_summary, re.I)
        if m:
            candidates.append(m.group(1))

    for url in candidates:
        if _is_safe_url(url):
            return url.strip()
    return ""


def _rss_body(entry: Any) -> str:
    """取 RSS 正文：优先 content:encoded，其次 description/summary。"""
    content = entry.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                value = str(block.get("value") or "").strip()
                if value:
                    return value
    for key in ("summary", "description"):
        value = str(entry.get(key) or "").strip()
        if value:
            return value
    return ""


def fetch_rss(session: requests.Session, source: Dict[str, str], cfg: Dict[str, Any]) -> List[NewsItem]:
    if feedparser is None:
        raise RuntimeError("未安装 feedparser，无法抓取 RSS（pip install feedparser）")

    timeout = cfg["fetch"]["timeout"]
    limit = int(cfg["fetch"]["per_source_limit"])

    resp = session.get(source["url"], timeout=timeout)
    resp.raise_for_status()

    parsed = feedparser.parse(resp.content)
    if getattr(parsed, "bozo", 0) and not parsed.entries:
        raise ValueError(f"RSS 解析失败: {getattr(parsed, 'bozo_exception', 'unknown')}")

    items: List[NewsItem] = []
    for entry in parsed.entries[: limit * 2]:
        title = clean_text(entry.get("title"), limit=200)
        if not title:
            continue
        published = (
            _struct_to_dt(entry.get("published_parsed"))
            or _struct_to_dt(entry.get("updated_parsed"))
            or parse_date(entry.get("published") or entry.get("updated"))
        )
        if not published:
            # 时间缺失就跳过，避免把旧闻伪装成“刚刚”
            log.debug("RSS 条目无可用时间，已跳过: %s", title[:30])
            continue

        link = entry.get("link") or ""
        source_name = clean_text(source.get("name") or "RSS", limit=40)
        body = _rss_body(entry)
        items.append(
            NewsItem(
                title=title,
                url=str(link),
                source=source_name,
                published=published,
                summary=clean_text(body, limit=120),
                image=_rss_image(entry),
                content=sanitize_body(body),
            )
        )
        if len(items) >= limit:
            break
    return items


# --------------------------------------------------------------------------- #
# 聚合
# --------------------------------------------------------------------------- #
def dedupe(items: Iterable[NewsItem], threshold: float) -> List[NewsItem]:
    """先按归一化标题精确去重，再对剩余项做相似度判重。"""
    exact: Dict[str, NewsItem] = {}
    for item in items:
        key = item.key
        if not key:
            continue
        exist = exact.get(key)
        # 同标题保留信息更完整/更早发布的那条
        if exist is None or item.published < exist.published:
            exact[key] = item
    candidates = sorted(exact.values(), key=lambda x: x.published, reverse=True)

    kept: List[NewsItem] = []
    for item in candidates:
        if any(is_similar(item.key, k.key, threshold) for k in kept):
            continue
        kept.append(item)
    return kept


def cryptocompare_enabled(cfg: Dict[str, Any]) -> bool:
    flag = (cfg.get("cryptocompare") or {}).get("enabled")
    if flag is None:  # auto：有 Key 才启用
        return bool(os.environ.get("CRYPTOCOMPARE_API_KEY", "").strip())
    return bool(flag)


def aggregate(cfg: Dict[str, Any], session: requests.Session) -> tuple[List[NewsItem], List[str]]:
    """并发抓取全部数据源。返回 (去重排序后的条目, 失败源描述)。"""
    tasks = []
    if cryptocompare_enabled(cfg):
        tasks.append(("CryptoCompare", fetch_cryptocompare, None))
    else:
        log.info("CryptoCompare 未启用（该接口现需 API Key，设置环境变量后可自动启用）")
    for src in cfg["rss_sources"]:
        tasks.append((src["name"], fetch_rss, src))

    if not tasks:
        return [], ["未配置任何可用数据源"]
    if len(tasks) == 1:
        log.warning("当前仅 1 个数据源，建议在 config.json 的 rss_sources 中至少再补 1 个，避免单点故障")

    collected: List[NewsItem] = []
    errors: List[str] = []
    workers = max(1, min(int(cfg["fetch"]["workers"]), len(tasks)))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for name, func, arg in tasks:
            if arg is None:
                fut = pool.submit(func, session, cfg)
            else:
                fut = pool.submit(func, session, arg, cfg)
            futures[fut] = name

        for fut in as_completed(futures):
            name = futures[fut]
            try:
                got = fut.result()
                collected.extend(got)
                log.info("数据源「%s」获取 %d 条", name, len(got))
                if not got:
                    errors.append(f"{name}(空)")
            except Exception as exc:
                errors.append(f"{name}({type(exc).__name__}: {exc})")
                log.warning("数据源「%s」抓取失败: %s", name, exc)

    items = dedupe(collected, float(cfg["fetch"]["dedup_ratio"]))
    items.sort(key=lambda x: x.published, reverse=True)
    return items[: int(cfg["fetch"]["max_items"])], errors


# --------------------------------------------------------------------------- #
# 快照（解决“重跑覆盖真实数据”与“全挂时编造新闻”）
# --------------------------------------------------------------------------- #
def snapshot_path(data_dir: Path, date_str: str) -> Path:
    return data_dir / "snapshots" / f"{date_str}.json"


def load_snapshot(path: Path) -> List[NewsItem]:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        out = []
        for d in raw.get("items", []):
            item = NewsItem.from_dict(d)
            if item:
                out.append(item)
        return out
    except Exception as exc:
        log.warning("读取快照失败 %s: %s", path, exc)
        return []


def save_snapshot(path: Path, date_str: str, items: List[NewsItem], degraded: bool) -> None:
    payload = {
        "date": date_str,
        "generated_at": datetime.now(TZ).isoformat(),
        "degraded": degraded,
        "items": [i.to_dict() for i in items],
    }
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))


def load_last_good(data_dir: Path) -> Optional[Dict[str, Any]]:
    path = data_dir / "last_good.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 文件写入
# --------------------------------------------------------------------------- #
def atomic_write(path: Path, content: str, skip_if_same: bool = True) -> bool:
    """原子写入；内容未变化则跳过。返回是否真正写入。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if skip_if_same and path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                log.debug("内容未变化，跳过写入: %s", path.name)
                return False
        except Exception:
            pass

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
        return True
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def build_env(template_dir: Path) -> Environment:
    if not template_dir.is_dir():
        raise ConfigError(f"模板目录不存在: {template_dir}")
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(enabled_extensions=("html", "xml"), default=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["truncate_desc"] = lambda s, n=120: clean_text(s, limit=n)
    return env


def render(env: Environment, template: str, **ctx: Any) -> str:
    return env.get_template(template).render(**ctx)


def common_context(cfg: Dict[str, Any], out_dir: Path, nav_current: str) -> Dict[str, Any]:
    site = cfg["site"]
    base = str(site.get("base_url") or "").rstrip("/")
    assets_rel = str((cfg.get("paths") or {}).get("assets") or "assets/newsbot").strip("/")
    return {
        "site": site,
        "base_url": base,
        "asset_prefix": f"./{assets_rel}",
        "year": datetime.now(TZ).year,
        "nav": cfg["nav"],
        "nav_current": nav_current,
        "exchanges": cfg["exchanges"],
        "crypto_disclaimer": (
            "本页内容由公开数据源自动聚合，仅供参考，不构成任何投资建议。"
            "数字资产价格波动剧烈，请自行评估风险。"
        ),
        "affiliate_disclosure": "本页含推广/返佣链接，通过该链接注册可能为我们带来收益。",
    }


# --------------------------------------------------------------------------- #
# 归档
# --------------------------------------------------------------------------- #
_DAILY_RE = re.compile(r"^news-(\d{4}-\d{2}-\d{2})\.html$")


def collect_archive(data_dir: Path, out_dir: Path, max_entries: int) -> List[Dict[str, Any]]:
    """以 data/snapshots 为准构建归档索引；老版本遗留的 html 也兼容纳入。"""
    entries: Dict[str, Dict[str, Any]] = {}

    snap_dir = data_dir / "snapshots"
    if snap_dir.is_dir():
        for path in snap_dir.glob("*.json"):
            date_str = path.stem
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                items = raw.get("items", [])
            except Exception:
                continue
            first_title = ""
            for d in items:
                t = clean_text(d.get("title"), limit=60)
                if t:
                    first_title = t
                    break
            entries[date_str] = {
                "date": date_str,
                "count": len(items),
                "title": first_title,
                "href": f"./news-{date_str}.html",
            }

    # 兼容旧脚本留下的 news-YYYY-MM-DD.html
    for path in out_dir.glob("news-*.html"):
        m = _DAILY_RE.match(path.name)
        if not m:
            continue
        date_str = m.group(1)
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        if date_str not in entries:
            entries[date_str] = {
                "date": date_str,
                "count": None,
                "title": "",
                "href": f"./{path.name}",
            }

    ordered = sorted(entries.values(), key=lambda x: x["date"], reverse=True)
    return ordered[:max_entries]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def copy_assets(assets_dir: Path, out_dir: Path, assets_rel: str) -> None:
    if not assets_dir.is_dir():
        log.warning("资源目录不存在，跳过: %s", assets_dir)
        return
    target = out_dir / assets_rel
    target.mkdir(parents=True, exist_ok=True)
    for src in assets_dir.iterdir():
        if not src.is_file():
            continue
        dst = target / src.name
        try:
            if dst.exists() and dst.read_bytes() == src.read_bytes():
                continue
            dst.write_bytes(src.read_bytes())
            log.debug("已同步静态资源: %s", dst.name)
        except Exception as exc:
            log.warning("同步静态资源失败 %s: %s", src.name, exc)


def apply_excerpt(items: Sequence[NewsItem], limit_chars: int) -> None:
    """只保留摘要长度正文，其余引导回原文（应对转载版权要求）。"""
    for item in items:
        if not item.content:
            continue
        plain = clean_text(item.content, limit=limit_chars)
        item.content = f"<p>{html.escape(plain)}</p>" if plain else ""


def render_articles(
    env: Environment,
    cfg: Dict[str, Any],
    items: Sequence[NewsItem],
    out_dir: Path,
    base_url: str,
    dry_run: bool,
) -> int:
    """为每条快讯生成站内详情页 article/<slug>.html，供列表页跳转。"""
    if not items:
        return 0
    assets_rel = str((cfg.get("paths") or {}).get("assets") or "assets/newsbot").strip("/")
    art_dir = out_dir / "article"
    written = 0

    for idx, item in enumerate(items):
        # 相关推荐：同批次的其它条目，避开自己
        related = [x for x in items if x.slug != item.slug][:3]
        meta_description = clean_text(
            item.summary or item.content, limit=150
        ) or f"{item.source} 于 {item.time_text} 发布的快讯。"

        og_image = ""
        if item.image_local and base_url:
            og_image = f"{base_url}/{item.image_local.lstrip('./')}"

        ctx = common_context(cfg, out_dir, nav_current="news")
        ctx.update(
            {
                "asset_prefix": f"../{assets_rel}",
                "nav_prefix": "../",
                "item": item,
                "related": related,
                "word_count": item.word_count,
                "meta_description": meta_description,
                "og_image": og_image,
                "canonical": f"{base_url}/{item.href}" if base_url else "",
            }
        )
        html_text = render(env, "article.html", **ctx)
        if dry_run:
            written += 1
            continue
        try:
            if atomic_write(art_dir / f"{item.slug}.html", html_text):
                written += 1
        except Exception as exc:
            log.warning("详情页写入失败 %s: %s", item.slug, exc)
    log.info("详情页: 生成/更新 %d 篇（共 %d 条）", written, len(items))
    return written


def prune_articles(data_dir: Path, out_dir: Path, retention_days: int, today: datetime) -> int:
    """删除不再被任何保留快照引用的详情页，避免无限增长。"""
    if retention_days <= 0:
        return 0
    art_dir = out_dir / "article"
    if not art_dir.is_dir():
        return 0

    cutoff = (today - timedelta(days=retention_days)).date()
    alive: set = set()
    snap_dir = data_dir / "snapshots"
    for snap in snap_dir.glob("*.json") if snap_dir.is_dir() else []:
        try:
            day = datetime.strptime(snap.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            continue
        try:
            with open(snap, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            continue
        for d in raw.get("items", []):
            title = str(d.get("title") or "")
            if title:
                alive.add(hashlib.sha1(normalize_title(title).encode("utf-8")).hexdigest()[:12])

    removed = 0
    for path in art_dir.glob("*.html"):
        if path.stem in alive:
            continue
        try:
            path.unlink()
            removed += 1
        except Exception:
            pass
    if removed:
        log.info("清理过期详情页 %d 篇（保留 %d 天）", removed, retention_days)
    return removed


def build(cfg: Dict[str, Any], out_dir: Path, dry_run: bool, latest_only: bool) -> int:
    data_dir = BASE_DIR / "data"
    env = build_env(BASE_DIR / "templates")
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")

    session = build_session(int(cfg["fetch"]["retries"]))
    fresh, errors = aggregate(cfg, session)

    max_per_day = int(cfg["fetch"]["max_items_per_day"])
    degraded = False
    banner = ""

    if fresh:
        # 与当日已有快照合并去重，重跑不会丢数据
        existing = load_snapshot(snapshot_path(data_dir, today))
        # 已本地化过的图片路径按标题继承，避免重复下载
        local_by_key = {i.key: i.image_local for i in existing if i.image_local}
        merged = dedupe(existing + fresh, float(cfg["fetch"]["dedup_ratio"]))
        merged.sort(key=lambda x: x.published, reverse=True)
        for item in merged:
            if not item.image_local:
                item.image_local = local_by_key.get(item.key, "")
        items = merged[:max_per_day]
        shown = items[: int(cfg["fetch"]["max_items"])]
        log.info("本次有效条目 %d 条（新抓 %d，当日累计 %d）", len(shown), len(fresh), len(items))
    else:
        # 全部数据源失败：优先当日快照，其次最近一次成功快照，最后空态；绝不编造内容
        items = load_snapshot(snapshot_path(data_dir, today))
        banner = "数据源暂时不可用，以下为今日已获取内容的最近一次留存。"
        if not items:
            last = load_last_good(data_dir)
            if last and last.get("items"):
                items = [i for i in (NewsItem.from_dict(d) for d in last["items"]) if i]
                banner = (
                    f"数据源暂时不可用，以下为 {last.get('date', '此前')} 的最后一次成功抓取结果。"
                )
        if not items:
            banner = "数据源暂时不可用，暂无内容可展示，请稍后刷新。"
        degraded = True
        shown = items[: int(cfg["fetch"]["max_items"])]
        log.error("全部数据源失败，已进入降级模式: %s", "; ".join(errors))

    if errors and fresh:
        log.warning("部分数据源失败: %s", "; ".join(errors))

    # 图片本地化：下载到 assets/newsbot/img，按内容哈希命名，同图只存一份
    assets_rel = str((cfg.get("paths") or {}).get("assets") or "assets/newsbot").strip("/")
    img_mapping: Dict[str, str] = {}
    if not dry_run and not degraded:  # 降级时网络本就不可用，不再浪费超时
        _, _, img_mapping = localize_images(shown, out_dir, assets_rel, cfg, session)

    # 源站通用图（水印/品牌图）识别：同一张图被多条共用时全部不展示
    img_cfg = cfg["images"]
    shared_local: set = set()
    if img_cfg.get("drop_shared", True):
        shared = detect_shared_images(
            shown,
            int(img_cfg.get("shared_min_count", 3)),
            float(img_cfg.get("shared_ratio", 0.4)),
        )
        # 正文里引用的是本地路径，只保留本地路径形态用于匹配
        shared_local = {p for p in shared if p.startswith(assets_rel)}
    if cfg["images"].get("enabled"):
        # 正文配图统一换成本地地址；通用图与下载失败的图整段移除
        rewrite_content_images(shown, img_mapping, up="../", drop=shared_local)
    apply_image_repeat_limit(shown, int(cfg["images"].get("max_repeat", 2)))

    # 快照必须在图片本地化之后写，否则记录不到 image_local
    if fresh and not dry_run:
        save_snapshot(snapshot_path(data_dir, today), today, items, degraded=False)
        atomic_write(
            data_dir / "last_good.json",
            json.dumps(
                {
                    "date": today,
                    "generated_at": now.isoformat(),
                    "items": [i.to_dict() for i in items[: int(cfg["fetch"]["max_items"])]],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )

    # 页面上下文
    site = cfg["site"]
    page_title = f"{today} 币圈实时快讯与行业动态"
    ctx = common_context(cfg, out_dir, nav_current="news")
    ctx.update(
        {
            "today": today,
            "generated_at": now.strftime("%Y-%m-%d %H:%M"),
            "items": shown,
            "count": len(shown),
            "banner": banner if degraded else "",
            "degraded": degraded,
            "page_title": page_title,
            "meta_description": clean_text(
                f"{today} 加密货币市场核心动态汇总，共 {len(shown)} 条，"
                "涵盖政策风向、项目进展与行情观察。",
                limit=150,
            ),
            "sources": sorted({i.source for i in shown if i.source}),
        }
    )

    # 0) 站内详情页：列表卡片点击后留在本站，不再跳到源站
    site_cfg = cfg.get("site") or {}
    if str(site_cfg.get("content_mode") or "full").lower() == "excerpt":
        apply_excerpt(shown, int(site_cfg.get("excerpt_chars") or 220))
        log.info("正文模式: excerpt（仅展示摘要，全文回源站）")
    render_articles(env, cfg, shown, out_dir, ctx["base_url"], dry_run)

    # 1) 当日归档页
    if not latest_only:
        ctx_arch = dict(ctx, canonical=f"{ctx['base_url']}/news-{today}.html" if ctx["base_url"] else "")
        html_daily = render(env, "daily.html", **ctx_arch)
        if dry_run:
            log.info("[dry-run] 将写入 news-%s.html (%.1f KB)", today, len(html_daily.encode("utf-8"))/1024)
        else:
            atomic_write(out_dir / f"news-{today}.html", html_daily)

    # 2) 稳定入口 news.html（始终为最新一期）
    ctx_latest = dict(ctx, canonical=f"{ctx['base_url']}/news.html" if ctx["base_url"] else "")
    html_latest = render(env, "daily.html", **ctx_latest)
    if dry_run:
        log.info("[dry-run] 将写入 news.html (%.1f KB)", len(html_latest.encode("utf-8"))/1024)
    else:
        atomic_write(out_dir / "news.html", html_latest)

    # 3) 归档索引
    entries = collect_archive(data_dir, out_dir, int(cfg["archive"]["max_entries"]))
    ctx_archive = common_context(cfg, out_dir, nav_current="archive")
    ctx_archive.update(
        {
            "entries": entries,
            "entry_count": len(entries),
            "page_title": "币圈快讯 · 往期归档",
            "meta_description": f"共 {len(entries)} 期币圈快讯历史归档，支持按日期检索回看。",
            "canonical": f"{ctx['base_url']}/news-archive.html" if ctx["base_url"] else "",
        }
    )
    html_archive = render(env, "archive.html", **ctx_archive)
    if dry_run:
        log.info("[dry-run] 将写入 news-archive.html (%d 条归档)", len(entries))
    else:
        atomic_write(out_dir / "news-archive.html", html_archive)
        copy_assets(BASE_DIR / "templates" / "assets", out_dir, assets_rel)
        retention = int(cfg["images"].get("retention_days", 0))
        prune_images(data_dir, out_dir, assets_rel, retention, now)
        prune_articles(data_dir, out_dir, retention, now)

    if degraded:
        log.error("降级完成：页面已生成，但内容不是实时数据")
        return 1
    log.info("构建完成，输出目录: %s", out_dir)
    return 0


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="币圈快讯聚合生成器")
    parser.add_argument("--config", default=None, help="配置文件路径，默认 <脚本目录>/config.json")
    parser.add_argument("--out", default=None, help="输出目录（网站根目录）")
    parser.add_argument("--dry-run", action="store_true", help="只抓取渲染，不写文件")
    parser.add_argument("--latest-only", action="store_true", help="只更新 news.html，不生成当日归档页")
    parser.add_argument("--no-images", action="store_true", help="不下载图片，直接引用源站地址")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    log.info("=== 构建开始 (v%s) ===", VERSION)

    config_path = Path(args.config).expanduser().resolve() if args.config else BASE_DIR / "config.json"
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        log.error("配置错误: %s", exc)
        return 2

    if args.no_images:
        cfg["images"]["enabled"] = False
        log.info("已按 --no-images 关闭图片本地化，页面直接引用源站图片")

    # 输出目录优先级：CLI > config.output_dir > 脚本上一级（通常就是站点根目录）
    raw_out = args.out or cfg.get("output_dir") or str(BASE_DIR)
    out_dir = Path(raw_out).expanduser().resolve()
    if not args.dry_run:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            log.error("无法创建输出目录 %s: %s", out_dir, exc)
            return 2

    started = time.time()
    try:
        code = build(cfg, out_dir, args.dry_run, args.latest_only)
    except ConfigError as exc:
        log.error("配置错误: %s", exc)
        return 2
    except KeyboardInterrupt:
        log.warning("用户中断")
        return 130
    except Exception as exc:
        log.exception("构建过程中发生未预期错误: %s", exc)
        return 1
    finally:
        log.info("=== 构建结束，耗时 %.1fs ===", time.time() - started)

    return code



# --------------------------------------------------------------------------- #
# 单文件模式：配置、模板、静态资源都已内嵌，整个项目只需这一个文件
# --------------------------------------------------------------------------- #
_EMBEDDED_BLOB = (
    "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"
    "pNxWynwymGln9ReOvfD/AEY+ttBBcAAA"
)


def _unpack_embedded() -> Dict[str, Any]:
    """解包内嵌数据（gzip + base64）。"""
    raw = json.loads(gzip.decompress(base64.b64decode("".join(_EMBEDDED_BLOB))).decode("utf-8"))
    return {
        "templates": raw.get("templates") or {},
        "assets": {k: base64.b64decode(v) for k, v in (raw.get("assets") or {}).items()},
        "config": raw.get("config") or "",
    }


_EMBEDDED = _unpack_embedded()
_ORIG_LOAD_CONFIG = load_config


def load_config(path: Path) -> Dict[str, Any]:
    """没有 config.json 时用内嵌配置；存在则完全以文件为准。"""
    if not path.exists() and _EMBEDDED["config"]:
        log.info("未找到 %s，使用内嵌配置（新建同名文件即可覆盖全部设置）", path.name)
        tmp = Path(tempfile.mkdtemp()) / "embedded-config.json"
        try:
            tmp.write_text(_EMBEDDED["config"], encoding="utf-8")
            return _ORIG_LOAD_CONFIG(tmp)
        finally:
            try:
                tmp.unlink()
                tmp.parent.rmdir()
            except OSError:
                pass
    return _ORIG_LOAD_CONFIG(path)


def build_env(template_dir: Path) -> Environment:
    """优先本地 templates/ 目录（方便改样式），缺失时回落到内嵌模板。"""
    loaders = []
    if template_dir.is_dir():
        loaders.append(FileSystemLoader(str(template_dir)))
    else:
        log.debug("未找到模板目录，使用内嵌模板")
    loaders.append(DictLoader(_EMBEDDED["templates"]))
    env = Environment(
        loader=ChoiceLoader(loaders),
        autoescape=select_autoescape(enabled_extensions=("html", "xml"), default=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["truncate_desc"] = lambda s, n=120: clean_text(s, limit=n)
    return env


def copy_assets(assets_dir: Path, out_dir: Path, assets_rel: str) -> None:
    """静态资源：本地目录优先，缺失时把内嵌版本写出去。"""
    target = out_dir / assets_rel
    target.mkdir(parents=True, exist_ok=True)
    pool: Dict[str, bytes] = dict(_EMBEDDED["assets"])
    if assets_dir.is_dir():
        for p in assets_dir.iterdir():
            if p.is_file():
                pool[p.name] = p.read_bytes()
    for name, data in pool.items():
        dst = target / name
        try:
            if dst.exists() and dst.read_bytes() == data:
                continue
            dst.write_bytes(data)
            log.debug("已同步静态资源: %s", name)
        except Exception as exc:
            log.warning("同步静态资源失败 %s: %s", name, exc)

if __name__ == "__main__":
    sys.exit(main())
import os
import datetime
import glob
import json

# 读取外部 config.json 配置文件
config_file = "config.json"
if os.path.exists(config_file):
    with open(config_file, "r", encoding="utf-8") as f:
        exchanges = json.load(f)
else:
    exchanges = {}

# 动态根据 JSON 生成交易所返佣按钮 HTML
btn_group_html = ""
for key, info in exchanges.items():
    btn_group_html += f'''
    <a href="{info["link"]}" target="_blank" style="background: #1e293b; color: #fff; padding: 12px 20px; border-radius: 8px; text-decoration: none; font-weight: bold; border: 1px solid #334155; display: inline-block; text-align: left; min-width: 200px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
        <div style="font-size: 0.95rem; color: #f8fafc; margin-bottom: 2px;">🔥 {info["name"]}</div>
        <div style="font-size: 0.75rem; color: #38bdf8; font-weight: normal;">{info["badge"]}</div>
    </a>
    '''

# 获取当前日期
today = datetime.datetime.now().strftime("%Y-%m-%d")
filename = f"news-{today}.html"

# 模拟采集到的快讯数据（包含配图链接）
news_list = [
    {
        "time": f"{today} 12:00",
        "title": "市场行情观察：主流资产震荡整理，链上活跃度持续提升",
        "desc": "今日加密市场整体保持高位震荡，头部交易所流动性充沛。分析指出，随着板块轮动加快，建议投资者注意防范杠杆风险。",
        "img": "https://images.unsplash.com/photo-1639762681485-074b7f938ba0?w=600&auto=format&fit=crop&q=80"
    },
    {
        "time": f"{today} 08:30",
        "title": "多维生态动态：Layer2 网络锁仓量稳步增长",
        "desc": "近期多条主流公链生态应用迎来更新，跨链桥与去中心化交易量表现亮眼。",
        "img": "https://images.unsplash.com/photo-1642543492481-44e81e3914a7?w=600&auto=format&fit=crop&q=80"
    }
]

# 动态生成包含 img 标签的图文快讯卡片 HTML
news_cards_html = ""
for item in news_list:
    news_cards_html += f"""
    <div class="news-box">
        <img src="{item['img']}" alt="新闻配图" class="news-img">
        <div class="news-content">
            <div class="news-time">{item['time']}</div>
            <div class="news-title">{item['title']}</div>
            <div class="news-desc">{item['desc']}</div>
        </div>
    </div>
    """

# 1. 生成今日快讯 HTML 内容（包含交易所返佣转化横幅）
html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{today} 币圈实时快讯与行业动态 - Web3 导航</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; background: #f9fafb; }}
        .header-bar {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #e2e8f0; padding-bottom: 15px; margin-bottom: 20px; }}
        .nav-links a {{ text-decoration: none; color: #2563eb; font-weight: 500; margin-right: 15px; }}
        
        /* 图文卡片样式 */
        .news-box {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 15px; margin-bottom: 15px; box-shadow: 0 2px 4px rgba(0,0,0,0.02); display: flex; gap: 15px; align-items: center; }}
        .news-img {{ width: 140px; height: 95px; object-fit: cover; border-radius: 6px; flex-shrink: 0; background: #e2e8f0; }}
        .news-content {{ flex-grow: 1; }}
        .news-time {{ font-size: 12px; color: #64748b; margin-bottom: 4px; }}
        .news-title {{ font-size: 1.05rem; font-weight: bold; color: #0f172a; margin-bottom: 6px; }}
        .news-desc {{ font-size: 0.9rem; color: #475569; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
        
        /* 交易所返佣转化横幅样式 */
        .affiliate-box {{ background: linear-gradient(135deg, #1e293b, #0f172a); color: #fff; padding: 25px; border-radius: 12px; margin-top: 30px; text-align: center; }}
        .affiliate-box h3 {{ margin-top: 0; margin-bottom: 10px; font-size: 1.2rem; }}
        .affiliate-box p {{ color: #94a3b8; font-size: 0.95rem; margin-bottom: 20px; }}
        .btn-group {{ display: flex; justify-content: center; gap: 15px; flex-wrap: wrap; }}
    </style>
</head>
<body>

    <header class="header-bar">
        <nav class="nav-links">
            <a href="./index.html">🏠 首页</a>
            <a href="./article-usstock.html">📈 美股教程</a>
            <a href="./news.html">🔥 币圈快讯</a>
        </nav>
    </header>

    <h1>🔥 {today} 币圈实时快讯</h1>
    <p>精选全网加密货币行业核心动态、政策风向与项目进展，7x24h 持续更新。</p>

    {news_cards_html}

    <!-- 交易所返佣转化横幅（仅在每日快讯正文页展示） -->
    <div class="affiliate-box">
        <h3>🚀 开启 Web3 交易之旅</h3>
        <p>通过专属邀请链接注册交易所，享受最高手续费返还与新手礼包。</p>
        <div class="btn-group">
            {btn_group_html}
        </div>
    </div>

    <footer>
        <p style="text-align: center; color: #94a3b8; font-size: 0.85rem; margin-top: 40px;">© 2026 Web3 Exchange Guide. 自动化快讯聚合系统运行中。</p>
    </footer>

</body>
</html>
"""

# 写入当天的快讯页面
with open(filename, "w", encoding="utf-8") as f:
    f.write(html_content)
print(f"成功生成今日快讯页面：{filename}")

# 2. 自动扫描所有历史快讯文件，动态生成聚合主页 news.html（已移除返佣横幅，保持干净）
news_files = sorted(glob.glob("news-*.html"), reverse=True)

list_items_html = ""
for file in news_files:
    date_str = file.replace("news-", "").replace(".html", "")
    list_items_html += f"""
    <div class="news-box">
        <div class="news-content">
            <div class="news-time">{date_str} 归档</div>
            <div class="news-title"><a href="./{file}" style="text-decoration: none; color: #2563eb;">{date_str} 币圈实时快讯与行业动态</a></div>
            <div class="news-desc">点击进入阅读 {date_str} 全网加密货币市场核心动态、行情观察与项目进展完整记录。</div>
        </div>
    </div>
    """

index_html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>币圈实时快讯中心 - 历史归档导航</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; background: #f9fafb; }}
        .header-bar {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #e2e8f0; padding-bottom: 15px; margin-bottom: 20px; }}
        .nav-links a {{ text-decoration: none; color: #2563eb; font-weight: 500; margin-right: 15px; }}
        .news-box {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 15px; margin-bottom: 15px; box-shadow: 0 2px 4px rgba(0,0,0,0.02); }}
        .news-time {{ font-size: 12px; color: #64748b; margin-bottom: 4px; }}
        .news-title {{ font-size: 1.05rem; font-weight: bold; color: #0f172a; margin-bottom: 6px; }}
        .news-desc {{ font-size: 0.9rem; color: #475569; }}
    </style>
</head>
<body>

    <header class="header-bar">
        <nav class="nav-links">
            <a href="./index.html">🏠 首页</a>
            <a href="./article-usstock.html">📈 美股教程</a>
            <a href="./news.html">🔥 币圈快讯</a>
        </nav>
    </header>

    <h1>🔥 币圈实时快讯中心</h1>
    <p>汇集每日加密货币市场核心动态、板块轮动与项目最新进展，支持 7x24h 自动化追踪归档。</p>

    {list_items_html}

    <footer>
        <p style="text-align: center; color: #94a3b8; font-size: 0.85rem; margin-top: 40px;">© 2026 Web3 Exchange Guide. 自动化快讯聚合系统运行中。</p>
    </footer>

</body>
</html>
"""

with open("news.html", "w", encoding="utf-8") as f:
    f.write(index_html_content)

print("成功更新聚合导航页：news.html")
