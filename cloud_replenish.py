# -*- coding: utf-8 -*-
"""Cloud replenisher: when the schedule runway runs low, turn pooled leads into
scheduled emails and publish the new SCHEDULE_B64_* secrets automatically.

Runs in GitHub Actions (daily). Needs: SCHEDULE_B64_* env, DATA_KEY, PAT_TOKEN.
"""
import os, json, gzip, base64, datetime, urllib.request, urllib.error, time
from zoneinfo import ZoneInfo
from collections import Counter

import replenish as R      # reuse classify / build_body / build_subject / slot_time

BJT = ZoneInfo("Asia/Shanghai")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
PAT = os.environ.get("PAT_TOKEN", "")
GH = os.environ.get("GITHUB_TOKEN", "")
DATA_KEY = os.environ.get("DATA_KEY", "")
DAILY_CAP = int(os.environ.get("DAILY_CAP", "20"))
MIN_RUNWAY = int(os.environ.get("MIN_RUNWAY_DAYS", "5"))
CHUNK = 28000


def api(url, data=None, method="GET", token=None, tries=4):
    tok = token or GH
    last = None
    for i in range(tries):
        try:
            hdr = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
            if data is not None:
                hdr["Content-Type"] = "application/json"
            return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=hdr,
                                                                method=method), timeout=40).read()
        except Exception as e:
            last = e
            time.sleep(2 + i)
    raise last


def xor(b):
    k = DATA_KEY.encode() or b"x"
    return bytes(x ^ k[i % len(k)] for i, x in enumerate(b))


def load_blob(path, default):
    try:
        d = json.loads(api(f"https://api.github.com/repos/{REPO}/contents/{path}"))
        return json.loads(xor(base64.b64decode(d["content"])).decode("utf-8"))
    except Exception as e:
        print(f"load {path}: {type(e).__name__}")
        return default


def save_blob(path, obj, msg):
    body_raw = base64.b64encode(xor(json.dumps(obj, ensure_ascii=False).encode()))
    content = base64.b64encode(body_raw).decode()
    url = f"https://api.github.com/repos/{REPO}/contents/{path}"
    for attempt in range(5):
        try:
            sha = None
            try:
                sha = json.loads(api(url))["sha"]
            except Exception:
                pass
            payload = {"message": msg, "content": content}
            if sha:
                payload["sha"] = sha
            api(url, data=json.dumps(payload).encode(), method="PUT")
            return True
        except urllib.error.HTTPError as he:
            if he.code in (409, 422) and attempt < 4:
                time.sleep(2)
                continue
            print("save err", he.code)
            return False
    return False


def read_schedule():
    parts = []
    for i in range(1, 12):
        p = os.environ.get(f"SCHEDULE_B64_{i}")
        if not p:
            break
        parts.append(p.strip())
    if not parts:
        return []
    return json.loads(gzip.decompress(base64.b64decode("".join(parts)))).get("entries", [])


def write_secrets(entries):
    from nacl import encoding, public
    raw = json.dumps({"entries": entries}, ensure_ascii=False).encode()
    b64 = base64.b64encode(gzip.compress(raw, 9)).decode()
    chunks = [b64[i:i + CHUNK] for i in range(0, len(b64), CHUNK)]
    if len(chunks) > 10:
        print("WARN too many chunks", len(chunks))
        return False
    pk = json.loads(api(f"https://api.github.com/repos/{REPO}/actions/secrets/public-key", token=PAT))
    pub = public.PublicKey(pk["key"].encode(), encoding.Base64Encoder())
    for i, c in enumerate(chunks, 1):
        sealed = public.SealedBox(pub).encrypt(c.encode())
        api(f"https://api.github.com/repos/{REPO}/actions/secrets/SCHEDULE_B64_{i}",
            data=json.dumps({"encrypted_value": base64.b64encode(sealed).decode(),
                             "key_id": pk["key_id"]}).encode(), method="PUT", token=PAT)
        print(f"  SCHEDULE_B64_{i} <- {len(c)} chars")
    # delete stale higher chunks
    try:
        sec = json.loads(api(f"https://api.github.com/repos/{REPO}/actions/secrets", token=PAT))
        have = {s["name"] for s in sec["secrets"]}
        for extra in range(len(chunks) + 1, 11):
            nm = f"SCHEDULE_B64_{extra}"
            if nm in have:
                api(f"https://api.github.com/repos/{REPO}/actions/secrets/{nm}", method="DELETE", token=PAT)
                print(f"  removed stale {nm}")
    except Exception:
        pass
    return True


def main():
    entries = read_schedule()
    if not entries:
        print("no schedule readable")
        return 1
    print(f"current schedule: {len(entries)} entries")

    # runway = distinct future days still holding unsent entries
    now = datetime.datetime.now(BJT)
    future = sorted({e["bjt_send"][:10] for e in entries
                     if datetime.datetime.fromisoformat(e["bjt_send"]) >= now})
    print(f"runway: {len(future)} days, last {future[-1] if future else '-'}")
    if len(future) >= MIN_RUNWAY:
        print(f"runway OK (>= {MIN_RUNWAY} days), nothing to do")
        return 0

    pool = load_blob("pool.b64", [])
    print(f"pool: {len(pool)} leads")
    if not pool:
        print("POOL EMPTY - miner needs to produce more leads")
        return 0

    taken_emails = {e["email"].lower() for e in entries}
    taken_doms = {e["email"].split("@")[-1].lower() for e in entries}
    fresh, seen = [], set()
    for c in pool:
        em = c["email"].lower()
        dom = em.split("@")[-1]
        if em in taken_emails or dom in taken_doms or dom in seen:
            continue
        seen.add(dom)
        fresh.append(c)
    print(f"usable pool leads: {len(fresh)}")
    if not fresh:
        print("all pool leads already scheduled/contacted")
        return 0

    last_day = max(datetime.date.fromisoformat(e["local_send"][:10]) for e in entries)
    start = last_day + datetime.timedelta(days=1)
    while start.weekday() > 4:
        start += datetime.timedelta(days=1)
    days = R.next_workdays(start, 120)

    per_bjt = Counter(e["bjt_send"][:10] for e in entries)
    new_entries, di, queue = [], 0, list(fresh)
    while queue and di < len(days):
        day = days[di]
        placed, i = 0, 0
        while i < len(queue):
            c = queue[i]
            if c["country"] in R.GULF and day.weekday() == 4:
                i += 1
                continue
            try:
                tz = ZoneInfo(c["tz"])
            except Exception:
                queue.pop(i)
                continue
            seg = R.classify(c)
            local_dt = datetime.datetime.combine(day, R.slot_time(placed), tzinfo=tz)
            bjt_dt = local_dt.astimezone(BJT)
            if bjt_dt.hour == 0 or per_bjt[bjt_dt.date()] >= DAILY_CAP:
                i += 1
                continue
            new_entries.append({"email": c["email"], "company": c["company"],
                                "country": c["country"], "pain": seg, "tz": c["tz"],
                                "bjt_send": bjt_dt.isoformat(), "local_send": local_dt.isoformat(),
                                "subject": R.build_subject(c, seg),
                                "body": R.build_body(c, seg)})
            per_bjt[bjt_dt.date()] += 1
            queue.pop(i)
            placed += 1
            if placed >= DAILY_CAP:
                break
        di += 1

    if not new_entries:
        print("nothing new could be placed")
        return 0

    merged = entries + new_entries
    merged.sort(key=lambda e: (e["bjt_send"], e["email"]))
    for i, e in enumerate(merged, 1):
        e["id"] = f"sv{i:04d}"

    per_day = Counter(e["bjt_send"][:10] for e in merged)
    em = [e["email"].lower() for e in merged]
    dm = [x.split("@")[-1] for x in em]
    print(f"\nmerged: {len(merged)} entries on {len(per_day)} days (max {max(per_day.values())}/day)")
    print(f"unique emails {len(set(em))}/{len(em)} | domains {len(set(dm))}/{len(dm)}")
    print(f"last day: {merged[-1]['bjt_send'][:10]}")

    if not write_secrets(merged):
        return 1
    used = {e["email"].lower() for e in new_entries}
    remaining = [c for c in pool if c["email"].lower() not in used]
    save_blob("pool.b64", remaining, f"replenish: scheduled {len(new_entries)}, pool left {len(remaining)}")
    print(f"pool after: {len(remaining)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
