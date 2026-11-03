#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
吉林大学 OA 通知监控（免登录、免浏览器）

只访问 https://oa.jlu.edu.cn/ 的公开通知列表和详情页，不登录、不提交任何表单。
每隔一段时间抓取一次“校内通知”，对新增通知做：
  1. 正文提取与结构化摘要；
  2. 按“入党 / 奖学金 / 竞赛 / 停水停电供暖 / 教学考试 / 校园安全”等规则打分；
  3. 生成手机可读的摘要，通过 DSH 微信机器人推送。

用法：
  python oa_watch.py --check                 # 只检查能否抓取，不改状态
  python oa_watch.py --dry-run --max-new 5   # 抓取并打印摘要，不发送、不记已读
  python oa_watch.py --baseline              # 首次运行：把当前通知记为基线，并发送一条启动消息
  python oa_watch.py                         # 正常巡检（供计划任务调用）
  python oa_watch.py --test-send             # 测试微信通道
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import logging.handlers
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any

BASE = "https://oa.jlu.edu.cn"
DEFAULT_CHANNEL = "179577"  # 校内通知
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
DIGEST_PATH = ROOT / "digest_latest.md"
LOG_DIR = ROOT / "logs"
LOCK_PATH = ROOT / "run.lock"
LOCK_STALE_SECONDS = 600

DEFAULT_CONFIG: dict[str, Any] = {
    "channels": [DEFAULT_CHANNEL],
    "profile": {
        "grade": "大一",
        "campus": "",
        "dorm": "",
        "extra_keywords": [],
        "mute_keywords": ["研究生", "博士", "教职工", "博士后"],
    },
    "notify": {
        "enabled": True,
        "min_score": 40,
        "important_score": 65,
        "max_items": 6,
        "max_chars": 1500,
        "send_empty": False,
        "quiet_start": "23:30",
        "quiet_end": "07:00",
        "urgent_score": 80,
        "wechat_node": "",
        "wechat_cli": "",
    },
    "fetch": {
        "max_pages": 4,
        "timeout": 20,
        "delay": 0.6,
        "retries": 2,
    },
    # 整轮失败后的重试间隔（秒）。空列表表示不重试，一次失败就等下一轮。
    # 例：[60, 120] 表示失败后 1 分钟再试，仍失败则 2 分钟后再试，最多 3 次。
    "run": {
        "retry_delays": [60, 120],
    },
    "ai": {
        "enabled": False,
        "base_url": "",
        "api_key": "",
        "model": "",
        "timeout": 60,
    },
}

LOG = logging.getLogger("jlu_oa")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG.setLevel(logging.DEBUG)
    LOG.handlers.clear()

    fh = logging.handlers.RotatingFileHandler(
        LOG_DIR / "oa_watch.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(fh)

    if verbose:
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.DEBUG)
        sh.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        LOG.addHandler(sh)


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SystemExit(f"config.json 读取失败：{exc}")
        cfg = deep_merge(DEFAULT_CONFIG, user)
    else:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG, ensure_ascii=False))
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        LOG.info("已生成默认配置：%s", CONFIG_PATH)
    return cfg


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            LOG.warning("state.json 损坏，重新建立")
            state = {}
    else:
        state = {}
    state.setdefault("version", 1)
    state.setdefault("seen", {})
    state.setdefault("pending_items", [])
    state.setdefault("last_run", None)
    return state


def acquire_lock() -> bool:
    """单实例锁：唤醒补跑和 30 分钟常规任务可能同时被触发，避免重复推送。

    进程崩溃留下的锁超过 10 分钟视为残留，自动清理。
    """
    for _ in range(2):
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - LOCK_PATH.stat().st_mtime
            except OSError:
                age = LOCK_STALE_SECONDS + 1
            if age > LOCK_STALE_SECONDS:
                try:
                    LOCK_PATH.unlink()
                except OSError:
                    pass
                continue
            return False
    return False


def release_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass


def save_state(state: dict[str, Any]) -> None:
    # 只保留最近的 8000 条已读记录，避免文件无限增长
    seen = state.get("seen", {})
    if len(seen) > 8000:
        keep = sorted(seen.items(), key=lambda kv: str(kv[1]), reverse=True)[:6000]
        state["seen"] = dict(keep)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def clean_text(value: str) -> str:
    value = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", value, flags=re.S | re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def strip_tags(value: str) -> str:
    return clean_text(value)


def truncate(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def smart_truncate(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= limit:
        return value
    window = value[:limit]
    cut = max(window.rfind("。"), window.rfind("；"), window.rfind("！"), window.rfind("？"))
    if cut >= int(limit * 0.55):
        return window[:cut].rstrip("；;，, ") + "。"
    return window[: limit - 1].rstrip("；;，, ") + "…"


def parse_hhmm(value: str) -> tuple[int, int]:
    try:
        hour, minute = value.strip().split(":")
        return int(hour), int(minute)
    except Exception:
        return 0, 0


def in_quiet_hours(now: datetime, cfg: dict[str, Any]) -> bool:
    start = parse_hhmm(cfg["notify"].get("quiet_start", "23:30"))
    end = parse_hhmm(cfg["notify"].get("quiet_end", "07:00"))
    minutes = now.hour * 60 + now.minute
    start_m = start[0] * 60 + start[1]
    end_m = end[0] * 60 + end[1]
    if start_m == end_m:
        return False
    if start_m < end_m:
        return start_m <= minutes < end_m
    return minutes >= start_m or minutes < end_m


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------

class Fetcher:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.timeout = int(cfg["fetch"]["timeout"])
        self.delay = float(cfg["fetch"]["delay"])
        self.retries = int(cfg["fetch"]["retries"])
        ctx = ssl.create_default_context()
        # OA 的证书链不完整（缺少中间证书），用校内的公开页面做只读监控，
        # 这里关闭校验以避免依赖系统证书链；不发送任何账号信息。
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()),
            urllib.request.HTTPSHandler(context=ctx),
        )

    def get(self, url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/130.0 Safari/537.36 Edg/130.0"
                        ),
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "zh-CN,zh;q=0.9",
                        "Connection": "close",
                    },
                )
                with self.opener.open(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    ctype = resp.headers.get("Content-Type", "")
                encoding = "utf-8"
                match = re.search(r"charset=([\w-]+)", ctype, re.I)
                if match:
                    encoding = match.group(1)
                text = raw.decode(encoding, "replace")
                # 少数页面没有正确声明编码，用替换字符比例做个兜底
                if text.count("\ufffd") > 5 and encoding.lower() not in ("gbk", "gb2312", "gb18030"):
                    alt = raw.decode("gbk", "replace")
                    if alt.count("\ufffd") < text.count("\ufffd"):
                        text = alt
                if self.delay:
                    time.sleep(self.delay)
                return text
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"抓取失败：{url}：{last_error}")


@dataclass
class Notice:
    id: str
    channel: str
    title: str
    org: str
    date: datetime
    url: str
    pinned: bool = False
    body: str = ""
    attachments: list[dict[str, str]] = field(default_factory=list)
    score: int = 0
    level: str = "归档"
    reasons: list[str] = field(default_factory=list)
    summary: str = ""
    deadline: str = ""
    location: str = ""
    audience: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel": self.channel,
            "title": self.title,
            "org": self.org,
            "date": self.date.isoformat(),
            "url": self.url,
            "pinned": self.pinned,
            "body": self.body,
            "attachments": self.attachments,
            "score": self.score,
            "level": self.level,
            "reasons": self.reasons,
            "summary": self.summary,
            "deadline": self.deadline,
            "location": self.location,
            "audience": self.audience,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Notice":
        return cls(
            id=str(data.get("id", "")),
            channel=str(data.get("channel", DEFAULT_CHANNEL)),
            title=data.get("title", ""),
            org=data.get("org", ""),
            date=datetime.fromisoformat(data["date"]) if data.get("date") else datetime.now().astimezone(),
            url=data.get("url", ""),
            pinned=bool(data.get("pinned", False)),
            body=data.get("body", ""),
            attachments=data.get("attachments", []),
            score=int(data.get("score", 0)),
            level=data.get("level", "归档"),
            reasons=data.get("reasons", []),
            summary=data.get("summary", ""),
            deadline=data.get("deadline", ""),
            location=data.get("location", ""),
            audience=data.get("audience", ""),
        )


def parse_list_date(raw: str, now: datetime) -> datetime:
    text = clean_text(raw)
    match = re.search(r"(今天|昨天|前天)\s*(\d{1,2}):(\d{2})", text)
    if match:
        offset = {"今天": 0, "昨天": 1, "前天": 2}[match.group(1)]
        day = (now - timedelta(days=offset)).date()
        return datetime(
            day.year, day.month, day.day, int(match.group(2)), int(match.group(3)),
            tzinfo=now.tzinfo,
        )
    match = re.search(r"(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?(?:\s*(\d{1,2}):(\d{2}))?", text)
    if match:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        hour = int(match.group(4) or 0)
        minute = int(match.group(5) or 0)
        return datetime(year, month, day, hour, minute, tzinfo=now.tzinfo)
    match = re.search(r"(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", text)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        hour = int(match.group(3) or 0)
        minute = int(match.group(4) or 0)
        return datetime(now.year, month, day, hour, minute, tzinfo=now.tzinfo)
    return now


def parse_items(html_text: str, channel: str, now: datetime) -> list[Notice]:
    items: list[Notice] = []
    blocks = re.findall(
        r'<div\s+class="li rel"\s*>(.*?)</div>', html_text, re.S | re.I
    )
    for block in blocks:
        link = re.search(
            r'<a\s+class="font14"\s+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S | re.I
        )
        if not link:
            continue
        href, title_html = link.group(1), link.group(2)
        id_match = re.search(r"[?&]id=(\d+)", href)
        if not id_match:
            continue
        title = clean_text(title_html)
        pinned = ("[置顶]" in title) or ("font class='red'" in block.lower())
        title = title.replace("[置顶]", "").strip()

        org_match = re.search(
            r'<a\s+class="column"[^>]*>(.*?)</a>', block, re.S | re.I
        )
        org = clean_text(org_match.group(1)) if org_match else ""

        date_match = re.search(
            r'<span\s+class="time"[^>]*>(.*?)</span>', block, re.S | re.I
        )
        date = parse_list_date(date_match.group(1), now) if date_match else now

        url = urllib.parse.urljoin(BASE + "/defaultroot/", href)
        items.append(
            Notice(
                id=id_match.group(1),
                channel=channel,
                title=title,
                org=org,
                date=date,
                url=url,
                pinned=pinned,
            )
        )
    return items


def fetch_list(fetcher: Fetcher, cfg: dict[str, Any], seen: dict[str, str]) -> list[Notice]:
    now = datetime.now().astimezone()
    collected: list[Notice] = []
    by_id: set[str] = set()
    max_pages = int(cfg["fetch"]["max_pages"])
    for channel in cfg.get("channels", [DEFAULT_CHANNEL]):
        channel = str(channel)
        for page in range(1, max_pages + 1):
            if page == 1:
                url = (
                    f"{BASE}/defaultroot/PortalInformation!jldxList.action"
                    f"?channelId={channel}"
                )
            else:
                url = (
                    f"{BASE}/defaultroot/PortalInformation!jldxList.action"
                    f"?1=1&channelId={channel}&startPage={page}"
                )
            try:
                html_text = fetcher.get(url)
            except Exception as exc:  # noqa: BLE001
                LOG.error("列表页抓取失败：%s", exc)
                # 第一页就失败说明整站不通（断网 / DNS 未就绪），交给上层重试；
                # 后面的页失败只是少了几条，不必重跑整轮。
                if page == 1:
                    raise RuntimeError(f"列表页抓取失败：{exc}") from exc
                break
            page_items = parse_items(html_text, channel, now)
            if not page_items:
                break
            new_on_page = [item for item in page_items if item.id not in seen]
            for item in page_items:
                if item.id not in by_id:
                    by_id.add(item.id)
                    collected.append(item)
            LOG.debug(
                "频道 %s 第 %s 页：%s 条，未读 %s 条",
                channel, page, len(page_items), len(new_on_page),
            )
            if page > 1 and not new_on_page:
                break
    return collected


def fetch_list_with_retry(
    fetcher: Fetcher, cfg: dict[str, Any], seen: dict[str, str]
) -> list[Notice]:
    """抓取列表，失败后按 config 里的间隔重试。

    唤醒瞬间网络往往还没就绪（DNS 解析失败），一次失败就空一整轮是原来最大的坑。
    重试次数刻意控制得很少：宁可这次放弃，也不要连着刷 OA。
    """
    delays = [int(x) for x in (cfg.get("run", {}).get("retry_delays") or [])]
    attempts = len(delays) + 1
    last_error: Exception | None = None
    for index in range(attempts):
        try:
            return fetch_list(fetcher, cfg, seen)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            LOG.warning("第 %s/%s 次抓取失败：%s", index + 1, attempts, exc)
            if index < attempts - 1:
                wait = max(0, delays[index])
                LOG.info("等待 %s 秒后重试", wait)
                time.sleep(wait)
    raise RuntimeError(f"连续 {attempts} 次抓取失败：{last_error}")


# ---------------------------------------------------------------------------
# 正文解析
# ---------------------------------------------------------------------------

class ContentParser(HTMLParser):
    """提取 div.content_font 内的正文、段落和附件链接。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_content = False
        self.depth = 0
        self.parts: list[str] = []
        self.attachments: list[dict[str, str]] = []
        self.skip_depth = 0
        self.current_link: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: (value or "") for key, value in attrs}
        if tag in ("script", "style"):
            self.skip_depth += 1
            return
        if self.skip_depth:
            return

        if (
            tag == "div"
            and not self.in_content
            and "content_font" in attrs_dict.get("class", "")
        ):
            self.in_content = True
            self.depth = 1
            return

        if not self.in_content:
            return

        if tag == "div":
            self.depth += 1
        if tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "table"):
            self.parts.append("\n")
        if tag == "a":
            self.current_link = (attrs_dict.get("href", ""), [])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth or not self.in_content:
            return
        if tag == "a" and self.current_link:
            href, chunks = self.current_link
            label = clean_text("".join(chunks))
            if href and re.search(
                r"\.(pdf|docx?|xlsx?|pptx?|zip|rar|7z|txt)(\?|$)", href, re.I
            ):
                self.attachments.append(
                    {
                        "name": label or Path(urllib.parse.urlparse(href).path).name,
                        "url": urllib.parse.urljoin(BASE + "/defaultroot/", href),
                    }
                )
            self.current_link = None
        if tag == "div":
            self.depth -= 1
            if self.depth <= 0:
                self.in_content = False

    def handle_data(self, data: str) -> None:
        if self.skip_depth or not self.in_content:
            return
        self.parts.append(data)
        if self.current_link is not None:
            self.current_link[1].append(data)

    def text(self) -> str:
        value = "".join(self.parts)
        value = value.replace("\xa0", " ")
        value = re.sub(r"[ \t\r\f\v]+", " ", value)
        value = re.sub(r"\n\s*\n+", "\n", value)
        return value.strip()


def extract_div(html_text: str, class_name: str) -> str:
    match = re.search(
        rf'<div[^>]*class="[^"]*{re.escape(class_name)}[^"]*"[^>]*>(.*?)</div>',
        html_text,
        re.S | re.I,
    )
    return match.group(1) if match else ""


def parse_detail(html_text: str, item: Notice) -> Notice:
    title = clean_text(extract_div(html_text, "content_t")) or item.title
    time_html = extract_div(html_text, "content_time")
    org = ""
    org_match = re.search(r"<span[^>]*>(.*?)</span>", time_html, re.S | re.I)
    if org_match:
        org = clean_text(org_match.group(1))
    date_text = clean_text(re.sub(r"<span.*?</span>", "", time_html, flags=re.S | re.I))
    if date_text:
        item.date = parse_list_date(date_text, item.date or datetime.now().astimezone())

    parser = ContentParser()
    try:
        parser.feed(html_text)
        body = parser.text()
        attachments = parser.attachments
    except Exception:  # noqa: BLE001
        body, attachments = "", []

    if not body:
        fallback = re.search(
            r'<div[^>]*class="[^"]*content_font[^"]*"[^>]*>(.*?)</div>\s*</div>',
            html_text,
            re.S | re.I,
        )
        body = strip_tags(fallback.group(1)) if fallback else ""

    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    item.title = title
    item.org = org or item.org
    item.body = body
    item.attachments = attachments
    return item


# ---------------------------------------------------------------------------
# 打分与摘要
# ---------------------------------------------------------------------------

CATEGORIES: list[tuple[str, str, int]] = [
    ("入党/党团", r"入党|党员|党组织|党课|党校|积极分子|发展对象|推优|团课|团委|党委|党团|主题党日|团支部|青马", 30),
    ("奖学金/资助", r"奖学金|奖助学金|助学金|励志奖学金|国家奖学金|评奖评优|评优|助学贷款|勤工助学|困难补助|资助", 35),
    ("竞赛/创新创业", r"竞赛|大赛|挑战杯|互联网\+|数学建模|电子设计|创新创业|学科竞赛|选拔赛|省赛|国赛|ACM|程序设计", 32),
    ("停水/停电/供暖/维修", r"停水|停电|停暖|供暖|维修|施工|封闭|公寓|宿舍|寝室|浴池|开水|食堂|校车|通勤|校园卡|一卡通|断网|燃气|消防", 34),
    ("教学/考试/学籍", r"(?<!公)选课|退课|补选|考试|补考|缓考|成绩|学籍|注册|报到|培养方案|转专业|辅修|双学位|四六级|外语考试|推免|保研", 28),
    ("安全/健康", r"紧急|应急|演练|诈骗|防汛|防火|防疫|健康|医保|疫苗|体检", 26),
    ("课程/公选课", r"公选课|开课|课程|课堂|教学", 10),
    ("讲座/活动", r"讲座|报告|论坛|沙龙|晚会|志愿|招募|社团|纳新|文体|运动会|比赛|活动", 8),
    ("科研/项目", r"科研|基金|项目|课题|立项|结题|申报|重点研发|自然科学", -12),
    ("人事/招标", r"招标|采购|任免|拟聘|招聘|人事|博士后|人才", -18),
]


def score_notice(item: Notice, cfg: dict[str, Any]) -> None:
    """标题决定主题相关性，正文只补充“谁/哪里/什么时候”等细节。

    之前的实现把正文也算进关键词，结果一篇讲座通知因为讲者简介里出现
    “党员”“创新创业”“教学”就被判成重要通知。这里改成标题优先。
    """
    title = item.title
    body = item.body[:4000]
    score = 0
    reasons: list[str] = []
    matched_positive = False

    for label, pattern, weight in CATEGORIES:
        if re.search(pattern, title, re.I):
            score += weight
            if weight > 0:
                reasons.append(label)
                matched_positive = True

    # 标题很泛（如“关于开展XX的通知”）时，才用正文兜底识别一个主题
    if not matched_positive:
        for label, pattern, weight in CATEGORIES[:6]:
            if weight > 0 and re.search(pattern, body, re.I):
                score += max(10, weight // 2)
                reasons.append(label + "（正文）")
                break

    if re.search(r"本科生|本科|2026级|新生|大一", title):
        score += 10
        reasons.append("本科生/新生")
    if re.search(r"研究生|博士", title):
        score -= 12
    if re.search(r"教师|教职工|导师|辅导员|博士后", title):
        score -= 10
    if re.search(r"公示|拟聘|任免", title):
        score -= 5
    if re.search(r"会议|培训|研讨会|座谈会", title) and not re.search(
        r"学生|竞赛|党团|选课|考试", title
    ):
        score -= 6

    campus = str(cfg["profile"].get("campus", "")).strip()
    campus_key = campus.replace("校区", "").strip()
    if campus_key and (campus_key in title or campus_key in body):
        score += 15
        reasons.append(campus)
    if re.search(r"各校区|全校|所有校区", title):
        score += 8
        reasons.append("全校/各校区")
    dorm = str(cfg["profile"].get("dorm", "")).strip()
    if dorm:
        dorm_pattern = re.escape(dorm).replace("12", "(?:12|十二)")
        if re.search(dorm_pattern, title) or re.search(dorm_pattern, body):
            score += 20
            reasons.append(dorm)
    if re.search(r"前卫|朝阳|新民|南湖|和平|中心", title) and campus_key not in title:
        score -= 8
    if re.search(r"截止|请于|务必于|报名|申请|提交|报送|前将", title + body):
        score += 8
    if re.search(r"紧急|临时|今天|明天|今晚", title):
        score += 8
    if re.search(r"学生", title) and "研究生" not in title:
        score += 5
    elif re.search(r"全体学生|全校学生|本科生|在籍学生", body):
        score += 5
    if item.pinned:
        score += 3
    for keyword in cfg["profile"].get("extra_keywords", []):
        if keyword and keyword in title + body:
            score += 12
            reasons.append(str(keyword))
    for keyword in cfg["profile"].get("mute_keywords", []):
        if keyword and keyword in title:
            score -= 6

    score = max(0, min(100, score))
    item.score = score
    item.level = (
        "重要" if score >= int(cfg["notify"]["important_score"])
        else "关注" if score >= int(cfg["notify"]["min_score"])
        else "归档"
    )
    # 去重，保留最多 4 个理由
    seen_reasons: list[str] = []
    for reason in reasons:
        if reason not in seen_reasons:
            seen_reasons.append(reason)
    item.reasons = seen_reasons[:4]


def split_sentences(text: str) -> list[str]:
    text = text.replace("\r", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n+", "\n", text)
    parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", text)
    return [part.strip() for part in parts if part.strip()]


BOILERPLATE_START = re.compile(
    r"^(为深入|为贯彻|为落实|为进一步|根据|按照|现将|为了|旨在|特此|附件|"
    r"联系人|联系电话|电子邮箱|抄送|吉林大学电子校务平台)"
)


def build_summary(item: Notice) -> str:
    body = item.body.strip()
    if not body:
        return item.title

    body = re.sub(
        r"^(?:校内各学院|校内各单位|各学院|各单位|各部门|全体师生|各位同学|同学们|"
        r"[\u4e00-\u9fa5]{2,8}校区各单位及师生员工)[：:]\s*",
        "",
        body,
    )
    sentences = split_sentences(body)
    scored: list[tuple[float, int, str]] = []
    for index, sentence in enumerate(sentences):
        sentence = re.sub(r"^[一二三四五六七八九十\d]+[、.．)]\s*", "", sentence).strip()
        sentence = re.sub(r"\s+", " ", sentence).strip("；; ")
        if len(sentence) < 12:
            continue
        if BOILERPLATE_START.search(sentence):
            continue
        if re.match(r"^(附件|附表|注[:：]|说明[:：])", sentence):
            continue
        score = 0.0
        if re.search(r"\d{1,2}月\d{1,2}日|\d{1,2}[:：]\d{2}", sentence):
            score += 4
        if re.search(r"报名|截止|请于|前将|申请|提交|参加|登录|扫码", sentence):
            score += 4
        if re.search(r"地点|校区|楼|教室|报告厅|会议室|体育馆", sentence):
            score += 3
        if re.search(r"对象|面向|全体|学生|本科生|新生", sentence):
            score += 2
        if re.search(r"时间|定于|将于|举行|举办|召开|开展", sentence):
            score += 1
        if len(sentence) > 160:
            score -= 3
        if score <= 0:
            continue
        scored.append((score, index, sentence))

    if not scored:
        fallback = next(
            (s for s in sentences if len(s) >= 12 and not BOILERPLATE_START.search(s)),
            item.title,
        )
        return truncate(fallback, 220)

    # 取分数最高的 4 句，再按原文顺序拼接，读起来更连贯
    top = sorted(scored, key=lambda row: (-row[0], row[1]))[:4]
    top.sort(key=lambda row: row[1])
    chosen: list[str] = []
    for _, _, sentence in top:
        if any(sentence[:18] == existing[:18] for existing in chosen):
            continue
        chosen.append(sentence)
    summary = "；".join(s.rstrip("。；; ") for s in chosen).strip("；; ") + "。"
    return smart_truncate(summary, 320)


DATE_PATTERN = (
    r"(?:\d{4}年)?\d{1,2}月\d{1,2}日"
    r"(?:\s*(?:上午|下午|晚上)?\s*\d{1,2}[:：]\d{2})?"
)


def extract_deadline(body: str) -> str:
    if not body:
        return ""
    for match in re.finditer(
        r"(?:截止|请于|务必于|报名|申请|提交|报送|前将)[^。；\n]{0,80}", body
    ):
        dates = re.findall(DATE_PATTERN, match.group(0))
        if dates:
            return "时间：" + dates[0].replace("：", ":")
    dates = re.findall(DATE_PATTERN, body)
    if dates:
        return "时间：" + dates[0].replace("：", ":")
    return ""


def extract_location(body: str) -> str:
    match = re.search(r"(?:地点|地址|会场)[:：]\s*([^\n。；：]{2,50})", body)
    if not match:
        match = re.search(
            r"((?:前卫|南岭|朝阳|新民|南湖|和平|中心)校区"
            r"[^\n。；]{0,40}?(?:楼|教室|报告厅|会议室|图书馆|馆|场|门|广场|食堂|公寓)"
            r"[^\n。；]{0,30})",
            body,
        )
    return "地点：" + match.group(1).strip() if match else ""


def extract_audience(body: str) -> str:
    match = re.search(
        r"(?:活动对象|参加对象|参与对象|对象)[:：]?\s*([^\n。；]{2,50})", body
    )
    return "对象：" + match.group(1).strip() if match else ""


def enrich_item(fetcher: Fetcher, item: Notice, cfg: dict[str, Any]) -> Notice:
    try:
        html_text = fetcher.get(item.url)
        parse_detail(html_text, item)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("详情页抓取失败 %s：%s", item.url, exc)
    score_notice(item, cfg)
    item.summary = build_summary(item)
    item.deadline = extract_deadline(item.body)
    item.location = extract_location(item.body)
    item.audience = extract_audience(item.body)
    return item


# ---------------------------------------------------------------------------
# 摘要输出
# ---------------------------------------------------------------------------

def format_message(
    items: list[Notice], now: datetime, cfg: dict[str, Any], intro: bool = False
) -> str:
    hot = [
        item
        for item in items
        if item.score >= int(cfg["notify"]["min_score"])
    ]
    hot.sort(key=lambda item: (-item.score, -item.date.timestamp()))

    lines: list[str] = []
    if intro:
        lines.append("✅ 吉大OA监控已启动")
        lines.append("以后有与你相关的新通知，我会自动发到这里。")
        lines.append("")

    lines.append(f"🔔 吉大OA · {now:%m月%d日 %H:%M}")
    if not hot:
        lines.append(f"本轮新增 {len(items)} 条，暂无与你直接相关的内容。")
        return "\n".join(lines)

    if intro:
        lines.append(f"先看最近 {len(items)} 条，其中 {len(hot)} 条值得关注：")
    else:
        lines.append(f"新增 {len(items)} 条，筛出 {len(hot)} 条与你相关：")
    max_items = int(cfg["notify"]["max_items"])
    for index, item in enumerate(hot[:max_items]):
        icon = "🔴" if item.level == "重要" else "🟡"
        lines.append("")
        lines.append(f"{icon} {truncate(item.title, 60)}")
        meta = f"｜{item.org or '未知部门'} · {item.date:%m-%d}"
        lines.append(meta)
        if item.reasons:
            lines.append("｜为什么：" + "、".join(item.reasons))
        if item.deadline:
            lines.append("｜" + item.deadline)
        if item.location:
            lines.append("｜" + item.location)
        if item.summary:
            summary_limit = 140 if index < 3 else 100
            lines.append("｜" + smart_truncate(item.summary, summary_limit))
        lines.append("｜" + item.url)

    if len(hot) > max_items:
        lines.append("")
        lines.append(f"另有 {len(hot) - max_items} 条，回复「OA 全部」可看完整清单。")

    text = "\n".join(lines)
    max_chars = int(cfg["notify"].get("max_chars", 1500))
    if len(text) > max_chars:
        # 优先砍摘要，再砍条目
        while len(text) > max_chars and len(lines) > 9:
            removable = None
            for i in range(len(lines) - 1, 0, -1):
                if lines[i].startswith("｜") and len(lines[i]) > 80 and not lines[i].startswith("｜http"):
                    removable = i
                    break
            if removable is None:
                lines.pop()
            else:
                lines[removable] = truncate(lines[removable], 60)
            text = "\n".join(lines)
    return text


def write_digest(items: list[Notice], now: datetime, cfg: dict[str, Any]) -> None:
    ordered = sorted(items, key=lambda item: (-item.score, -item.date.timestamp()))
    lines = [
        f"# 吉大OA通知摘要 · {now:%Y-%m-%d %H:%M}",
        "",
        f"本轮新增 **{len(items)}** 条。",
        "",
    ]
    groups = [("重要", "🔴 重要"), ("关注", "🟡 关注"), ("归档", "⚪ 归档")]
    for level, heading in groups:
        group = [item for item in ordered if item.level == level]
        if not group:
            continue
        lines.append(f"## {heading}（{len(group)} 条）")
        lines.append("")
        for item in group:
            lines.append(f"### {item.title}")
            lines.append(
                f"- 部门：{item.org or '未知'} ｜ 时间：{item.date:%Y-%m-%d %H:%M} ｜ 评分：{item.score}"
            )
            if item.reasons:
                lines.append(f"- 命中：{'、'.join(item.reasons)}")
            if item.deadline:
                lines.append(f"- {item.deadline}")
            if item.location:
                lines.append(f"- {item.location}")
            if item.audience:
                lines.append(f"- {item.audience}")
            if item.summary:
                lines.append(f"- 摘要：{item.summary}")
            lines.append(f"- 原文：{item.url}")
            if item.attachments:
                lines.append("- 附件：")
                for attachment in item.attachments:
                    lines.append(f"  - [{attachment['name']}]({attachment['url']})")
            lines.append("")
    DIGEST_PATH.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# 微信推送
# ---------------------------------------------------------------------------

def find_wechat_sender(cfg: dict[str, Any]) -> tuple[list[str], dict[str, str]] | None:
    notify = cfg["notify"]
    dsh_home = Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))

    cli = notify.get("wechat_cli") or ""
    node = notify.get("wechat_node") or ""
    if cli and node and Path(cli).exists() and Path(node).exists():
        return [str(node), str(cli)], os.environ.copy()

    candidates = [
        dsh_home / "profiles" / "desktop" / "node_modules" / "dsh-wechat-plugin" / "cli.js",
    ]
    candidates += sorted(
        (dsh_home / "profiles").glob("*/node_modules/dsh-wechat-plugin/cli.js")
    ) if (dsh_home / "profiles").exists() else []
    cli_path = next((path for path in candidates if path.exists()), None)

    node_candidates = sorted(
        (dsh_home / "dsh-runtimes").glob("*/dependencies/node/bin/node.exe")
    ) if (dsh_home / "dsh-runtimes").exists() else []
    node_path = next((path for path in node_candidates if path.exists()), None)

    if cli_path and node_path:
        env = os.environ.copy()
        env["PATH"] = str(node_path.parent) + os.pathsep + env.get("PATH", "")
        return [str(node_path), str(cli_path)], env

    exe = shutil.which("dsh-wechat-send")
    if exe:
        return [exe], os.environ.copy()

    if cli_path:
        env = os.environ.copy()
        if node_path:
            env["PATH"] = str(node_path.parent) + os.pathsep + env.get("PATH", "")
        return ["node", str(cli_path)], env

    return None


def send_wechat(text: str, cfg: dict[str, Any], dry_run: bool = False) -> int:
    if dry_run:
        print("\n----- 微信消息预览 -----")
        print(text)
        print("----- 预览结束 -----\n")
        return 0

    sender = find_wechat_sender(cfg)
    if not sender:
        LOG.error("找不到 dsh-wechat-send；请确认 DSH 桌面版和微信插件已安装")
        return 4

    argv, env = sender
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        proc = subprocess.run(
            argv + [text],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception as exc:  # noqa: BLE001
        LOG.error("调用微信发送命令失败：%s", exc)
        return 1

    if proc.stdout.strip():
        LOG.info("微信发送 stdout：%s", proc.stdout.strip()[:500])
    if proc.stderr.strip():
        LOG.warning("微信发送 stderr：%s", proc.stderr.strip()[:500])
    LOG.info("微信发送退出码：%s", proc.returncode)
    return proc.returncode


# ---------------------------------------------------------------------------
# 可选：模型增强（OpenAI 兼容接口）
# ---------------------------------------------------------------------------

def ai_enhance(items: list[Notice], cfg: dict[str, Any]) -> bool:
    ai = cfg.get("ai", {})
    if not ai.get("enabled") or not ai.get("api_key") or not ai.get("base_url"):
        return False
    payload_items = [
        {
            "id": item.id,
            "title": item.title,
            "org": item.org,
            "body": truncate(item.body, 2500),
        }
        for item in items
        if item.body
    ]
    if not payload_items:
        return False

    profile = cfg.get("profile", {})
    profile_text = "、".join(
        str(profile.get(key, "")).strip()
        for key in ("grade", "campus", "dorm")
        if str(profile.get(key, "")).strip()
    ) or "吉林大学学生"
    prompt = (
        f"你是吉林大学学生的信息助理。学生情况：{profile_text}。"
        "请阅读下面的 OA 通知，逐条输出 JSON 数组，每项包含 id、score（0-100）、"
        "reasons（字符串数组，说明为什么与该生相关）、summary（不超过120字，概括是什么、"
        "谁需要做、截止时间、地点）。只输出 JSON，不要输出其他文字。\n\n"
        + json.dumps(payload_items, ensure_ascii=False)
    )
    body = json.dumps(
        {
            "model": ai.get("model") or "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        ai["base_url"].rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + str(ai["api_key"]),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=int(ai.get("timeout", 60))) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        content = data["choices"][0]["message"]["content"]
        match = re.search(r"\[.*\]", content, re.S)
        if not match:
            return False
        parsed = json.loads(match.group(0))
        by_id = {str(row.get("id")): row for row in parsed if isinstance(row, dict)}
        changed = False
        for item in items:
            row = by_id.get(item.id)
            if not row:
                continue
            if isinstance(row.get("score"), (int, float)):
                item.score = max(0, min(100, int(row["score"])))
                item.level = (
                    "重要" if item.score >= int(cfg["notify"]["important_score"])
                    else "关注" if item.score >= int(cfg["notify"]["min_score"])
                    else "归档"
                )
            if isinstance(row.get("reasons"), list):
                item.reasons = [str(x) for x in row["reasons"]][:4]
            if row.get("summary"):
                item.summary = truncate(str(row["summary"]), 320)
            changed = True
        return changed
    except Exception as exc:  # noqa: BLE001
        LOG.warning("模型增强失败，回退到规则摘要：%s", exc)
        return False


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def mark_seen(state: dict[str, Any], items: list[Notice], now: datetime) -> None:
    for item in items:
        state["seen"][item.id] = now.isoformat()


def run_once(args: argparse.Namespace) -> int:
    cfg = load_config()
    state = load_state()
    now = datetime.now().astimezone()
    fetcher = Fetcher(cfg)

    # 唤醒补跑用：刚刚才跑过就不重复跑，避免一次唤醒连发两条一样的摘要
    if getattr(args, "if_stale", 0):
        last_run = state.get("last_run")
        if last_run:
            try:
                age = (now - datetime.fromisoformat(last_run)).total_seconds() / 60
            except Exception:  # noqa: BLE001
                age = None
            if age is not None and age < args.if_stale:
                LOG.info("距上次巡检 %.0f 分钟（< %s），跳过本次", age, args.if_stale)
                if not args.quiet:
                    print(f"距上次巡检 {age:.0f} 分钟，跳过。")
                return 0

    LOG.info("开始巡检（dry_run=%s, baseline=%s）", args.dry_run, args.baseline)
    try:
        all_items = fetch_list_with_retry(fetcher, cfg, state["seen"])
    except Exception as exc:  # noqa: BLE001
        LOG.error("巡检失败：%s", exc)
        if not args.quiet:
            print(f"抓取失败：{exc}")
        return 1

    if args.check:
        print(f"抓取成功：{len(all_items)} 条通知")
        for item in all_items[:10]:
            print(f"  {item.date:%m-%d %H:%M} | {item.title[:60]} | {item.org}")
        return 0

    if args.test_send:
        code = send_wechat(
            f"✅ 吉大OA监控测试\n{now:%Y-%m-%d %H:%M}\n微信通道正常。",
            cfg,
            dry_run=args.dry_run,
        )
        print(f"发送结果：{code}")
        return 0 if code in (0, 3) else 1

    if args.baseline:
        # 基线：当前所有通知记为已读，只挑最近 3 天、标题初筛较高的几条做启动摘要
        candidates = [
            item
            for item in all_items
            if (now - item.date) <= timedelta(days=3) and item.id not in state["seen"]
        ]
        for item in candidates:
            score_notice(item, cfg)
        candidates.sort(key=lambda item: -item.score)
        selected = candidates[:4]
        for item in selected:
            enrich_item(fetcher, item, cfg)
        ai_enhance(selected, cfg)
        message = format_message(selected, now, cfg, intro=True)

        if args.init_only:
            mark_seen(state, all_items, now)
            state["last_run"] = now.isoformat()
            write_digest(selected, now, cfg)
            save_state(state)
            LOG.info("仅初始化基线，未发送；基线 %s 条", len(all_items))
            if not args.quiet:
                print(f"已初始化基线：{len(all_items)} 条通知记为已读，未发送。")
                print(message)
            return 0

        code = send_wechat(message, cfg, dry_run=args.dry_run)
        if not args.dry_run and code in (0, 3):
            mark_seen(state, all_items, now)
            state["last_run"] = now.isoformat()
            write_digest(selected, now, cfg)
            save_state(state)
        elif args.dry_run:
            write_digest(selected, now, cfg)
        LOG.info("基线完成，发送结果 %s，基线 %s 条", code, len(all_items))
        if not args.quiet and not args.dry_run:
            print(message)
        return 0 if code in (0, 3) or args.dry_run else 1

    pending = [Notice.from_dict(row) for row in state.get("pending_items", [])]
    pending_ids = {item.id for item in pending}
    new_items = [
        item
        for item in all_items
        if item.id not in state["seen"] and item.id not in pending_ids
    ]
    todo = pending + new_items
    if args.max_new:
        todo = todo[: args.max_new]

    if not todo:
        LOG.info("没有新通知")
        state["last_run"] = now.isoformat()
        save_state(state)
        if not args.quiet:
            print("没有新通知。")
        return 0

    # 详情页只抓一次；pending 里已带正文的不重复抓
    for item in todo:
        if not item.body:
            enrich_item(fetcher, item, cfg)
        else:
            score_notice(item, cfg)
            if not item.summary:
                item.summary = build_summary(item)
                item.deadline = extract_deadline(item.body)
                item.location = extract_location(item.body)
                item.audience = extract_audience(item.body)

    ai_enhance(todo, cfg)
    todo.sort(key=lambda item: (-item.score, -item.date.timestamp()))
    write_digest(todo, now, cfg)

    max_score = max((item.score for item in todo), default=0)
    if in_quiet_hours(now, cfg) and max_score < int(cfg["notify"]["urgent_score"]):
        LOG.info("静默时段，暂缓推送（最高分 %s），留待早上", max_score)
        if not args.dry_run:
            state["pending_items"] = [item.to_dict() for item in todo[:30]]
            state["last_run"] = now.isoformat()
            save_state(state)
        if not args.quiet:
            print(format_message(todo, now, cfg))
        return 0

    message = format_message(todo, now, cfg)
    should_send = bool(
        cfg["notify"].get("enabled", True)
        and (
            cfg["notify"].get("send_empty", False)
            or any(item.score >= int(cfg["notify"]["min_score"]) for item in todo)
        )
    )
    if not should_send:
        LOG.info("没有达到推送阈值的通知，只写本地摘要")
        if not args.dry_run:
            mark_seen(state, todo, now)
            state["pending_items"] = []
            state["last_run"] = now.isoformat()
            save_state(state)
        if not args.quiet:
            print(message)
        return 0

    code = send_wechat(message, cfg, dry_run=args.dry_run)
    if not args.dry_run and code in (0, 3):
        mark_seen(state, todo, now)
        state["pending_items"] = []
        state["last_run"] = now.isoformat()
        save_state(state)
        LOG.info("推送完成，处理 %s 条", len(todo))
    elif not args.dry_run:
        # 发送失败：留到下一轮重试，不标记已读
        state["pending_items"] = [item.to_dict() for item in todo[:30]]
        state["last_run"] = now.isoformat()
        save_state(state)
        LOG.warning("推送失败（%s），已留待下一轮重试", code)

    if not args.quiet and not args.dry_run:
        print(message)
    return 0 if code in (0, 3) or args.dry_run else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="吉林大学 OA 通知监控")
    parser.add_argument("--check", action="store_true", help="只检查抓取，不生成摘要")
    parser.add_argument("--baseline", action="store_true", help="首次运行：建立已读基线并发送启动消息")
    parser.add_argument("--init-only", action="store_true", help="配合 --baseline：只建立基线，不发送")
    parser.add_argument("--dry-run", action="store_true", help="打印摘要，不发送、不写状态")
    parser.add_argument("--test-send", action="store_true", help="测试微信通道")
    parser.add_argument("--max-new", type=int, default=0, help="本轮最多处理多少条新通知")
    parser.add_argument("--quiet", action="store_true", help="不打印到控制台（计划任务用）")
    parser.add_argument(
        "--if-stale",
        type=float,
        default=0,
        metavar="MIN",
        help="距上次巡检不足 MIN 分钟就直接跳过（唤醒补跑用，避免重复推送）",
    )
    parser.add_argument("--verbose", action="store_true", help="控制台输出调试日志")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    setup_logging(verbose=args.verbose)
    locked = False
    if not args.dry_run and not args.check:
        if not acquire_lock():
            LOG.info("上一轮还没结束，跳过本次")
            if not args.quiet:
                print("上一轮还在跑，跳过。")
            return 0
        locked = True
    try:
        return run_once(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        LOG.exception("未处理异常")
        if not args.quiet:
            print(f"运行失败：{exc}")
        return 1
    finally:
        if locked:
            release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
