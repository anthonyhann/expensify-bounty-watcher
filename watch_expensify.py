#!/usr/bin/env python3
"""Watch Expensify/App for newly opened paid issues.

Zero dependencies: Python 3 standard library only.

Usage:
    python3 watch_expensify.py                 # poll every 60s, notify on macOS
    python3 watch_expensify.py --interval 30
    python3 watch_expensify.py --once          # single pass, good for cron
    python3 watch_expensify.py --no-notify

Environment:
    GITHUB_TOKEN   optional, raises the API rate limit from 60 to 5000 req/hour
"""

from __future__ import annotations

import argparse
import gzip
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

API = "https://api.github.com/repos/Expensify/App/issues"
USER_AGENT = "expensify-bounty-watcher/1.2"
STATE_FILE = Path(__file__).resolve().parent / ".state" / "expensify-watch.json"
LOCK_FILE = STATE_FILE.parent / "watch.lock"

AMOUNT_RE = re.compile(r"\[\$(\d+(?:\.\d+)?)\]")
UPWORK_RE = re.compile(r"https://www\.upwork\.com/jobs/~[0-9A-Za-z]+")
PAID_LABELS = {"External", "Help Wanted"}
SKIP_LABELS = {"Internal", "Task", "Reviewing", "Awaiting Payment"}


def now() -> str:
    return datetime.now().strftime("%m-%d %H:%M:%S")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_created_at": None, "seen": [], "stages": {}, "etag": None}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def acquire_lock():
    """Hold an exclusive lock for the process lifetime so two watchers cannot run."""
    if fcntl is None:
        return open(os.devnull, "w")
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_FILE, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def request(url: str, etag: str | None, retries: int = 2) -> tuple[int, dict, bytes]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
        "Accept-Encoding": "gzip",
    }
    if etag:
        headers["If-None-Match"] = etag
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return resp.status, dict(resp.headers), raw
        except urllib.error.HTTPError as exc:
            body = exc.read()
            if exc.code == 304:
                return 304, dict(exc.headers), b""
            return exc.code, dict(exc.headers), body
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            if attempt == retries:
                print(f"[{now()}] network error: {exc}")
                return 0, {}, b""
            time.sleep(2)
    return 0, {}, b""


def fetch_issues(etag: str | None) -> tuple[list[dict] | None, str | None, dict, int]:
    params = {
        "state": "open",
        "sort": "created",
        "direction": "desc",
        "per_page": "50",
    }
    url = API + "?" + urllib.parse.urlencode(params)
    status, headers, body = request(url, etag)

    if status == 0:
        return None, etag, headers, status
    if status == 304:
        return None, etag, headers, status
    if status == 200:
        return json.loads(body), headers.get("ETag"), headers, status
    return None, etag, headers, status


def describe(issue: dict) -> dict:
    labels = {label["name"] for label in issue.get("labels", [])}
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    match = AMOUNT_RE.search(title)
    amount = float(match.group(1)) if match else 0.0
    upwork = UPWORK_RE.search(body)
    return {
        "number": issue["number"],
        "title": title,
        "amount": amount,
        "labels": sorted(labels & PAID_LABELS),
        "help_wanted": "Help Wanted" in labels,
        "assignees": [user["login"] for user in issue.get("assignees", [])],
        "comments": issue.get("comments", 0),
        "created_at": issue.get("created_at"),
        "url": issue.get("html_url"),
        "upwork": upwork.group(0) if upwork else None,
        "claimable": not issue.get("assignees"),
    }


def is_wanted(issue: dict) -> bool:
    if "pull_request" in issue:
        return False
    labels = {label["name"] for label in issue.get("labels", [])}
    if labels & SKIP_LABELS:
        return False
    return bool(labels & PAID_LABELS)


def notify(title: str, message: str) -> None:
    print("\a", end="", flush=True)
    if sys.platform == "darwin":
        escaped_title = title.replace("\\", "\\\\").replace('"', '\\"')
        escaped_message = message.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            f'display notification "{escaped_message}" '
            f'with title "{escaped_title}" sound name "Glass"'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        return
    if shutil.which("notify-send"):
        try:
            subprocess.run(
                ["notify-send", title, message],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            pass


def proposal_summary(number: int) -> dict | None:
    """Count proposals already posted on the issue (one extra request, only on OPEN alerts)."""
    url = f"https://api.github.com/repos/Expensify/App/issues/{number}/comments?per_page=100"
    status, _, body = request(url, None)
    if status != 200:
        return None
    try:
        comments = json.loads(body)
    except json.JSONDecodeError:
        return None
    proposals = [c for c in comments if "## Proposal" in (c.get("body") or "")]
    return {
        "proposals": len(proposals),
        "melvin": any(c.get("user", {}).get("login") == "MelvinBot" for c in proposals),
    }


def print_issue(item: dict, stage: str) -> None:
    amount = f"${item['amount']:.0f}" if item["amount"] else "$?"
    state = "UNASSIGNED" if item["claimable"] else "assigned:" + ",".join(item["assignees"][:3])
    if stage == "open":
        headline = f"OPEN NOW {amount} — Help Wanted added, proposal allowed"
    else:
        headline = f"NEW {amount} — waiting for Help Wanted, do not propose yet"
    print(f"[{now()}] {headline}")
    print(f"  Expensify/App#{item['number']} · {state}")
    print(f"  {item['title']}")
    print(f"  {item['url']}")
    if item["upwork"]:
        print(f"  Upwork: {item['upwork']}")
    print(f"  labels={item['labels']} comments={item['comments']}")
    if stage == "open":
        summary = proposal_summary(item["number"])
        if summary:
            melvin = "yes" if summary["melvin"] else "no"
            print(f"  proposals already posted={summary['proposals']} · MelvinBot={melvin}")


def print_seed(items: list[dict]) -> None:
    paid = [item for item in items if item["amount"] >= 100]
    print(f"[{now()}] seeded with {len(paid)} currently open paid issue(s); no alerts sent")
    for item in paid[:15]:
        state = "UNASSIGNED" if item["claimable"] else "assigned"
        print(f"  ${item['amount']:.0f} · {state} · #{item['number']} · {item['title'][:70]}")


def detect_alerts(described: list[dict], stages: dict) -> list[tuple[dict, str]]:
    """Return (item, stage) alerts and update stages in place."""
    alerts: list[tuple[dict, str]] = []
    for item in sorted(described, key=lambda entry: entry["created_at"]):
        key = str(item["number"])
        previous = stages.get(key)
        if previous is None:
            if item["help_wanted"]:
                alerts.append((item, "open"))
                stages[key] = "open"
            else:
                alerts.append((item, "new"))
                stages[key] = "external"
        elif previous == "external" and item["help_wanted"]:
            alerts.append((item, "open"))
            stages[key] = "open"
    return alerts


def sleep_until_reset(headers: dict, fallback: int) -> int:
    reset = headers.get("X-RateLimit-Reset")
    if reset and reset.isdigit():
        wait = max(5, int(reset) - int(time.time()) + 2)
    else:
        wait = fallback
    print(f"[{now()}] rate limited, sleeping {wait}s")
    return wait


def poll_once(state: dict, args: argparse.Namespace) -> bool:
    # Force a full response when stages are missing, otherwise a 304 would skip seeding.
    cached_etag = None if "stages" not in state else state.get("etag")
    items, etag, headers, status = fetch_issues(cached_etag)

    remaining = headers.get("X-RateLimit-Remaining")
    if remaining is not None and remaining.isdigit() and int(remaining) <= 2:
        print(f"[{now()}] API quota low: {remaining} left")

    if status == 304:
        if args.verbose:
            print(f"[{now()}] no changes (304)")
        return True

    if status != 200:
        print(f"[{now()}] GitHub API error {status}: {headers.get('X-RateLimit-Reset', '')}")
        if status in (403, 429):
            time.sleep(sleep_until_reset(headers, 60))
        return False

    state["etag"] = etag
    wanted = [issue for issue in items if is_wanted(issue)]
    described = [describe(issue) for issue in wanted]
    described = [item for item in described if item["amount"] >= args.min_amount]

    last_created = state.get("last_created_at")
    if last_created is None or "stages" not in state:
        state["last_created_at"] = max(
            (issue["created_at"] for issue in items), default=None
        )
        state["seen"] = sorted({item["number"] for item in described})
        state["stages"] = {
            str(item["number"]): "open" if item["help_wanted"] else "external"
            for item in described
        }
        print_seed(described)
        save_state(state)
        return True

    stages = state["stages"]
    alerts = detect_alerts(described, stages)

    for item, stage in alerts:
        print_issue(item, stage)
        if args.notify:
            amount = f"${item['amount']:.0f}" if item["amount"] else "?"
            notify(
                f"Expensify {'OPEN' if stage == 'open' else 'NEW'} {amount}",
                f"#{item['number']} {item['title'][:110]}",
            )

    if alerts:
        state["seen"] = sorted({item["number"] for item, _ in alerts} | set(state.get("seen", [])))
        state["seen"] = state["seen"][-2000:]
        state["stages"] = dict(list(stages.items())[-300:])
        newest = max((issue["created_at"] for issue in items), default=last_created)
        state["last_created_at"] = max(last_created, newest)
        save_state(state)
        print(f"[{now()}] {len(alerts)} alert(s)")
    elif args.verbose:
        print(f"[{now()}] no new paid issues")

    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch Expensify/App for new paid issues")
    parser.add_argument("--interval", type=int, default=60, help="seconds between polls (default 60)")
    parser.add_argument("--once", action="store_true", help="run one poll and exit")
    parser.add_argument("--no-notify", dest="notify", action="store_false", help="disable desktop notifications")
    parser.add_argument("--min-amount", type=float, default=100, help="ignore issues below this amount (default 100)")
    parser.add_argument("--verbose", action="store_true", help="log every poll")
    parser.set_defaults(notify=True)
    args = parser.parse_args()

    lock = acquire_lock()
    if lock is None:
        print(f"[{now()}] another watcher is already running; exiting")
        return 1

    state = load_state()

    if args.once:
        ok = poll_once(state, args)
        return 0 if ok else 1

    print(f"[{now()}] watching Expensify/App every {args.interval}s (Ctrl+C to stop)")
    print(f"[{now()}] state: {STATE_FILE}")
    if not (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")):
        print(f"[{now()}] no GITHUB_TOKEN set; using unauthenticated rate limit")
    failures = 0
    try:
        while True:
            try:
                ok = poll_once(state, args)
                failures = 0 if ok else failures + 1
            except Exception as exc:
                failures += 1
                print(f"[{now()}] poll failed ({failures}): {exc}")
            wait = max(10, args.interval)
            if failures:
                wait = min(300, 30 * failures)
            time.sleep(wait)
    except KeyboardInterrupt:
        print(f"\n[{now()}] stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
