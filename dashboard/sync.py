#!/usr/bin/env python3
"""Build the Moodle Robot dashboard from your inbox.

The robot mails a report after every run ("[moodle-robot] <run> ..."), each
with a machine-readable ``changes-<run>.json`` attachment. This script
collects those attachments (plus the JKU Moodle notification mails that land
in the same inbox), throws away the noise, and writes ``data.js`` next to
``index.html``.

Two ways to feed it:

  # 1. Straight from Gmail over IMAP (needs an app password, see README)
  GMAIL_USER=you@gmail.com GMAIL_APP_PASSWORD=xxxx python3 dashboard/sync.py

  # 2. From attachments you already saved (no network)
  python3 dashboard/sync.py --reports path/to/folder-with-changes-json

Standard library only. Messages are read with BODY.PEEK, so nothing in your
inbox gets marked as read.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email
import email.policy
import email.utils
import imaplib
import json
import os
import pathlib
import re
import sys
from collections import Counter

HERE = pathlib.Path(__file__).resolve().parent

DEFAULT_ROBOT_QUERY = 'subject:"[moodle-robot]"'
DEFAULT_INBOX_QUERY = 'from:noreply@jku.at subject:"JKU Moodle"'

REPORT_FILE_RE = re.compile(r"^changes-(?P<run>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{4}Z)\.json$")
SUBJECT_RUN_RE = re.compile(r"\[moodle-robot\]\s+(?P<run>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{4}Z)")
COURSE_RE = re.compile(
    r"^(?P<type>[A-Z]{2,3}(?:\s*/\s*[A-Z]{2,3})*)\s+(?P<name>.+?)\s+(?P<code>\d{3}\.[0-9A-Z]{3})$"
)

# Activity types that are something you have to *do* (vs. something to read).
TASK_MODS = {"assign", "quiz", "checkmark", "feedback", "choice", "workshop", "lesson", "h5pactivity"}
# Meeting links whose "open" date rolls forward every week. Pure churn.
MEETING_MODS = {"zoom", "bigbluebuttonbn", "opencast", "webex", "teams"}

CAT_TASKS, CAT_CONTENT, CAT_FORUM = "tasks", "content", "forum"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def iso_to_epoch(value: str | None) -> int | None:
    if not value:
        return None
    return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def parse_course(full: str) -> dict:
    """'VL / UE Mathematics for AI III 357.260' -> type, name, code."""
    m = COURSE_RE.match(full or "")
    if not m:
        return {"full": full, "type": "", "name": full or "Unknown course", "code": ""}
    name = m.group("name")
    # 'Freiwilligenarbeit im Sozialbereich - „Lernen...“' -> keep the head
    name = re.split(r"\s+[-–]\s+", name, maxsplit=1)[0]
    return {"full": full, "type": re.sub(r"\s+", " ", m.group("type")), "name": name, "code": m.group("code")}


def grade_value(after) -> tuple[str | None, float | None]:
    """'100.00 Grade analysis' -> ('100.00', 100.0); '- Grade analysis' -> (None, None)."""
    if not isinstance(after, str):
        return None, None
    m = re.match(r"\s*(-?\d+(?:[.,]\d+)?)", after)
    if not m:
        return None, None
    raw = m.group(1).replace(",", ".")
    return raw, float(raw)


def pct_value(pct) -> float | None:
    if not isinstance(pct, str):
        return None
    m = re.match(r"\s*(\d+(?:[.,]\d+)?)\s*%", pct)
    return float(m.group(1).replace(",", ".")) if m else None


def post_excerpt(detail: dict) -> str:
    """Strip the '<title> by <author> - <weekday, date, time>' prefix Moodle puts in front."""
    text = (detail.get("text") or "").strip()
    text = re.sub(
        r"^.*?\s-\s(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Montag|Dienstag|Mittwoch|"
        r"Donnerstag|Freitag|Samstag|Sonntag),\s[^,]+,\s\d{1,2}:\d{2}(?:\s?[AP]M)?\s+",
        "",
        text,
        count=1,
    )
    return text[:420]


# --------------------------------------------------------------------------
# Turning robot changes into dashboard events
# --------------------------------------------------------------------------

def noise_reason(c: dict) -> str | None:
    """Return why a change is noise, or None if it is worth showing."""
    kind, detail = c.get("kind"), c.get("detail") or {}
    if c.get("sev") == "noise":
        return "robot marked noise"
    if kind == "item_status_changed" and c.get("mod") == "checkmark":
        before, after = detail.get("before") or {}, detail.get("after") or {}
        if before.get("checked") == after.get("checked") and before.get("examples") == after.get("examples"):
            return "checkmark hash churn"
    if kind == "grade_changed" and grade_value(detail.get("after"))[0] is None:
        return "empty grade placeholder"
    if kind == "item_date_changed" and c.get("mod") in MEETING_MODS:
        return "recurring meeting link"
    if kind == "item_text_changed" and not detail.get("addedCount") and not detail.get("removedCount"):
        return "text change with no diff"
    if kind == "file_added" and "mod_forum" in str(c.get("key", "")):
        return "forum attachment"
    return None


def change_to_event(c: dict, run: dict) -> dict:
    kind, detail = c.get("kind"), c.get("detail") or {}
    mod = c.get("mod")
    ev = {
        "id": c.get("id"),
        "run": run["runId"],
        "ts": run["finishedTs"],
        "sev": c.get("sev", "info"),
        "course": c.get("course", ""),
        "section": c.get("section") or "",
        "item": c.get("item") or "",
        "url": c.get("url"),
        "cmId": c.get("cmId"),
        "title": c.get("title", ""),
    }

    if kind == "post_added":
        is_announcement = (c.get("title", "").startswith("Announcement") or
                           re.search(r"announce|ankündig|nachrichten", str(detail.get("forum", "")), re.I))
        ev.update(
            cat=CAT_FORUM,
            type="announcement" if is_announcement else "post",
            ts=detail.get("ts") or run["finishedTs"],
            title=detail.get("discussion") or c.get("item"),
            forum=detail.get("forum"),
            author=detail.get("author"),
            staff=bool(detail.get("byTeacher") or is_announcement),
            excerpt=post_excerpt(detail),
            key=c.get("key") or c.get("id"),
        )
    elif kind == "grade_changed":
        raw, val = grade_value(detail.get("after"))
        rng = re.search(r"range\s+(\d+(?:[.,]\d+)?)\s*[–-]\s*(\d+(?:[.,]\d+)?)", c.get("title", ""))
        top = float(rng.group(2).replace(",", ".")) if rng else None
        pct = pct_value(detail.get("pct"))
        if pct is None and val is not None and top:
            pct = round(val / top * 100, 1)
        ev.update(cat=CAT_TASKS, type="grade", score=raw, max=top, pct=pct,
                  feedback=(detail.get("feedback") or "").strip() or None,
                  key=f"grade:{c.get('course')}:{c.get('item')}")
    elif kind == "item_status_changed" and (detail.get("after") or {}).get("grade"):
        after = detail["after"]
        m = re.match(r"\s*([\d.,]+)\s*/\s*([\d.,]+)", str(after.get("grade")))
        val = top = pct = None
        if m:
            val, top = float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", "."))
            pct = round(val / top * 100, 1) if top else None
        ev.update(cat=CAT_TASKS, type="grade", score=m.group(1) if m else after.get("grade"),
                  max=top, pct=pct, feedback=(after.get("feedback") or None),
                  key=f"grade:{c.get('course')}:{c.get('item')}")
    elif kind == "item_added" and mod in TASK_MODS:
        dates = detail.get("dates") or {}
        ev.update(cat=CAT_TASKS, type="new-task", mod=mod, due=dates.get("due") or dates.get("close"),
                  opens=dates.get("open"), excerpt=(detail.get("intro") or "")[:300],
                  key=f"task:{c.get('cmId')}")
    elif kind == "item_added":
        ev.update(cat=CAT_CONTENT, type="new-content", mod=mod, excerpt=(detail.get("intro") or "")[:300],
                  target=detail.get("target"), key=c.get("id"))
    elif kind == "file_added":
        ev.update(cat=CAT_CONTENT, type="new-file", file=c.get("file"), key=c.get("key") or c.get("id"))
    elif kind == "section_added":
        ev.update(cat=CAT_CONTENT, type="new-section", title=c.get("section") or c.get("title"), key=c.get("id"))
    elif kind == "section_renamed":
        ev.update(cat=CAT_CONTENT, type="section-renamed", key=c.get("id"))
    elif kind == "item_text_changed":
        ev.update(cat=CAT_CONTENT, type="text-changed", key=c.get("id"))
    elif kind == "item_date_changed":
        ev.update(cat=CAT_TASKS, type="date-changed", role=detail.get("role"),
                  before=detail.get("before"), after=detail.get("after"),
                  key=f"date:{c.get('cmId')}:{detail.get('role')}")
    elif kind == "item_status_changed" and mod == "checkmark":
        ev.update(cat=CAT_TASKS, type="status", mod=mod, checked=(detail.get("after") or {}).get("checked"),
                  examples=(detail.get("after") or {}).get("examples"),
                  before=detail.get("before"), after=detail.get("after"), key=f"status:{c.get('cmId')}")
    else:  # item_status_changed (non-grade) and anything new the robot learns
        ev.update(cat=CAT_TASKS if mod in TASK_MODS else CAT_CONTENT, type="status", mod=mod,
                  before=detail.get("before"), after=detail.get("after"),
                  key=f"status:{c.get('cmId') or c.get('id')}")
    ev["src"] = "change"
    return ev


def action_event_to_event(a: dict, run: dict) -> dict | None:
    stage = a.get("stage")
    if stage not in ("new", "done"):
        return None  # reminders / missed are shown in the deadline panel instead
    return {
        "id": f"{run['runId']}:{a.get('key')}:{stage}",
        "run": run["runId"],
        "ts": run["finishedTs"],
        "sev": "important",
        "cat": CAT_TASKS,
        "type": "todo" if stage == "new" else "done",
        "course": a.get("course", ""),
        "section": "",
        "item": a.get("item", ""),
        "title": a.get("item", ""),
        "url": a.get("url"),
        "cmId": a.get("cmId"),
        "mod": a.get("mod"),
        "due": a.get("due"),
        "opens": a.get("open"),
        "todo": a.get("kind"),
        "optional": bool((a.get("flags") or {}).get("optional")),
        "key": f"task:{a.get('cmId')}" if stage == "new" else f"done:{a.get('key')}",
        "src": "action",
    }


def drop_reverted(events: list[dict], filtered: Counter) -> list[dict]:
    """A date or status that goes X -> Y and later Y -> X is a scraping hiccup, not news."""
    flips = [e for e in events if e["type"] in ("date-changed", "status")]
    dead = set()
    for i, a in enumerate(flips):
        for b in flips[i + 1:]:
            if a["key"] == b["key"] and a.get("before") == b.get("after") and a.get("after") == b.get("before"):
                dead.update({a["id"], b["id"]})
    filtered["reverted change"] += len(dead)
    return [e for e in events if e["id"] not in dead]


def drop_status_covered_by_done(events: list[dict], filtered: Counter) -> list[dict]:
    """'Status changed: Cross: Exercise 4' says the same thing as 'Done: Cross: Exercise 4'."""
    done = {(e["course"], e["item"]) for e in events if e["type"] == "done"}
    keep = [e for e in events if not (e["type"] == "status" and (e["course"], e["item"]) in done)]
    filtered["duplicate"] += len(events) - len(keep)
    return keep


def normalise_action(a: dict) -> dict:
    flags = a.get("flags") or {}
    return {
        "key": a.get("key"),
        "item": a.get("item", ""),
        "course": a.get("course", ""),
        "mod": a.get("mod"),
        "todo": a.get("kind"),
        "due": a.get("due"),
        "opens": a.get("open"),
        "cutoff": a.get("cutoff"),
        "optional": bool(flags.get("optional")),
        "missed": bool(flags.get("missed")),
        "noDeadline": bool(flags.get("noDeadline")) or not a.get("due"),
        "options": flags.get("options"),
        "note": a.get("note") or "",
        "url": a.get("url"),
    }


# --------------------------------------------------------------------------
# Inbox (JKU Moodle notification mails)
# --------------------------------------------------------------------------

INBOX_PATTERNS = [
    ("signin", re.compile(r"new sign[- ]in", re.I)),
    ("submitted", re.compile(r"submission confirmation\s*[-–:]\s*(?P<title>.+)$", re.I)),
    ("due", re.compile(r"^Due on (?P<when>.+?(?:[AP]M|\d{2}:\d{2})):\s+(?P<title>.+)$", re.I)),
    ("opens", re.compile(r"^Opens on (?P<when>.+?(?:[AP]M|\d{2}:\d{2})):\s+(?P<title>.+)$", re.I)),
    ("closes", re.compile(r"^Closes on (?P<when>.+?(?:[AP]M|\d{2}:\d{2})):\s+(?P<title>.+)$", re.I)),
    ("digest", re.compile(r"assignments due in", re.I)),
    ("content", re.compile(r"^(?P<title>.+?)\s+(?:new content|content change)$", re.I)),
]


def classify_inbox(subject: str) -> dict:
    clean = re.sub(r"^[^:]{0,40}\bMoodle:\s*", "", subject or "").strip()
    for kind, rx in INBOX_PATTERNS:
        m = rx.search(clean)
        if m:
            g = m.groupdict()
            title = g.get("title") or clean
            if kind == "content":
                # '344.053, KV Special Topics, Markus Schedl / Shah Nawaz, 2026W' -> course + code
                parts = [p.strip() for p in title.split(",")]
                if len(parts) >= 2 and re.match(r"\d{3}\.[0-9A-Z]{3}$", parts[0]):
                    title = f"{parts[1]} {parts[0]}"
                title += " — " + ("new content" if "new content" in clean.lower() else "content changed")
            return {"kind": kind, "title": title.strip(), "when": g.get("when")}
    return {"kind": "other", "title": clean, "when": None}


def normalise_inbox(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        info = classify_inbox(r.get("subject", ""))
        out.append({"id": r.get("id"), "subject": r.get("subject", ""), "ts": iso_to_epoch(r.get("date")), **info})
    out.sort(key=lambda r: r["ts"] or 0, reverse=True)
    return out


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def build(reports: list[dict], inbox_rows: list[dict] | None = None, *, days: int = 7,
          name: str | None = None, tz: str = "Europe/Vienna", source: str = "files") -> dict:
    if not reports:
        raise SystemExit("No robot reports found - nothing to build.")

    runs = []
    for r in reports:
        s = r["summary"]
        runs.append({
            "runId": s["runId"],
            "mode": s.get("mode"),
            "status": s.get("status"),
            "baseline": bool(s.get("baseline")),
            "startedTs": iso_to_epoch(s.get("startedAt")),
            "finishedTs": iso_to_epoch(s.get("finishedAt")),
            "raw": len(r.get("changes", [])),
            "kept": 0,
            "keptChanges": 0,
            "actionEvents": len(r.get("actionEvents", [])),
            "openActions": len(r.get("actions", [])),
            "errors": (s.get("counts") or {}).get("errors", 0),
            "messages": s.get("messages") or [],
        })
    order = sorted(range(len(reports)), key=lambda i: runs[i]["startedTs"] or 0)
    reports = [reports[i] for i in order]
    runs = [runs[i] for i in order]
    latest_report, latest_run = reports[-1], runs[-1]
    window_start = latest_run["finishedTs"] - days * 86400
    in_window = [i for i, r in enumerate(runs) if (r["finishedTs"] or 0) >= window_start]
    reports = [reports[i] for i in in_window]
    runs = [runs[i] for i in in_window]

    filtered: Counter = Counter()
    events: list[dict] = []
    for report, run in zip(reports, runs):
        for c in report.get("changes", []):
            reason = noise_reason(c)
            if reason:
                filtered[reason] += 1
                continue
            events.append(change_to_event(c, run))
        if run["baseline"]:
            continue  # the baseline lists every existing to-do as "new"
        for a in report.get("actionEvents", []):
            ev = action_event_to_event(a, run)
            if ev:
                events.append(ev)

    events = drop_reverted(events, filtered)

    # De-duplicate: a new assignment shows up as item_added *and* as a "new" action
    # event; a grade shows up as grade_changed *and* as a status change. Keep one,
    # merging fields, and keep the newest value of anything that changed twice.
    merged: dict[str, dict] = {}
    for ev in sorted(events, key=lambda e: e["ts"] or 0):
        key = ev.get("key") or ev["id"]
        if key in merged:
            prev = merged[key]
            if ev["type"] in ("grade", "status"):  # newest value wins
                prev.update({k: v for k, v in ev.items() if v is not None and k not in ("id", "run", "src")})
            else:
                for k, v in ev.items():
                    if prev.get(k) in (None, "") and v not in (None, ""):
                        prev[k] = v
            filtered["duplicate"] += 1
            continue
        merged[key] = dict(ev)
    events = drop_status_covered_by_done(list(merged.values()), filtered)

    by_run = {r["runId"]: r for r in runs}
    for e in events:
        run = by_run[e["run"]]
        run["kept"] += 1
        if e["src"] == "change":
            run["keptChanges"] += 1
    for run in runs:
        run["noise"] = run["raw"] - run["keptChanges"]
    for e in events:  # internal bookkeeping, not needed by the page
        for k in ("before", "after", "src"):
            if e["type"] != "date-changed" or k == "src":
                e.pop(k, None)
    events.sort(key=lambda e: e["ts"] or 0, reverse=True)

    courses = {}
    for full in {e["course"] for e in events} | {a.get("course", "") for a in latest_report.get("actions", [])}:
        if full:
            courses[full] = parse_course(full)

    grades = [e for e in events if e["type"] == "grade"]
    raw_total = sum(r["raw"] for r in runs)
    s = latest_report["summary"]
    return {
        "version": 1,
        "generatedTs": int(dt.datetime.now(dt.timezone.utc).timestamp()),
        "source": source,
        "name": name,
        "tz": tz,
        "windowDays": days,
        "windowStartTs": window_start,
        "latestRun": {**latest_run, "counts": s.get("counts") or {}, "files": s.get("files") or {}},
        "runs": runs,
        "feed": events,
        "actions": [normalise_action(a) for a in latest_report.get("actions", [])],
        "grades": grades,
        "inbox": normalise_inbox(inbox_rows or []),
        "courses": courses,
        "filtered": {"raw": raw_total, "noise": sum(r["noise"] for r in runs), "kept": len(events),
                     "reasons": dict(filtered.most_common())},
    }


# --------------------------------------------------------------------------
# Sources: local folder or Gmail IMAP
# --------------------------------------------------------------------------

def load_reports_from_dir(folder: pathlib.Path) -> list[dict]:
    reports = []
    for path in sorted(folder.glob("changes-*.json")):
        if REPORT_FILE_RE.match(path.name):
            reports.append(json.loads(path.read_text(encoding="utf-8")))
    return reports


def extract_report(raw_message: bytes) -> tuple[str, dict] | None:
    """Pull the changes-<run>.json attachment out of one robot mail."""
    msg = email.message_from_bytes(raw_message, policy=email.policy.default)
    for part in msg.iter_attachments():
        filename = part.get_filename() or ""
        if REPORT_FILE_RE.match(filename):
            payload = part.get_payload(decode=True) or b""
            return filename, json.loads(payload.decode("utf-8"))
    return None


def parse_header_block(raw_headers: bytes, uid: str) -> dict:
    msg = email.message_from_bytes(raw_headers, policy=email.policy.default)
    date = msg.get("Date")
    try:
        when = email.utils.parsedate_to_datetime(date).astimezone(dt.timezone.utc).isoformat() if date else None
    except (TypeError, ValueError):
        when = None
    return {"id": msg.get("Message-ID") or uid, "from": str(msg.get("From", "")),
            "subject": str(msg.get("Subject", "")), "date": when}


class Gmail:
    """Just enough IMAP to search with Gmail syntax and read without marking as read."""

    def __init__(self, user: str, password: str, host: str = "imap.gmail.com"):
        self.imap = imaplib.IMAP4_SSL(host)
        self.imap.login(user, password)
        self._select_all_mail()

    def _select_all_mail(self) -> None:
        # The "All Mail" folder name is localised ("[Gmail]/Alle Nachrichten"), so find it by flag.
        _, folders = self.imap.list()
        for raw in folders or []:
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            if "\\All" in line:
                m = re.search(r'"([^"]+)"\s*$', line) or re.search(r"(\S+)\s*$", line)
                if m and self.imap.select(f'"{m.group(1)}"', readonly=True)[0] == "OK":
                    return
        self.imap.select("INBOX", readonly=True)

    def search(self, gmail_query: str) -> list[bytes]:
        self.imap.literal = gmail_query.encode("utf-8")
        typ, data = self.imap.uid("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW")
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed for {gmail_query!r}: {data}")
        return (data[0] or b"").split()

    def headers(self, uids: list[bytes]) -> list[dict]:
        if not uids:
            return []
        typ, data = self.imap.uid(
            "FETCH", b",".join(uids), "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE FROM MESSAGE-ID)])")
        rows = []
        for item in data or []:
            if isinstance(item, tuple):
                m = re.search(rb"UID (\d+)", item[0])
                rows.append({"uid": m.group(1) if m else None, **parse_header_block(item[1], "")})
        return rows

    def body(self, uid: bytes) -> bytes:
        typ, data = self.imap.uid("FETCH", uid, "(BODY.PEEK[])")
        for item in data or []:
            if isinstance(item, tuple):
                return item[1]
        raise RuntimeError(f"Could not fetch message {uid!r}")

    def close(self) -> None:
        try:
            self.imap.logout()
        except Exception:
            pass


def gmail_credentials() -> tuple[str, str]:
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not password:
        raise SystemExit("Set GMAIL_USER and GMAIL_APP_PASSWORD (see dashboard/README.md), "
                         "or pass --reports DIR to build from saved attachments.")
    return user, password


def sync_from_gmail(args, cache: pathlib.Path) -> None:
    fetch_gmail(*gmail_credentials(), cache, days=args.days, robot_query=args.robot_query,
                inbox_query=args.inbox_query, with_inbox=not args.no_inbox)


def fetch_gmail(user: str, password: str, cache: pathlib.Path, *, days: int = 7,
                robot_query: str = DEFAULT_ROBOT_QUERY, inbox_query: str = DEFAULT_INBOX_QUERY,
                with_inbox: bool = True, log=print) -> None:
    """Download new robot report attachments (and the Moodle notification list) into `cache`."""
    cache.mkdir(parents=True, exist_ok=True)
    lookback = days + 1
    gm = Gmail(user, password)
    try:
        heads = gm.headers(gm.search(f"{robot_query} newer_than:{lookback}d"))
        fetched = 0
        for h in heads:
            m = SUBJECT_RUN_RE.search(h["subject"])
            if m and (cache / f"changes-{m.group('run')}.json").exists():
                continue  # already cached
            found = extract_report(gm.body(h["uid"]))
            if found:
                filename, report = found
                (cache / filename).write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
                fetched += 1
        log(f"robot reports: {len(heads)} in inbox, {fetched} newly downloaded")

        if with_inbox:
            rows = gm.headers(gm.search(f"{inbox_query} newer_than:{lookback}d"))
            for r in rows:
                r.pop("uid", None)
            (cache / "inbox.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
            log(f"moodle notifications: {len(rows)}")
    finally:
        gm.close()


def build_from_gmail(cache: pathlib.Path, *, days: int = 7, name: str | None = None,
                     tz: str = "Europe/Vienna", robot_query: str = DEFAULT_ROBOT_QUERY,
                     inbox_query: str = DEFAULT_INBOX_QUERY, with_inbox: bool = True) -> dict:
    """Fetch from Gmail and build the dashboard data in one go (used by the Vercel function)."""
    fetch_gmail(*gmail_credentials(), cache, days=days, robot_query=robot_query,
                inbox_query=inbox_query, with_inbox=with_inbox, log=lambda *_: None)
    inbox_path = cache / "inbox.json"
    inbox = json.loads(inbox_path.read_text(encoding="utf-8")) if with_inbox and inbox_path.exists() else []
    return build(load_reports_from_dir(cache), inbox, days=days, name=name, tz=tz, source="gmail")


def redact_students(data: dict) -> dict:
    """Remove other students' names and post text. Staff posts and announcements stay."""
    for e in data.get("feed", []):
        if e.get("cat") == "forum" and not e.get("staff"):
            e["author"] = None
            e["excerpt"] = ""
    data["redacted"] = True
    return data


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def to_js(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # Forum posts are user content: never let them close the <script> tag.
    payload = payload.replace("</", "<\\/").replace("<!--", "<\\!--")
    return f"window.MOODLE_DASHBOARD = {payload};\n"


DATA_TAG = '<script src="data.js" data-dashboard-data></script>'


def write_standalone(js: str, target: pathlib.Path) -> None:
    template = (HERE / "index.html").read_text(encoding="utf-8")
    if DATA_TAG not in template:
        raise SystemExit("index.html no longer contains the data.js script tag; cannot inline data.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(template.replace(DATA_TAG, f"<script>{js}</script>"), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--reports", type=pathlib.Path,
                   help="folder with changes-*.json (skips Gmail). Reads inbox.json from it if present.")
    p.add_argument("--inbox-json", type=pathlib.Path, help="Moodle notification list to use instead of Gmail")
    p.add_argument("--cache", type=pathlib.Path, default=HERE / "reports",
                   help="where downloaded attachments are kept (default: dashboard/reports)")
    p.add_argument("--days", type=int, default=7, help="how far back the 'what's new' feed goes (default 7)")
    p.add_argument("--name", default=os.environ.get("MOODLE_DASH_NAME"), help="first name for the greeting")
    p.add_argument("--tz", default="Europe/Vienna", help="time zone for dates (default Europe/Vienna)")
    p.add_argument("--robot-query", default=DEFAULT_ROBOT_QUERY, help="Gmail search for robot reports")
    p.add_argument("--inbox-query", default=DEFAULT_INBOX_QUERY, help="Gmail search for Moodle notifications")
    p.add_argument("--no-inbox", action="store_true", help="skip the Moodle notification panel")
    p.add_argument("--out", type=pathlib.Path, default=HERE / "data.js", help="output data file")
    p.add_argument("--standalone", type=pathlib.Path,
                   help="also write a single self-contained HTML file (handy for sharing or recording)")
    args = p.parse_args(argv)

    if args.reports:
        folder, source = args.reports, "files"
    else:
        sync_from_gmail(args, args.cache)
        folder, source = args.cache, "gmail"

    reports = load_reports_from_dir(folder)
    inbox_path = args.inbox_json or (folder / "inbox.json")
    inbox = [] if args.no_inbox or not inbox_path.exists() else json.loads(inbox_path.read_text(encoding="utf-8"))

    data = build(reports, inbox, days=args.days, name=args.name, tz=args.tz, source=source)
    js = to_js(data)
    args.out.write_text(js, encoding="utf-8")
    f = data["filtered"]
    print(f"wrote {args.out} - {len(data['runs'])} runs, {f['kept']} updates kept of {f['raw']} raw changes, "
          f"{len(data['actions'])} open to-dos, {len(data['inbox'])} inbox mails")
    if args.standalone:
        write_standalone(js, args.standalone)
        print(f"wrote {args.standalone}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
