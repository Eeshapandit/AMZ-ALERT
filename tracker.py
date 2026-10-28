"""Multi-company job tracker."""
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "tracker_config.json").read_text())
OUT = ROOT / "tracker"
JOBS_FILE, STATE_FILE, STATUS_FILE = OUT / "jobs.json", OUT / "state.json", OUT / "status.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
UA = {"User-Agent": "Mozilla/5.0 (personal job tracker)", "Accept": "application/json"}
ROLE_RES = {k: re.compile(v, re.I) for k, v in CFG["roles"].items()}
MAX_YEARS = CFG.get("max_years", 3)

# ---------- location rules ----------
TARGET_LOC = re.compile(
    r"new york|\bnyc\b|brooklyn|manhattan|,\s*ny\b|\bny,|new jersey|,\s*nj\b|jersey city|newark|hoboken|princeton|"
    r"seattle|bellevue|redmond|kirkland|,\s*wa\b|"
    r"california|,\s*ca\b|san francisco|\bsf\b|bay area|los angeles|san jose|palo alto|mountain view|sunnyvale|"
    r"san diego|menlo park|redwood city|santa clara|oakland|irvine|santa monica|cupertino|san mateo|"
    r"foster city|emeryville|berkeley|burlingame|culver city|pasadena|el segundo|san carlos",
    re.I,
)
REMOTE = re.compile(r"remote|virtual|anywhere|distributed", re.I)
US = re.compile(r"\bus\b|\busa\b|u\.s\.|united states|america|\bnorth america\b", re.I)
FOREIGN = re.compile(
    r"canada|toronto|vancouver|montreal|\buk\b|united kingdom|london|ireland|dublin|europe|emea|germany|berlin|"
    r"france|paris|spain|madrid|netherlands|amsterdam|poland|india|bangalore|bengaluru|hyderabad|apac|"
    r"australia|sydney|singapore|japan|tokyo|israel|tel aviv|mexico|brazil|latam|argentina|colombia|"
    r"switzerland|zurich|sweden|portugal|lisbon|korea|china|philippines",
    re.I,
)


def location_ok(loc, workplace):
    """Returns 'Remote' / 'Hybrid' / 'Onsite' style label if it passes, else None."""
    loc = loc or ""
    wp = (workplace or "").lower()
    is_remote = "remote" in wp or bool(REMOTE.search(loc))
    if TARGET_LOC.search(loc):
        if "hybrid" in wp:
            return "Hybrid"
        if "remote" in wp:
            return "Remote"
        if "onsite" in wp or "on-site" in wp:
            return "Onsite"
        return "Remote" if REMOTE.search(loc) else "Onsite/Hybrid"
    if is_remote:
        if FOREIGN.search(loc) and not US.search(loc):
            return None
        return "Remote"
    return None


# ---------- experience parsing ----------
NUM_WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())}
YEARS_RE = re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*\d{1,2}\s*)?\+?\s*(?:or more\s+)?(?:years?|yrs?)\b", re.I)
PREFERRED_CUT = re.compile(
    r"(preferred|bonus|nice[\s-]to[\s-]have|nice to haves|plus if you|extra credit)\s*(qualifications|skills|points|requirements)?\s*[:\n]",
    re.I,
)


def to_text(raw):
    t = html.unescape(raw or "")
    t = re.sub(r"<\s*(br|/p|/li|/div|/h\d|li)\b[^>]*>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"[•●▪◦·]", "\n", t)
    return t


def required_years(raw, lenient=False):
    """Experience the role asks for. Each line's alternatives count as the lowest,
    the role needs the highest line. None when no years are mentioned."""
    text = to_text(raw)
    m = PREFERRED_CUT.search(text)
    if m and m.start() > 200:
        text = text[: m.start()]
    text = re.sub(r"\b(" + "|".join(NUM_WORDS) + r")\b(?=\s*\(?\d*\)?\s*\+?\s*(?:or more\s+)?years?)",
                  lambda x: NUM_WORDS[x.group(1).lower()], text, flags=re.I)
    vals = []
    for line in re.split(r"[\n;]|(?<=[.!?])\s+", text):
        if not lenient and not re.search(r"experience|exp\b|background|working in|industry", line, re.I):
            continue
        if re.search(r"preferred|nice to have|bonus|ideally|a plus", line, re.I):
            continue
        nums = [int(n) for n in YEARS_RE.findall(line) if 0 <= int(n) <= 20]
        if nums:
            vals.append(min(nums))
    return max(vals) if vals else None


RETAIL_CFG = CFG.get("retail", {})
RETAIL_ROLE_RES = {k: re.compile(v, re.I) for k, v in RETAIL_CFG.get("extra_roles", {}).items()}
RETAIL_KEYS = {"amazon:amazon"} | {f"{kind}:{s}" for kind in ("greenhouse", "lever", "ashby")
                                   for s in RETAIL_CFG.get(kind, [])}


def role_of(title, retail=False):
    rules = list(ROLE_RES.items()) + (list(RETAIL_ROLE_RES.items()) if retail else [])
    for name, rx in rules:
        if rx.search(title or ""):
            return name
    return None


# ---------- sources ----------
def get_json(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def iso_ms(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return ""


def src_greenhouse(slug):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    out = []
    for j in data.get("jobs", []):
        out.append({
            "id": f"gh:{slug}:{j['id']}",
            "company": j.get("company_name") or slug,
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "workplace": "",
            "link": j.get("absolute_url", ""),
            "posted": j.get("first_published") or j.get("updated_at") or "",
            "_detail": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{j['id']}",
        })
    return out


def src_lever(slug):
    data = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in data:
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location") or ""]
        desc = (j.get("descriptionPlain") or "") + "\n" + "\n".join(
            (l.get("text") or "") + "\n" + (l.get("content") or "") for l in j.get("lists") or [])
        out.append({
            "id": f"lv:{slug}:{j['id']}",
            "company": slug,
            "title": j.get("text", ""),
            "location": "; ".join(x for x in locs if x),
            "workplace": j.get("workplaceType") or "",
            "link": j.get("hostedUrl", ""),
            "posted": iso_ms(j.get("createdAt")),
            "_desc": desc,
        })
    return out


def src_ashby(slug):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [
            (s.get("location") or "") for s in j.get("secondaryLocations") or []]
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        loc = "; ".join(x for x in locs if x)
        if addr.get("addressCountry") and addr["addressCountry"] not in loc:
            loc += f" ({addr['addressCountry']})"
        wp = j.get("workplaceType") or ("Remote" if j.get("isRemote") else "")
        out.append({
            "id": f"ab:{slug}:{j.get('id')}",
            "company": slug,
            "title": j.get("title", ""),
            "location": loc,
            "workplace": wp,
            "link": j.get("jobUrl", ""),
            "posted": j.get("publishedAt", ""),
            "_desc": j.get("descriptionPlain") or j.get("descriptionHtml") or "",
        })
    return out


def src_amazon(_):
    out, seen = [], set()
    for q in CFG.get("amazon_queries", []):
        params = [("base_query", q), ("sort", "recent"), ("result_limit", "100"),
                  ("normalized_country_code[]", "USA")]
        data = get_json("https://www.amazon.jobs/en/search.json?" + urllib.parse.urlencode(params))
        for j in data.get("jobs", []):
            jid = str(j.get("id_icims") or j.get("id"))
            if jid in seen:
                continue
            seen.add(jid)
            out.append({
                "id": f"amz:{jid}",
                "company": "Amazon",
                "title": j.get("title", ""),
                "location": j.get("normalized_location") or j.get("location") or "",
                "workplace": "",
                "link": "https://www.amazon.jobs" + (j.get("job_path") or ""),
                "posted": j.get("posted_date", ""),
                "_desc": j.get("basic_qualifications") or "",
                "_lenient": True,
            })
    return out


SOURCES = [("amazon", "amazon", src_amazon)] + \
    [("greenhouse", s, src_greenhouse) for s in CFG.get("greenhouse", [])] + \
    [("lever", s, src_lever) for s in CFG.get("lever", [])] + \
    [("ashby", s, src_ashby) for s in CFG.get("ashby", [])]
_have = {(k, s) for k, s, _ in SOURCES}
SOURCES += [(k, s, fn) for k, fn in (("greenhouse", src_greenhouse), ("lever", src_lever), ("ashby", src_ashby))
            for s in RETAIL_CFG.get(k, []) if (k, s) not in _have]


def run_source(src):
    kind, slug, fn = src
    try:
        return (kind, slug, fn(slug), None)
    except Exception as e:
        return (kind, slug, None, f"{type(e).__name__}: {e}"[:160])


def fetch_detail(job):
    try:
        return job["id"], get_json(job["_detail"]).get("content", "")
    except Exception:
        return job["id"], None


# ---------- notify ----------
def ntfy(title, body, link=""):
    print(f"ALERT: {title} | {body.splitlines()[0] if body else ''}")
    if not NTFY_TOPIC:
        return
    headers = {"Title": title.encode("ascii", "ignore").decode()[:250], "Priority": "high", "Tags": "rocket"}
    if link:
        headers["Click"] = link
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode(), headers=headers), timeout=15)
    except Exception as e:
        print(f"ntfy failed: {e}", file=sys.stderr)


def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def main():
    OUT.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state = load(STATE_FILE, {"companies": [], "jobs": {}})   # id -> {"y": years, "keep": bool}
    prev_jobs = {j["id"]: j for j in load(JOBS_FILE, {}).get("jobs", [])}
    known_companies = set(state["companies"])

    with ThreadPoolExecutor(max_workers=24) as ex:
        results = list(ex.map(run_source, SOURCES))

    status = {"ok": {}, "errors": {}}
    candidates, ok_keys = [], set()
    for kind, slug, jobs, err in results:
        key = f"{kind}:{slug}"
        if err:
            status["errors"][key] = err
            continue
        ok_keys.add(key)
        hits = 0
        for j in jobs:
            role = role_of(j["title"], retail=key in RETAIL_KEYS)
            if not role:
                continue
            label = location_ok(j["location"], j["workplace"])
            if not label:
                continue
            j["role"], j["mode"], j["_src"] = role, label, key
            candidates.append(j)
            hits += 1
        status["ok"][key] = {"open": len(jobs), "matching": hits}

    # Greenhouse descriptions only for jobs not evaluated before
    need = [j for j in candidates if "_detail" in j and j["id"] not in state["jobs"]]
    with ThreadPoolExecutor(max_workers=16) as ex:
        details = dict(ex.map(fetch_detail, need))

    kept, new_alerts = {}, []
    for j in candidates:
        st = state["jobs"].get(j["id"])
        if st is None:
            desc = details.get(j["id"]) if "_detail" in j else j.get("_desc")
            if desc is None:          # detail fetch failed, retry next run
                continue
            y = required_years(desc, lenient=j.get("_lenient", False))
            st = {"y": y, "keep": y is None or y <= MAX_YEARS}
            state["jobs"][j["id"]] = st
            if st["keep"] and j["_src"] in known_companies:
                new_alerts.append(j)
        if not st["keep"]:
            continue
        prev = prev_jobs.get(j["id"], {})
        kept[j["id"]] = {
            "id": j["id"], "company": j["company"], "title": j["title"], "role": j["role"],
            "location": j["location"], "mode": j["mode"], "years": st["y"], "link": j["link"],
            "posted": j["posted"], "first_seen": prev.get("first_seen", now),
            "seeded": prev.get("seeded", j["_src"] not in known_companies),
            "sector": "retail" if j["_src"] in RETAIL_KEYS else "",
        }

    # keep last known jobs for sources that errored this run
    for jid, j in prev_jobs.items():
        src = jid.split(":")[0]
        key = {"gh": "greenhouse", "lv": "lever", "ab": "ashby", "amz": "amazon"}.get(src, src)
        slug = "amazon" if src == "amz" else jid.split(":")[1]
        if f"{key}:{slug}" not in ok_keys and jid not in kept:
            kept[jid] = j

    state["companies"] = sorted(known_companies | ok_keys)
    open_ids = set(kept) | {j["id"] for j in candidates}
    state["jobs"] = {k: v for k, v in state["jobs"].items() if k in open_ids}

    if len(new_alerts) > 5:
        ntfy(f"{len(new_alerts)} new data/FDE roles",
             "\n".join(f"{j['company']}: {j['title']} ({j['mode']})" for j in new_alerts[:15]))
    else:
        for j in new_alerts:
            yrs = "no yrs listed" if kept[j["id"]]["years"] is None else f"{kept[j['id']]['years']}+ yrs"
            ntfy(f"{j['company']}: {j['title']}", f"{j['location']} · {j['mode']} · {yrs}\n{j['link']}", j["link"])

    jobs = sorted(kept.values(), key=lambda j: (j["first_seen"], j["posted"] or ""), reverse=True)
    new_doc = {"jobs": jobs}
    if prev_jobs.keys() != kept.keys() or not JOBS_FILE.exists():
        new_doc["updated"] = now
    else:
        new_doc["updated"] = load(JOBS_FILE, {}).get("updated", now)
    JOBS_FILE.write_text(json.dumps(new_doc, indent=0))
    STATE_FILE.write_text(json.dumps(state, separators=(",", ":"), sort_keys=True))
    status["summary"] = {"sources_ok": len(status["ok"]), "sources_failed": len(status["errors"]),
                         "candidates": len(candidates), "kept": len(jobs)}
    STATUS_FILE.write_text(json.dumps(status, indent=1, sort_keys=True))
    print(json.dumps(status["summary"]), f"new alerts: {len(new_alerts)}")


if __name__ == "__main__":
    main()
