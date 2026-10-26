"""Job watcher."""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text())
SEEN_FILE = ROOT / "seen.json"
JOBS_FILE = ROOT / "jobs.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
UA = "Mozilla/5.0 (job-alert personal script)"
MAX_JOBS = 1000


def fetch(query):
    params = [
        ("base_query", query),
        ("sort", "recent"),
        ("result_limit", "100"),
        ("offset", "0"),
    ]
    for cc in CONFIG.get("countries", ["USA"]):
        params.append(("normalized_country_code[]", cc))
    url = "https://www.amazon.jobs/en/search.json?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("jobs", [])


def matches(job):
    title = (job.get("title") or "").lower()
    loc = " ".join(
        str(job.get(k) or "") for k in ("location", "normalized_location", "city", "state")
    ).lower()
    if any(x.lower() in title for x in CONFIG.get("exclude_title", [])):
        return False
    locs = CONFIG.get("locations", [])
    return not locs or any(l.lower() in loc for l in locs)


def job_id(job):
    return str(job.get("id_icims") or job.get("id") or job.get("job_path"))


def link_of(job):
    return "https://www.amazon.jobs" + (job.get("job_path") or "")


def record(job, query, now, seeded):
    return {
        "id": job_id(job),
        "title": job.get("title", ""),
        "location": job.get("normalized_location") or job.get("location") or "",
        "team": job.get("business_category") or job.get("job_category") or job.get("team") or "",
        "posted_date": job.get("posted_date", ""),
        "link": link_of(job),
        "query": query,
        "first_seen": now,
        "seeded": seeded,
    }


def notify(job):
    link = link_of(job)
    title = job.get("title", "New Amazon job")
    loc = job.get("normalized_location") or job.get("location") or ""
    body = f"{loc}\nPosted {job.get('posted_date', '')}\n{link}"
    print(f"NEW: {title} | {loc} | {link}")
    if not NTFY_TOPIC:
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode(),
        headers={
            "Title": ("Amazon: " + title)[:250].encode("ascii", "ignore").decode(),
            "Click": link,
            "Priority": "high",
            "Tags": "briefcase",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"ntfy failed: {e}", file=sys.stderr)


def main():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    first_run = not SEEN_FILE.exists()
    seen = set() if first_run else set(json.loads(SEEN_FILE.read_text()))
    stored = {}
    if JOBS_FILE.exists():
        stored = {j["id"]: j for j in json.loads(JOBS_FILE.read_text()).get("jobs", [])}

    found = {}  # id -> (job, query)
    for q in CONFIG["queries"]:
        try:
            for job in fetch(q):
                if matches(job):
                    found.setdefault(job_id(job), (job, q))
        except Exception as e:  # one bad query should not kill the run
            print(f"query '{q}' failed: {e}", file=sys.stderr)

    if not found:
        print("WARNING: 0 matching jobs. Check filters or whether the amazon.jobs API changed.", file=sys.stderr)

    new_ids = [i for i in found if i not in seen]
    if first_run:
        print(f"First run: seeded {len(found)} existing jobs, no alerts sent.")
    else:
        for i in new_ids:
            notify(found[i][0])
        print(f"{len(new_ids)} new jobs.")

    for i, (job, q) in found.items():
        if i not in stored:
            stored[i] = record(job, q, now, seeded=first_run)

    if first_run or new_ids or not JOBS_FILE.exists():
        SEEN_FILE.write_text(json.dumps(sorted(seen | set(found)), indent=0))
        jobs = sorted(stored.values(), key=lambda j: j["first_seen"], reverse=True)[:MAX_JOBS]
        JOBS_FILE.write_text(json.dumps({"updated": now, "jobs": jobs}, indent=1))


if __name__ == "__main__":
    main()
