#!/usr/bin/env python3
"""Watch public job boards and report roles that were not there last time.

  python3 watch.py              check once and notify on this Mac
  python3 watch.py --status     last check and recent alerts
  python3 watch.py --install    check every 5 minutes (macOS, while you are logged in)
  python3 watch.py --uninstall  stop the background check
  python3 watch.py --github     open a GitHub issue per new role (used by Actions)

Companies live in companies.json. See README.md and companies.example.json.
The first time a company is seen, every open role is remembered and no alert
is sent. Later runs alert only for new ids.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LABEL = "com.vacancy-tracker"
INTERVAL_SECONDS = 300
ISSUE_CAP = 20
USER_AGENT = "vacancy-tracker/1.0"

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
STATE_PATH = STATE_DIR / "seen.json"
LOG_PATH = STATE_DIR / "alerts.log"
STATUS_PATH = STATE_DIR / "status.txt"
COMPANIES_PATH = ROOT / "companies.json"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def now_local() -> datetime:
    return datetime.now().astimezone()


def stamp(when: datetime | None = None) -> str:
    return (when or now_local()).isoformat(timespec="seconds")


def as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("name", "location", "label"):
            text = as_text(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, list):
        parts = []
        for item in value:
            text = as_text(item)
            if text and text not in parts:
                parts.append(text)
        return ", ".join(parts)
    return str(value).strip()


def to_iso(value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        raw = int(value)
        seconds = raw / 1000 if raw > 10_000_000_000 else raw
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    return str(value)


def pretty_published(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(to_iso(value).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return value
    return parsed.strftime("%-d %b %H:%M")


def log_line(message: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp()}  {message}\n")


def write_status(open_roles: int, companies: int, result: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(
        "\n".join(
            [
                f"last_checked: {stamp()}",
                f"open_roles: {open_roles}",
                f"companies: {companies}",
                f"last_result: {result}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def company_key(company: dict) -> str:
    return f"{company['source']}:{company['board']}"


def is_placeholder(company: dict) -> bool:
    board = str(company.get("board") or "").strip()
    return (not board) or board.startswith("REPLACE") or board.startswith("the-")


def load_companies() -> list[dict]:
    if not COMPANIES_PATH.exists():
        return []
    data = json.loads(COMPANIES_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("companies.json must be a list")
    companies = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source") or "").strip().lower()
        board = str(entry.get("board") or "").strip()
        name = str(entry.get("name") or board or source).strip()
        companies.append(
            {
                "name": name,
                "source": source,
                "board": board,
                "keywords": entry.get("keywords") or [],
                "locations": entry.get("locations") or [],
            }
        )
    return companies


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"companies": {}, "legacy_jobs": None}
    data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if isinstance(data.get("companies"), dict):
        return {"companies": data["companies"], "legacy_jobs": None}
    return {"companies": {}, "legacy_jobs": data.get("jobs") or {}}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"companies": state.get("companies") or {}}
    temporary = STATE_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(STATE_PATH)


def adopt_legacy(state: dict, companies: list[dict]) -> None:
    legacy = state.get("legacy_jobs") or {}
    if not legacy or len(companies) != 1:
        return
    key = company_key(companies[0])
    bucket = state["companies"].setdefault(key, {"seeded": True, "jobs": {}})
    jobs = bucket.setdefault("jobs", {})
    for job_id, job in legacy.items():
        jobs.setdefault(str(job_id), job)
    bucket["seeded"] = True
    state["legacy_jobs"] = None


def http_json(url: str):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"HTTP {error.code} for {url}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"could not reach {url}: {error.reason}") from error


def greenhouse_category(raw: dict) -> str:
    found = []
    for item in raw.get("metadata") or []:
        name = str(item.get("name") or "").lower()
        if any(word in name for word in ("department", "team", "category")):
            text = as_text(item.get("value"))
            if text and text not in found:
                found.append(text)
    return ", ".join(found)


def job_is_remote(raw: dict) -> bool:
    if raw.get("isRemote") is True:
        return True
    return str(raw.get("workplaceType") or "").strip().lower() == "remote"


def fetch_greenhouse(board: str) -> list[dict]:
    token = urllib.parse.quote(board, safe="")
    payload = http_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    raw_jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(raw_jobs, list):
        raise RuntimeError("Greenhouse response had no jobs list")
    jobs = []
    for raw in raw_jobs:
        job_id = str(raw.get("id") or "")
        if not job_id:
            continue
        jobs.append(
            {
                "id": job_id,
                "title": as_text(raw.get("title")) or "(untitled)",
                "location": as_text((raw.get("location") or {}).get("name")),
                "remote": job_is_remote(raw),
                "category": greenhouse_category(raw),
                "url": as_text(raw.get("absolute_url"))
                or f"https://job-boards.greenhouse.io/{token}/jobs/{job_id}",
                "first_published": to_iso(raw.get("first_published")),
            }
        )
    return jobs


def fetch_lever(board: str) -> list[dict]:
    token = urllib.parse.quote(board, safe="")
    payload = http_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    if not isinstance(payload, list):
        raise RuntimeError("Lever response was not a list")
    jobs = []
    for raw in payload:
        job_id = str(raw.get("id") or "")
        if not job_id:
            continue
        categories = raw.get("categories") or {}
        jobs.append(
            {
                "id": job_id,
                "title": as_text(raw.get("text")) or "(untitled)",
                "location": as_text(categories.get("location")),
                "remote": job_is_remote(raw),
                "category": as_text(categories.get("team")),
                "url": as_text(raw.get("hostedUrl")) or as_text(raw.get("applyUrl")),
                "first_published": to_iso(raw.get("createdAt")),
            }
        )
    return jobs


def fetch_ashby(board: str) -> list[dict]:
    token = urllib.parse.quote(board, safe="")
    payload = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    raw_jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(raw_jobs, list):
        raise RuntimeError("Ashby response had no jobs list")
    jobs = []
    for raw in raw_jobs:
        if raw.get("isListed") is False:
            continue
        job_id = str(raw.get("id") or "")
        if not job_id:
            continue
        locations = [as_text(raw.get("location"))]
        for extra in raw.get("secondaryLocations") or []:
            locations.append(as_text(extra.get("location") if isinstance(extra, dict) else extra))
        jobs.append(
            {
                "id": job_id,
                "title": as_text(raw.get("title")) or "(untitled)",
                "location": as_text(locations),
                "remote": job_is_remote(raw),
                "category": as_text(raw.get("department")) or as_text(raw.get("team")),
                "url": as_text(raw.get("applyUrl")) or as_text(raw.get("jobUrl")),
                "first_published": to_iso(raw.get("publishedAt")),
            }
        )
    return jobs


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


def fetch_company(company: dict) -> list[dict]:
    fetcher = FETCHERS.get(company["source"])
    if fetcher is None:
        known = ", ".join(sorted(FETCHERS))
        raise RuntimeError(f"unknown source {company['source']!r} (use {known})")
    return fetcher(company["board"])


def terms(values) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value).strip().lower() for value in values if str(value).strip()]


def matches(job: dict, company: dict) -> bool:
    keywords = terms(company.get("keywords"))
    locations = terms(company.get("locations"))
    if keywords:
        haystack = " ".join([job["title"], job.get("category") or "", job.get("location") or ""]).lower()
        if not any(word in haystack for word in keywords):
            return False
    if locations:
        place = job.get("location") or ""
        if job.get("remote") and "remote" not in place.lower():
            place = f"{place} remote"
        if not any(word in place.lower() for word in locations):
            return False
    return True


def detail(job: dict) -> str:
    published = pretty_published(job.get("first_published") or "")
    bits = [bit for bit in (job.get("category") or "", job.get("location") or "", published) if bit]
    place = " · ".join(bits)
    url = job.get("url") or ""
    if place and url:
        return f"{place}\n{url}"
    return place or url


def describe(job: dict) -> str:
    place = " | ".join(bit for bit in (job.get("category") or "", job.get("location") or "") if bit)
    published = pretty_published(job.get("first_published") or "")
    when = f" posted {published}" if published else ""
    tail = f" | {place}" if place else ""
    url = f" | {job['url']}" if job.get("url") else ""
    return f"{job['title']}{tail}{when}{url}"


NOTIFIER = ROOT / "macos" / "notifier.app" / "Contents" / "MacOS" / "notifier"


def notify(title: str, body: str, url: str = "") -> None:
    if NOTIFIER.exists():
        completed = subprocess.run(
            [str(NOTIFIER), "notify", title, body, url],
            check=False,
            timeout=60,
        )
        if completed.returncode == 0:
            return
        if completed.returncode == 2:
            print("Allow notifications for job-alerts in System Settings.", file=sys.stderr)
    if not Path("/usr/bin/osascript").exists():
        print(f"{title}\n{body}\n")
        return
    subprocess.run(
        [
            "/usr/bin/osascript",
            "-e",
            "on run argv",
            "-e",
            'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"',
            "-e",
            "end run",
            title,
            body,
        ],
        check=False,
        timeout=30,
    )


def announce(company: dict, jobs: list[dict]) -> None:
    if len(jobs) > 6:
        preview = "\n".join(job["title"] for job in jobs[:6])
        notify(
            f"{len(jobs)} new {company['name']} roles",
            f"{preview}\n…and {len(jobs) - 6} more in state/alerts.log",
        )
        return
    for job in jobs:
        notify(f"{company['name']}: {job['title']}", detail(job), job.get("url") or "")


def github_api(method: str, path: str, payload: dict | None = None):
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        return 0, "no token"
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def ensure_label(repo: str) -> None:
    status, _body = github_api(
        "POST",
        f"/repos/{repo}/labels",
        {
            "name": "vacancy",
            "color": "5319E7",
            "description": "A newly posted public role",
        },
    )
    if status not in (201, 422):
        print(f"could not create the vacancy label ({status})")


def issue_exists(repo: str, marker: str) -> bool:
    query = urllib.parse.quote(f'repo:{repo} "{marker}"')
    status, body = github_api("GET", f"/search/issues?q={query}")
    if status != 200 or not isinstance(body, dict):
        return False
    return bool(body.get("total_count"))


def publish_issue(repo: str, company: dict, job: dict) -> bool:
    marker = f"vacancy-id:{company_key(company)}:{job['id']}"
    if issue_exists(repo, marker):
        return True
    title = f"{company['name']}: {job['title']}"[:240]
    published = pretty_published(job.get("first_published") or "") or "unknown"
    body = "\n".join(
        [
            "A new role is open.",
            "",
            f"- **Company:** {company['name']}",
            f"- **Team:** {job.get('category') or '—'}",
            f"- **Location:** {job.get('location') or '—'}",
            f"- **Posted:** {published}",
            f"- **Apply:** {job.get('url') or '—'}",
            "",
            "- [ ] Applied",
            "",
            f"Tracker id: `{marker}`",
        ]
    )
    status, response = github_api(
        "POST",
        f"/repos/{repo}/issues",
        {"title": title, "body": body, "labels": ["vacancy"]},
    )
    if status == 201 and isinstance(response, dict):
        print(response.get("html_url") or title)
        return True
    print(f"issue create failed ({status}): {response}", file=sys.stderr)
    return False


def check(mode: str) -> int:
    configured = load_companies()
    companies = [company for company in configured if not is_placeholder(company)]
    if not companies:
        print("No companies configured. Copy companies.example.json to companies.json and edit it.")
        return 0

    state = load_state()
    adopt_legacy(state, companies)
    notes = []
    open_roles = 0
    repo = os.environ.get("GITHUB_REPOSITORY") or ""
    label_ready = False

    for company in companies:
        key = company_key(company)
        previous = state["companies"].get(key) or {"seeded": False, "jobs": {}}
        try:
            current = fetch_company(company)
        except Exception as error:
            log_line(f"ERROR  {company['name']}  {error}")
            print(f"{company['name']}: {error}", file=sys.stderr)
            notes.append(f"{company['name']}: check failed")
            continue

        known = previous.get("jobs") or {}
        if previous.get("seeded") and len(known) >= 5 and len(current) == 0:
            log_line(f"ERROR  {company['name']}  board came back empty; kept the previous list")
            notes.append(f"{company['name']}: empty board ignored")
            continue

        open_roles += len(current)
        if not previous.get("seeded"):
            state["companies"][key] = {"seeded": True, "jobs": {job["id"]: job for job in current}}
            log_line(f"SEED  {company['name']}  watching {len(current)} open roles")
            notes.append(f"seeded {company['name']} ({len(current)})")
            if mode == "local":
                notify(
                    f"Watching {company['name']}",
                    f"{len(current)} open roles. I'll ping you when a new one is posted.",
                )
            continue

        found = [job for job in current if job["id"] not in known]
        found.sort(key=lambda job: job.get("first_published") or "")
        interesting = [job for job in found if matches(job, company)]
        saved = {job["id"]: job for job in current}
        undelivered = []

        if mode == "github":
            if interesting and not label_ready:
                if repo:
                    ensure_label(repo)
                label_ready = True
            opened = 0
            for job in interesting:
                if opened >= ISSUE_CAP:
                    undelivered.append(job)
                    continue
                if repo and publish_issue(repo, company, job):
                    opened += 1
                    log_line(f"ISSUE  {company['name']}  {describe(job)}")
                else:
                    undelivered.append(job)
                    log_line(f"ERROR  could not open an issue for {describe(job)}")
            for job in found:
                if job not in interesting:
                    log_line(f"SKIP  {company['name']}  {describe(job)}")
        else:
            for job in found:
                flag = "ALERT" if job in interesting else "SKIP"
                log_line(f"{flag}  {company['name']}  {describe(job)}")
            if interesting and mode == "local":
                announce(company, interesting)

        for job in undelivered:
            saved.pop(job["id"], None)
        state["companies"][key] = {"seeded": True, "jobs": saved}
        hidden = len(found) - len(interesting)
        if interesting:
            note = f"{len(interesting)} new at {company['name']}"
            if hidden:
                note += f", {hidden} hidden by filters"
            notes.append(note)
            if mode != "github":
                for job in interesting:
                    print(describe(job))
        elif found:
            notes.append(f"{len(found)} new at {company['name']}, hidden by filters")

    save_state(state)
    result = "; ".join(notes) if notes else "no new roles"
    write_status(open_roles, len(companies), result)
    print(f"{result} ({open_roles} open)")
    return 0


def show_status() -> int:
    if STATUS_PATH.exists():
        print(STATUS_PATH.read_text(encoding="utf-8").rstrip())
    else:
        print("no checks yet")
    if LOG_PATH.exists():
        lines = [line for line in LOG_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
        recent = lines[-8:]
        if recent:
            print("\nrecent:")
            print("\n".join(recent))
    print(f"\nbackground: {'installed' if PLIST_PATH.exists() else 'not installed'}")
    return 0


def python_bin() -> str:
    system = Path("/usr/bin/python3")
    if system.exists():
        return str(system)
    return sys.executable


def plist_text() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_bin()}</string>
        <string>{ROOT / "watch.py"}</string>
    </array>
    <key>StartInterval</key>
    <integer>{INTERVAL_SECONDS}</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{STATE_DIR / "launchd.log"}</string>
    <key>StandardErrorPath</key>
    <string>{STATE_DIR / "launchd.err"}</string>
</dict>
</plist>
"""


def launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["/bin/launchctl", *args], check=False, text=True, capture_output=True)


def install() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(plist_text(), encoding="utf-8")
    domain = f"gui/{os.getuid()}"
    launchctl("bootout", domain, str(PLIST_PATH))
    loaded = launchctl("bootstrap", domain, str(PLIST_PATH))
    if loaded.returncode != 0:
        print(loaded.stderr.strip() or loaded.stdout.strip() or "launchctl bootstrap failed", file=sys.stderr)
        return 1
    launchctl("enable", f"{domain}/{LABEL}")
    print(f"checking every {INTERVAL_SECONDS // 60} minutes ({PLIST_PATH})")
    return 0


def uninstall() -> int:
    domain = f"gui/{os.getuid()}"
    if PLIST_PATH.exists():
        launchctl("bootout", domain, str(PLIST_PATH))
        PLIST_PATH.unlink()
    print("background check stopped")
    return 0


def self_check() -> int:
    previous = {"1": {"id": "1"}}
    current = [
        {"id": "1", "title": "Old", "first_published": "2026-01-01T00:00:00+00:00"},
        {"id": "2", "title": "New", "first_published": "2026-10-06T09:00:00+00:00"},
    ]
    found = [job for job in current if job["id"] not in previous]
    assert [job["id"] for job in found] == ["2"]
    company = {"keywords": ["writer"], "locations": ["amsterdam"]}
    assert matches(
        {"title": "Product Marketing Manager", "category": "", "location": "United States", "remote": True},
        {"keywords": ["product marketing"], "locations": ["remote", "canada"]},
    )
    assert not matches(
        {"title": "Product Marketing Manager", "category": "", "location": "United States", "remote": False},
        {"keywords": ["product marketing"], "locations": ["remote", "canada"]},
    )
    assert matches({"title": "Writer", "category": "Academy", "location": "Amsterdam"}, company)
    assert not matches({"title": "Writer", "category": "Academy", "location": "Amsterdam"}, {"keywords": ["finance"], "locations": []})
    assert is_placeholder({"board": "REPLACE_ME"})
    assert not is_placeholder({"board": "acme"})
    state = {"companies": {}, "legacy_jobs": {"9": {"id": "9", "title": "Kept"}}}
    adopt_legacy(state, [{"name": "Acme", "source": "greenhouse", "board": "acme"}])
    assert state["companies"]["greenhouse:acme"]["jobs"]["9"]["title"] == "Kept"
    assert pretty_published("2026-10-05T17:37:27-04:00")
    assert pretty_published("1782214185805")
    print("self-check ok")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Notify when a watched company posts a new public role.")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="check without a Mac notification or a GitHub issue")
    parser.add_argument("--github", action="store_true", help="open a GitHub issue for each new role")
    parser.add_argument("--self-check", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.self_check:
            return self_check()
        if args.status:
            return show_status()
        if args.uninstall:
            return uninstall()
        if args.install:
            return install()
        if args.quiet:
            mode = "quiet"
        elif args.github:
            mode = "github"
        else:
            mode = "local"
        return check(mode)
    except Exception as error:
        log_line(f"ERROR  {error}")
        print(f"check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
