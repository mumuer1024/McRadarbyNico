"""活动日历展示与快照 diff。

真实约束（实测）：
- campaign-calendar 返回**按日期分组的营销文案**，没有结构化起止日期
- 没有适用范围、没有门店字段、没有参与条件
- 因此：
  * **不得**把文案里的日期当作活动有效期
  * 不得做倒计时
  * 范围 / 截止时间 / 适用门店一律标记「未知」
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .state import StateStore, iso_ts

__all__ = [
    "CampaignEntry",
    "parse_campaign_calendar",
    "diff_snapshot",
    "save_snapshot",
    "load_snapshot",
    "SNAPSHOT_KEY",
]

SNAPSHOT_KEY = "campaign_snapshot"

# 分组标题形态：2026年10月9日 今日 / 2026年10月7日 往期回顾 / 2026年10月10日
_GROUP_RE = re.compile(r"^\s*#*\s*(\d{4})年(\d{1,2})月(\d{1,2})日\s*(.*)$")


@dataclass
class CampaignEntry:
    date_group: str            # 仅作为「分组标签」，不是有效期
    day_tag: str = ""          # 今日 / 往期回顾 / 空
    title: str = ""
    body: str = ""
    image: str = ""
    valid_from: str | None = None   # 一律 None：接口未提供
    valid_to: str | None = None     # 一律 None
    scope: str = "未知"             # 全国/地区/门店/用户专属：接口未区分
    applicable_stores: str = "未知"

    @property
    def key(self) -> str:
        return f"{self.date_group}::{self.title}"

    def to_dict(self) -> dict:
        return {
            "dateGroup": self.date_group,
            "dayTag": self.day_tag,
            "title": self.title,
            "body": self.body,
            "image": self.image,
            "validFrom": self.valid_from,
            "validTo": self.valid_to,
            "scope": self.scope,
            "applicableStores": self.applicable_stores,
        }


def parse_campaign_calendar(raw) -> list[CampaignEntry]:
    text = _text(raw)
    if not text:
        return []
    entries: list[CampaignEntry] = []
    cur_group, cur_tag = "", ""
    cur_title, cur_body, cur_img = "", "", ""

    def flush():
        nonlocal cur_title, cur_body, cur_img
        if cur_title:
            entries.append(CampaignEntry(
                date_group=cur_group, day_tag=cur_tag, title=cur_title,
                body=cur_body.strip(), image=cur_img,
            ))
        cur_title, cur_body, cur_img = "", "", ""

    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        m = _GROUP_RE.match(s)
        if m:
            flush()
            cur_group = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            cur_tag = m.group(4).strip()
            continue
        # 编号列表必须形如 "1. xxx"（数字后紧跟". "）。
        # 不能放宽成 ^\d+\. —— 实测正文里存在 "13.9r 起随心拼" 这类小数开头的行，
        # 会被误判成新条目。
        if s.startswith("-") or s.startswith("*") or re.match(r"^\d{1,2}\.\s", s):
            flush()
            cur_title = re.sub(r"^([-*]\s*|\d+\.\s*)", "", s).strip()
            continue
        if "img" in s or s.startswith("!["):
            mm = re.search(r"\((https?://[^)]+)\)", s)
            if mm:
                cur_img = mm.group(1)
            continue
        if cur_title:
            cur_body += s + " "
    flush()
    return entries


def _text(raw) -> str:
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, dict):
        return ""
    for k in ("content", "text", "markdown"):
        v = raw.get(k)
        if isinstance(v, str):
            return v
    data = raw.get("data")
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for k in ("content", "text", "markdown"):
            if isinstance(data.get(k), str):
                return data[k]
    return ""


def save_snapshot(store: StateStore, entries: list[CampaignEntry]) -> dict:
    snap = {
        "captured_at": iso_ts(),
        "entries": [e.to_dict() for e in entries],
        "keys": [e.key for e in entries],
    }
    store.write(SNAPSHOT_KEY, snap)
    return snap


def load_snapshot(store: StateStore) -> dict | None:
    return store.read(SNAPSHOT_KEY)


def diff_snapshot(prev: dict | None, entries: list[CampaignEntry]) -> dict:
    cur_keys = [e.key for e in entries]
    cur_set = set(cur_keys)
    if prev is None:
        return {
            "first_run": True,
            "new": [], "gone": [], "still": [],
            "captured_at_prev": None,
            "note": "首次记录，无历史快照可对比",
        }
    prev_set = set(prev.get("keys") or [])
    new = sorted(cur_set - prev_set)
    gone = sorted(prev_set - cur_set)
    still = sorted(cur_set & prev_set)
    return {
        "first_run": False,
        "new": new,
        "gone": gone,
        "still": still,
        "captured_at_prev": prev.get("captured_at"),
        "note": "活动接口无结构化起止日期，本 diff 仅按「日期分组 + 标题」比对，"
                "不能据此推断活动有效期或截止时间",
    }


def render_entries(entries: list[CampaignEntry], limit: int = 20) -> str:
    if not entries:
        return "（未获取到活动数据）"
    lines = ["| 日期分组 | 标签 | 活动 | 有效期 | 适用范围 |", "|---|---|---|---|---|"]
    for e in entries[:limit]:
        lines.append(
            f"| {e.date_group} | {e.day_tag or '—'} | {e.title} | "
            f"{'未知（接口未提供）'} | {e.scope} |"
        )
    lines.append("")
    lines.append("> ⚠️ 活动接口未返回结构化起止日期与适用范围，上表中的日期仅为官方文案的分组标签，"
                 "**不作为活动有效期**，也不支持倒计时。")
    return "\n".join(lines)
