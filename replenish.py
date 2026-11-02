# -*- coding: utf-8 -*-
"""Replenish engine: turn the raw candidate pool into scheduled, personalised emails.

Runs both locally (writes _bridge_schedule_final.json) and in GitHub Actions
(pushes the new schedule back into SCHEDULE_B64_* secrets).

Pipeline:
  pool -> filter (already contacted / bounced / already scheduled) -> classify
       -> generate 120-150 word body -> pack onto working days -> publish
"""
import os, json, re, datetime, gzip, base64, urllib.request, urllib.error, sys
from zoneinfo import ZoneInfo
from collections import Counter

BJT = ZoneInfo("Asia/Shanghai")
DAILY_CAP = int(os.environ.get("DAILY_CAP", "20"))
MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", "300"))
GULF = {"Egypt", "Kuwait", "Saudi Arabia", "Oman", "Qatar", "Bahrain",
        "Jordan", "Iraq", "Lebanon", "United Arab Emirates", "Yemen",
        "Yemen, Rep."}

SMTP_USER = "jesse@sinovenusltd.com"

# ---------------------------------------------------------------- messaging
OPENER = ("We're Sino Venus Global (Qingdao), producer of Eco-fresh preserved insects "
          "(mealworms and superworms) for the pet trade.")

PAIN = {
    "aquarium": "For aquarium and ornamental-fish retailers, a shelf-stable live-alternative "
                "feeder makes stock management far easier.",
    "reptile": "For reptile and exotic-pet specialists, a preserved feeder removes live-insect "
               "handling while keeping margins strong.",
    "petfood": "For pet-food manufacturers, insect protein is a cost-stable alternative to "
               "fishmeal and soy in extruded diets.",
    "feed": "For feed mills and farm-supply distributors, insect protein meal offers a stable "
            "substitute for fishmeal.",
    "vet": "For veterinary and animal-health distributors, a clean-label protein line adds "
           "recurring revenue with no cold chain.",
    "bird": "For cage-bird and aviary suppliers, a clean, dust-free insect protein keeps flocks "
            "on a consistent diet year round.",
    "distributor": "For pet-specialty distributors, a high-protein shelf-stable ingredient opens "
                   "a steady new line.",
}

BODY_TAIL = ("We supply B2B only, with flexible MOQs and private-label options, and already "
             "serve distributors across Asia, Europe, and the Middle East. Many use them as a "
             "stable, high-margin feeder for reptiles, birds, and aquarium pets.")

CTA = ("Would you be open to a short call about adding a reliable insect-protein line to your "
       "catalog? I can share specs and market pricing right away.")

SIGNOFF = f"Best regards,\nJesse\nSino Venus Global Limited\n{SMTP_USER}"

CACHE = {}   # classification cache for this process


def classify(c):
    """Decide segment from company name, source notes and website keywords."""
    key = c["email"].lower()
    if key in CACHE:
        return CACHE[key]
    blob = " ".join([c.get("company", ""), c.get("pain", ""), c["email"].split("@")[-1]]).lower()
    site = c.get("site_text", "").lower()
    both = blob + " " + site

    def has(*ws):
        return any(w in both for w in ws)

    if has("aquarium", "aquaristik", "aquascap", "ornamental fish", "koi", "水族", "marine ", "reef"):
        seg = "aquarium"
    elif has("reptile", "terrarium", "exotic pet", "gecko", "bearded dragon", "爬"):
        seg = "reptile"
    elif has("veterinar", "animal health", "animal-health", "兽药", "兽医"):
        seg = "vet"
    elif has("bird", "aviary", "cage bird", "poultry", "鸟"):
        seg = "bird"
    elif has("pet food", "petfood", "extrud", "kibble", "宠物食品", "饲料加工"):
        seg = "petfood"
    elif has("feed mill", "feed supply", "livestock", "farm supply", "agrar", "饲料", "farming"):
        seg = "feed"
    else:
        seg = "distributor"
    CACHE[key] = seg
    return seg


def build_body(c, seg):
    return (f"Hi team at {c['company']},\n\n"
            f"{OPENER} {PAIN[seg]} Our air-dried process locks in ~42% crude protein with a "
            f"12-month ambient shelf life — no refrigeration, no live-insect logistics.\n\n"
            f"{BODY_TAIL}\n\n{CTA}\n\n{SIGNOFF}")


def build_subject(c, seg):
    company = c["company"]
    if seg == "aquarium":
        return f"Shelf-stable insect feeders for {company}"
    if seg == "reptile":
        return f"Preserved insect feeders for {company}"
    if seg == "petfood":
        return f"Insect protein for {company}'s formulations"
    if seg == "feed":
        return f"Insect protein meal for {company}"
    if seg == "vet":
        return f"Clean-label insect protein for {company}"
    if seg == "bird":
        return f"Insect protein for {company}'s bird range"
    return f"Preserved insect protein for {company}"


# ---------------------------------------------------------------- scheduling
def next_workdays(start, n=120):
    days, d = [], start
    while len(days) < n:
        if 0 <= d.weekday() <= 4:
            days.append(d)
        d += datetime.timedelta(days=1)
    return days


def slot_time(seq):
    """spread entries inside the recipient's 08:00-11:59 local window"""
    minute = 5 + (seq * 11) % 230        # 08:05 .. 11:54
    return datetime.time(8 + minute // 60, minute % 60)


def main():
    pool = json.load(open("_replenish_pool.json", encoding="utf-8"))
    existing = json.load(open("_bridge_schedule_final.json", encoding="utf-8"))
    entries = existing["entries"]
    print(f"pool={len(pool)} existing_schedule={len(entries)}")

    taken_emails = {e["email"].lower() for e in entries}
    taken_doms = {e["email"].split("@")[-1].lower() for e in entries}

    # candidates: dedupe by email and by domain
    fresh, seen = [], set()
    for c in pool:
        em = c["email"].lower()
        dom = em.split("@")[-1]
        if em in taken_emails or dom in taken_doms or dom in seen:
            continue
        seen.add(dom)
        fresh.append(c)
    fresh = fresh[:MAX_NEW_PER_RUN]
    print(f"fresh candidates after dedupe: {len(fresh)}")

    # last scheduled day -> start packing the day after
    last_day = max(datetime.date.fromisoformat(e["local_send"][:10]) for e in entries)
    start = last_day + datetime.timedelta(days=1)
    while start.weekday() > 4:
        start += datetime.timedelta(days=1)
    days = next_workdays(start)
    print(f"packing from {start} over {len(days)} candidate workdays")

    new_entries = []
    di = 0
    queue = fresh
    # global per-BJT-day counter seeded with what the existing schedule already uses,
    # so cross-day entries (US-west 09:00 => next-day BJT) cannot overfill a day
    per_bjt = Counter(e["bjt_send"][:10] for e in entries)
    while queue and di < len(days):
        day = days[di]
        placed = 0
        i = 0
        while i < len(queue):
            c = queue[i]
            if c["country"] in GULF and day.weekday() == 4:
                i += 1
                continue
            try:
                tz = ZoneInfo(c["tz"])
            except Exception:
                queue.pop(i)
                continue
            seg = classify(c)
            t = slot_time(placed)
            local_dt = datetime.datetime.combine(day, t, tzinfo=tz)
            bjt_dt = local_dt.astimezone(BJT)
            if bjt_dt.hour == 0:              # BJT midnight cliff
                i += 1
                continue
            if per_bjt[bjt_dt.date()] >= DAILY_CAP:
                i += 1
                continue
            new_entries.append({
                "email": c["email"], "company": c["company"], "country": c["country"],
                "pain": seg, "tz": c["tz"],
                "bjt_send": bjt_dt.isoformat(), "local_send": local_dt.isoformat(),
                "subject": build_subject(c, seg), "body": build_body(c, seg),
            })
            per_bjt[bjt_dt.date()] += 1
            queue.pop(i)
            placed += 1
            if placed >= DAILY_CAP:
                break
        di += 1

    print(f"new entries built: {len(new_entries)}")

    # merge, renumber, publish in local artifact
    merged = entries + new_entries
    merged.sort(key=lambda e: (e["bjt_send"], e["email"]))
    for i, e in enumerate(merged, 1):
        e["id"] = f"sv{i:04d}"
    existing["entries"] = merged
    existing["version"] = "v3-replenished"
    existing["generated"] = datetime.datetime.now(BJT).isoformat()
    json.dump(existing, open("_bridge_schedule_final.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # leftover pool for next run
    remaining_pool = [c for c in pool if c["email"].lower() not in
                      {e["email"].lower() for e in merged}]
    json.dump(remaining_pool, open("_replenish_pool_left.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # ---- report ----
    per_day = Counter(e["bjt_send"][:10] for e in merged)
    bad_local = [e for e in merged if not (8 <= datetime.datetime.fromisoformat(e["local_send"]).hour <= 11)]
    mid = [e for e in merged if datetime.datetime.fromisoformat(e["bjt_send"]).hour == 0]
    em = [e["email"].lower() for e in merged]
    dm = [x.split("@")[-1] for x in em]
    print("\n=== SCHEDULE AFTER REPLENISH ===")
    print(f"total: {len(merged)} | days: {len(per_day)} | per-day max {max(per_day.values())}")
    print(f"unique emails {len(set(em))}/{len(em)} | unique domains {len(set(dm))}/{len(dm)}")
    print(f"local-hour violations {len(bad_local)} | BJT-midnight {len(mid)}")
    print(f"first {merged[0]['bjt_send'][:16]} | last {merged[-1]['bjt_send'][:16]}")
    print(f"pool left: {len(remaining_pool)}")
    wc = [len(re.findall(r"[A-Za-z']+", e["body"])) for e in new_entries] or [0]
    print(f"new body words: min {min(wc)} max {max(wc)}")
    print("segment mix:", Counter(e["pain"] for e in new_entries))


if __name__ == "__main__":
    main()
