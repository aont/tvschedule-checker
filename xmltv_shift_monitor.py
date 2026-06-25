
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp
from lxml import etree
from zoneinfo import ZoneInfo


DEFAULT_GUIDE_URL = "https://animenosekai.github.io/japanterebi-xmltv/guide.xml"
DEFAULT_GUIDE_CACHE = "guide.xml"
DEFAULT_GUIDE_CACHE_MAX_AGE_SECONDS = 6 * 60 * 60

XMLTV_TIME_RE = re.compile(r"^(\d{14})(?:\s*([+-]\d{4}|Z))?$")

WEEKDAYS = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


@dataclass(frozen=True)
class Program:
    channel: str
    title: str
    start: datetime
    stop: datetime | None
    description: str = ""
    categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class Occurrence:
    rule_id: str
    rule: dict[str, Any]
    expected_start: datetime


@dataclass(frozen=True)
class Alert:
    kind: str
    occurrence: Occurrence
    program: Program | None
    delta_minutes: int | None


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def parse_time_of_day(value: str) -> time:
    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"invalid time {value!r}; use HH:MM or HH:MM:SS")

    hour, minute = int(parts[0]), int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    return time(hour=hour, minute=minute, second=second)


def parse_weekday(value: Any) -> int:
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise ValueError(f"weekday integer must be 0..6, got {value}")

    key = str(value).strip().lower()
    if key not in WEEKDAYS:
        raise ValueError(f"invalid weekday {value!r}; use mon/tue/.../sun")
    return WEEKDAYS[key]


def parse_xmltv_time(value: str, default_tz: ZoneInfo) -> datetime:
    match = XMLTV_TIME_RE.match(value.strip())
    if not match:
        raise ValueError(f"unsupported XMLTV time format: {value!r}")

    stamp, offset = match.groups()
    base = datetime.strptime(stamp, "%Y%m%d%H%M%S")

    if offset is None:
        return base.replace(tzinfo=default_tz)
    if offset == "Z":
        return base.replace(tzinfo=timezone.utc)

    sign = 1 if offset[0] == "+" else -1
    hours = int(offset[1:3])
    minutes = int(offset[3:5])
    tz = timezone(sign * timedelta(hours=hours, minutes=minutes))
    return base.replace(tzinfo=tz)


def normalize_title(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def title_matches(rule: dict[str, Any], guide_title: str) -> bool:
    wanted = str(rule["title"])
    mode = str(rule.get("title_match", "exact")).lower()

    if mode == "exact":
        return normalize_title(wanted) == normalize_title(guide_title)
    if mode == "contains":
        return normalize_title(wanted) in normalize_title(guide_title)
    if mode == "regex":
        return re.search(wanted, guide_title) is not None

    raise ValueError(f"unsupported title_match {mode!r}; use exact, contains, or regex")


def channel_matches(rule: dict[str, Any], guide_channel: str) -> bool:
    channel_spec = rule.get("channels", rule.get("channel"))
    if channel_spec in (None, ""):
        return True

    if isinstance(channel_spec, list):
        return guide_channel in {str(c) for c in channel_spec}

    return guide_channel == str(channel_spec)


def first_child_text(elem: etree._Element, local_name: str) -> str:
    for child in elem:
        if isinstance(child.tag, str) and etree.QName(child).localname == local_name:
            return (child.text or "").strip()
    return ""


def child_texts(elem: etree._Element, local_name: str) -> tuple[str, ...]:
    values: list[str] = []
    for child in elem:
        if isinstance(child.tag, str) and etree.QName(child).localname == local_name:
            text = (child.text or "").strip()
            if text:
                values.append(text)
    return tuple(values)


async def fetch_guide(session: aiohttp.ClientSession, url: str) -> bytes:
    headers = {"User-Agent": "xmltv-recording-shift-monitor/1.0"}
    async with session.get(url, headers=headers) as resp:
        body = await resp.read()
        if resp.status >= 400:
            raise RuntimeError(f"guide fetch failed: HTTP {resp.status}: {body[:200]!r}")
        return body


def cache_is_fresh(path: Path, max_age_seconds: int, now: datetime) -> bool:
    if max_age_seconds < 0 or not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=now.tzinfo)
    return now - mtime <= timedelta(seconds=max_age_seconds)


async def load_guide(
    session: aiohttp.ClientSession,
    url: str,
    cache_path: Path | None,
    cache_max_age_seconds: int,
    now: datetime,
    force_refresh: bool = False,
    log: Callable[[str], None] | None = None,
) -> bytes:
    if cache_path and not force_refresh and cache_is_fresh(cache_path, cache_max_age_seconds, now):
        if log:
            log(f"Using cached guide: {cache_path}")
        return cache_path.read_bytes()

    try:
        xml_bytes = await fetch_guide(session, url)
    except Exception:
        if cache_path and cache_path.exists():
            if log:
                log(f"Guide fetch failed; using cached guide: {cache_path}")
            return cache_path.read_bytes()
        raise

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(xml_bytes)
        if log:
            log(f"Cached guide: {cache_path}")

    return xml_bytes


def parse_guide(xml_bytes: bytes, tz: ZoneInfo) -> list[Program]:
    parser = etree.XMLParser(
        recover=True,
        huge_tree=True,
        resolve_entities=False,
        no_network=True,
    )
    root = etree.fromstring(xml_bytes, parser=parser)

    programs: list[Program] = []
    for elem in root.iter():
        if not isinstance(elem.tag, str) or etree.QName(elem).localname != "programme":
            continue

        start_raw = elem.get("start")
        if not start_raw:
            continue

        title = first_child_text(elem, "title")
        if not title:
            continue

        channel = elem.get("channel") or ""
        stop_raw = elem.get("stop")
        description = first_child_text(elem, "desc")
        categories = child_texts(elem, "category")

        try:
            start = parse_xmltv_time(start_raw, tz).astimezone(tz)
            stop = parse_xmltv_time(stop_raw, tz).astimezone(tz) if stop_raw else None
        except ValueError:
            continue

        programs.append(
            Program(
                channel=channel,
                title=title,
                start=start,
                stop=stop,
                description=description,
                categories=categories,
            )
        )

    return programs


def rule_occurrence_dates(
    rule: dict[str, Any],
    start_date: date,
    lookahead_days: int,
) -> list[date]:
    rec = str(rule.get("recurrence", "once")).lower()
    end_date = start_date + timedelta(days=lookahead_days - 1)

    rule_start_date = date.fromisoformat(rule["start_date"]) if rule.get("start_date") else None
    rule_end_date = date.fromisoformat(rule["end_date"]) if rule.get("end_date") else None

    def within_rule_range(d: date) -> bool:
        if rule_start_date and d < rule_start_date:
            return False
        if rule_end_date and d > rule_end_date:
            return False
        return True

    if rec == "daily":
        return [
            start_date + timedelta(days=i)
            for i in range(lookahead_days)
            if within_rule_range(start_date + timedelta(days=i))
        ]

    if rec == "weekly":
        raw_weekdays = rule.get("weekdays", rule.get("weekday"))
        if raw_weekdays is None:
            raise ValueError(f"weekly rule {rule.get('id')!r} needs weekday or weekdays")

        if isinstance(raw_weekdays, list):
            weekdays = {parse_weekday(w) for w in raw_weekdays}
        else:
            weekdays = {parse_weekday(raw_weekdays)}

        return [
            start_date + timedelta(days=i)
            for i in range(lookahead_days)
            if (start_date + timedelta(days=i)).weekday() in weekdays
            and within_rule_range(start_date + timedelta(days=i))
        ]

    if rec in {"once", "one_time", "one-time", "oneoff", "one-off"}:
        if not rule.get("date"):
            raise ValueError(f"one-time rule {rule.get('id')!r} needs date")
        d = date.fromisoformat(rule["date"])
        return [d] if start_date <= d <= end_date and within_rule_range(d) else []

    raise ValueError(f"unsupported recurrence {rec!r}; use daily, weekly, or once")


def expand_occurrences(
    config: dict[str, Any],
    tz: ZoneInfo,
    now: datetime,
) -> list[Occurrence]:
    lookahead_days = int(config.get("lookahead_days", 14))
    start_date = now.date()

    occurrences: list[Occurrence] = []
    for rule in config.get("recordings", []):
        if "id" not in rule:
            raise ValueError("each recording rule needs an id")
        if "title" not in rule:
            raise ValueError(f"rule {rule.get('id')!r} needs title")
        if "time" not in rule:
            raise ValueError(f"rule {rule.get('id')!r} needs time")

        tod = parse_time_of_day(str(rule["time"]))
        for d in rule_occurrence_dates(rule, start_date, lookahead_days):
            expected = datetime.combine(d, tod).replace(tzinfo=tz)
            occurrences.append(
                Occurrence(
                    rule_id=str(rule["id"]),
                    rule=rule,
                    expected_start=expected,
                )
            )

    return occurrences


def find_best_program(
    programs: list[Program],
    rule: dict[str, Any],
    expected_start: datetime,
    window: timedelta,
) -> Program | None:
    low = expected_start - window
    high = expected_start + window

    candidates = [
        p
        for p in programs
        if low <= p.start <= high
        and channel_matches(rule, p.channel)
        and title_matches(rule, p.title)
    ]

    if not candidates:
        return None

    return min(
        candidates,
        key=lambda p: abs((p.start - expected_start).total_seconds()),
    )


def minute_floor(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def occurrence_key(occurrence: Occurrence) -> str:
    # Include the registered HH:MM so a later schedule edit does not collide
    # with an older notification state for the same title/date.
    return (
        f"{occurrence.rule_id}|"
        f"{occurrence.expected_start.date().isoformat()}|"
        f"{occurrence.expected_start.strftime('%H:%M')}"
    )


def detect_changes(
    programs: list[Program],
    occurrences: list[Occurrence],
    config: dict[str, Any],
    state: dict[str, Any],
    now: datetime,
) -> list[Alert]:
    notified = state.setdefault("notified", {})
    alerts: list[Alert] = []

    threshold = timedelta(minutes=int(config.get("shift_threshold_minutes", 0)))
    window = timedelta(hours=float(config.get("matching_window_hours", 12)))
    notify_missing = bool(config.get("notify_missing", False))
    notify_recovery = bool(config.get("notify_recovery", False))

    for occ in occurrences:
        key = occurrence_key(occ)
        existing = notified.get(key, {})
        program = find_best_program(programs, occ.rule, occ.expected_start, window)

        if program is None:
            if notify_missing and existing.get("status") != "missing":
                alerts.append(Alert(kind="missing", occurrence=occ, program=None, delta_minutes=None))
                notified[key] = {
                    "status": "missing",
                    "expected_start": occ.expected_start.isoformat(),
                    "notified_at": now.isoformat(),
                }
            continue

        expected = minute_floor(occ.expected_start)
        actual = minute_floor(program.start)
        delta = actual - expected
        delta_minutes = int(delta.total_seconds() // 60)

        if abs(delta) <= threshold:
            if existing.get("status") == "shifted" and notify_recovery:
                alerts.append(Alert(kind="recovered", occurrence=occ, program=program, delta_minutes=0))
            notified.pop(key, None)
            continue

        actual_marker = actual.isoformat()
        if existing.get("status") == "shifted" and existing.get("actual_start") == actual_marker:
            continue

        alerts.append(Alert(kind="shifted", occurrence=occ, program=program, delta_minutes=delta_minutes))
        notified[key] = {
            "status": "shifted",
            "title": program.title,
            "channel": program.channel,
            "expected_start": expected.isoformat(),
            "actual_start": actual_marker,
            "delta_minutes": delta_minutes,
            "notified_at": now.isoformat(),
        }

    return alerts


def search_programs(
    programs: list[Program],
    query: str,
    match_mode: str = "contains",
) -> list[Program]:
    rule = {"title": query, "title_match": match_mode}
    return sorted(
        [program for program in programs if title_matches(rule, program.title)],
        key=lambda program: (program.start, program.channel, program.title),
    )


def format_program_search_results(programs: list[Program], tz: ZoneInfo, limit: int) -> str:
    if not programs:
        return "No matching programmes found."

    selected = programs[:limit]
    lines = [f"Found {len(programs)} matching programme(s); showing {len(selected)}.", ""]
    for program in selected:
        local_start = program.start.astimezone(tz)
        local_stop = program.stop.astimezone(tz) if program.stop else None
        lines.extend(
            [
                program.title,
                f"  channel: {program.channel}",
                f"  start: {fmt_dt(local_start)}",
                f"  stop: {fmt_dt(local_stop) if local_stop else 'unknown'}",
                f"  recordings.json helper: title={json.dumps(program.title, ensure_ascii=False)}, "
                f"channel={json.dumps(program.channel, ensure_ascii=False)}, "
                f"weekday={local_start.strftime('%a').lower()}, time={local_start.strftime('%H:%M')}",
            ]
        )
        if program.categories:
            lines.append(f"  categories: {', '.join(program.categories)}")
        if program.description:
            lines.append(f"  description: {program.description}")
        lines.append("")

    return "\n".join(lines).rstrip()


def prune_state(state: dict[str, Any], now: datetime, retention_days: int) -> None:
    cutoff = now.date() - timedelta(days=retention_days)
    notified = state.setdefault("notified", {})

    for key in list(notified.keys()):
        parts = key.split("|")
        if len(parts) < 2:
            continue
        try:
            occurrence_date = date.fromisoformat(parts[1])
        except ValueError:
            continue
        if occurrence_date < cutoff:
            del notified[key]


def slack_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %a %H:%M %Z")


def fmt_delta(delta_minutes: int) -> str:
    sign = "+" if delta_minutes > 0 else ""
    return f"{sign}{delta_minutes} min"


def format_alerts(alerts: list[Alert], guide_url: str, now: datetime) -> str:
    lines = [
        ":warning: *XMLTV recording schedule change detected*",
        f"Checked: {fmt_dt(now)}",
        f"Guide: {guide_url}",
        "",
    ]

    for alert in alerts:
        occ = alert.occurrence
        rule = occ.rule
        registered_title = slack_escape(str(rule["title"]))
        registered_channel = slack_escape(str(rule.get("channel", rule.get("channels", "any"))))

        if alert.kind == "missing":
            lines.extend(
                [
                    f"*{registered_title}*",
                    f"• Rule: `{slack_escape(occ.rule_id)}`",
                    f"• Channel: `{registered_channel}`",
                    f"• Registered start: `{fmt_dt(occ.expected_start)}`",
                    "• Status: no matching programme found near the registered time",
                    "",
                ]
            )
            continue

        assert alert.program is not None
        program = alert.program
        guide_title = slack_escape(program.title)
        channel = slack_escape(program.channel)

        if alert.kind == "recovered":
            status = "back to registered start time"
        else:
            assert alert.delta_minutes is not None
            status = f"shifted `{fmt_delta(alert.delta_minutes)}`"

        lines.extend(
            [
                f"*{guide_title}*",
                f"• Rule: `{slack_escape(occ.rule_id)}`",
                f"• Channel: `{channel}`",
                f"• Registered start: `{fmt_dt(occ.expected_start)}`",
                f"• Current guide start: `{fmt_dt(program.start)}`",
                f"• Status: {status}",
                "",
            ]
        )

    return "\n".join(lines).rstrip()


async def send_slack(
    session: aiohttp.ClientSession,
    webhook_url: str,
    text: str,
) -> None:
    async with session.post(webhook_url, json={"text": text}) as resp:
        body = await resp.text()
        if resp.status >= 300:
            raise RuntimeError(f"Slack webhook failed: HTTP {resp.status}: {body[:500]}")


async def run(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    state_path = Path(args.state)

    config = load_json(config_path, {})
    tz = ZoneInfo(str(config.get("timezone", "Asia/Tokyo")))
    guide_url = str(config.get("guide_url", DEFAULT_GUIDE_URL))
    now = datetime.now(tz)

    cache_arg = getattr(args, "guide_cache", None)
    cache_config = config.get("guide_cache", DEFAULT_GUIDE_CACHE)
    cache_value = cache_arg if cache_arg is not None else cache_config
    cache_path = Path(str(cache_value)) if cache_value else None
    cache_max_age = int(config.get("guide_cache_max_age_seconds", DEFAULT_GUIDE_CACHE_MAX_AGE_SECONDS))
    if getattr(args, "guide_cache_max_age_seconds", None) is not None:
        cache_max_age = int(args.guide_cache_max_age_seconds)

    timeout = aiohttp.ClientTimeout(total=int(config.get("http_timeout_seconds", 60)))

    async with aiohttp.ClientSession(timeout=timeout) as session:
        xml_bytes = await load_guide(
            session,
            guide_url,
            cache_path,
            cache_max_age,
            now,
            force_refresh=bool(getattr(args, "refresh_guide", False)),
        )
        programs = parse_guide(xml_bytes, tz)

        if getattr(args, "search_title", None):
            matches = search_programs(programs, args.search_title, args.search_title_match)
            print(format_program_search_results(matches, tz, int(args.search_limit)))
            return 0

        occurrences = expand_occurrences(config, tz, now)
        new_state = copy.deepcopy(load_json(state_path, {"notified": {}}))
        prune_state(new_state, now, int(config.get("state_retention_days", 30)))

        alerts = detect_changes(programs, occurrences, config, new_state, now)

        if alerts:
            text = format_alerts(alerts, guide_url, now)

            if args.dry_run:
                print(text)
            else:
                webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
                if not webhook_url:
                    raise RuntimeError("SLACK_WEBHOOK_URL is not set; use --dry-run to test locally")
                await send_slack(session, webhook_url, text)
                save_json_atomic(state_path, new_state)

            print(f"{len(alerts)} alert(s) generated")
        else:
            if not args.dry_run:
                save_json_atomic(state_path, new_state)
            print(
                f"No shifts detected. Programs parsed={len(programs)}, "
                f"occurrences checked={len(occurrences)}"
            )

    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detect XMLTV recording start-time shifts.")
    parser.add_argument("--config", default="recordings.json", help="recording schedule JSON")
    parser.add_argument("--state", default="state.json", help="notification state JSON")
    parser.add_argument("--dry-run", action="store_true", help="print Slack message instead of sending")
    parser.add_argument("--guide-cache", default=None, help="local guide.xml cache path; set config guide_cache to empty to disable")
    parser.add_argument("--guide-cache-max-age-seconds", type=int, default=None, help="reuse guide cache while it is this fresh")
    parser.add_argument("--refresh-guide", action="store_true", help="download guide.xml even when the local cache is fresh")
    parser.add_argument("--search-title", help="search guide.xml by programme title and print recording details")
    parser.add_argument(
        "--search-title-match",
        choices=["exact", "contains", "regex"],
        default="contains",
        help="title matching mode for --search-title",
    )
    parser.add_argument("--search-limit", type=int, default=25, help="maximum search results to display")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
