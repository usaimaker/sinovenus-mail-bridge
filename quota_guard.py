# -*- coding: utf-8 -*-
"""Front-loaded plan builder: pre-build the ENTIRE runway at >= DAILY_CAP/day.

This is NOT a "patch the gap at send time" tool. It runs right after mining has
finished background-checking / validating new leads, and turns every qualified
lead into a fully-composed, compliance-checked schedule entry for a FUTURE BJT
day. By the time a send time arrives the plan is long finished -- the sender
only has to deliver the already-written subject/body.

ISOLATION CONTRACT (do not break):
  * Never edits sender.py, cloud_replenish.py, bridge.yml or any business value.
  * Never renumbers existing entries -- ledger.sent_ids is an id->email map, so
    renumbering would cause already-sent emails to be sent again.
  * Only APPENDS new entries, with fresh ids above the current maximum.
  * Appended entries obey the same iron rules as the existing scheduler:
    recipient-local 08:00-11:59, no weekends (Gulf Fri/Sat), no BJT midnight
    cliff, >=8 min gap from any other send that BJT day, one contact per
    company (email + domain + normalised company triple dedupe), no ISP boxes,
    zero-guessing (email must sit on the company's own domain).
  * Writes nothing unless EVERY pre-write assertion passes. DRY_RUN=1 only
    prints the diagnosis and the full plan-health table.
  * Idempotent: when every future day is already at cap, it writes nothing.
"""
import os, json, gzip, base64, datetime, math, re, time
from zoneinfo import ZoneInfo
from collections import Counter
from copy import deepcopy

import replenish as R
from cloud_replenish import (REPO, PAT, GH, DATA_KEY, DAILY_CAP, CHUNK, BJT,
                             api, load_blob, save_blob, read_schedule, write_secrets)

DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
# Scan the whole runway (45 working days > current 34-day runway), not just a week.
LOOKAHEAD_DAYS = int(os.environ.get("LOOKAHEAD_DAYS", "45"))
# Safety ceiling only -- the real limit is the size of the validated pool.
# A small rolling cap used to leave far-away days short; front-loading needs all
# qualified leads placed in ONE pass.
MAX_FILL = int(os.environ.get("MAX_FILL", "300"))
MIN_GAP_MIN = 8

ISP = {"gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com", "yahoo.com",
       "ymail.com", "aol.com", "icloud.com", "me.com", "mail.com", "gmx.net", "gmx.de",
       "web.de", "free.fr", "orange.fr", "wanadoo.fr", "tiscali.it", "libero.it", "virgilio.it",
       "terra.com.br", "uol.com.br", "bol.com.br", "qq.com", "163.com", "126.com", "sina.com",
       "naver.com", "daum.net", "hanmail.net", "yandex.ru", "mail.ru", "protonmail.com",
       "msn.com", "mac.com", "btinternet.com", "prodigy.net.mx"}

CORP = re.compile(r"[^a-z0-9]+")
CORP_SUFFIX = {"ltd", "limited", "inc", "incorporated", "llc", "gmbh", "bv", "nv",
               "sa", "srl", "sarl", "ab", "as", "oy", "aps", "plc", "co", "company",
               "group", "holdings", "international", "trading", "import", "export"}

# Iron rule: never pitch a manufacturer/competitor or a brand-owned branch office.
# Exact-domain matching only (no fuzzy tokens) so genuine distributors are not killed.
EXCLUDE_DOMAINS = {
    "vitapol.pl", "iqi-petfood.com", "daro.co.za", "plantex.ch", "gibbon.se",
    "vinhthinhbio.com", "vinhthinhbio.com.vn", "sera.de", "hagen.com",
    "exo-terra.com", "zoomed.com", "repashy.com", "omegaone.com",
    "hikari.com", "fluker.com",
}


def group_key(company):
    toks = [t for t in CORP.split((company or "").lower()) if t and t not in CORP_SUFFIX]
    return " ".join(toks[:3])


def fetch_ledger():
    try:
        d = json.loads(api(f"https://api.github.com/repos/{REPO}/contents/ledger.json"))
        return json.loads(base64.b64decode(d["content"]).decode())
    except Exception as e:
        print("ledger read failed:", type(e).__name__, e)
        return {"sent_ids": [], "daily": {}, "bounced": []}


def usable_pool(pool, entries, bounced):
    """Re-verify every pooled lead before it is allowed into the plan.

    The miner already did the background check (official-site email, DNS, B2B
    signals, country/timezone). This is the second gate before scheduling:
    zero-guessing domain ownership, segment label present, exclusion list,
    ISP/bounce/triple-dedupe and a real ZoneInfo lookup.
    """
    taken_em = {e["email"].lower() for e in entries}
    taken_dom = {e["email"].split("@")[-1].lower() for e in entries}
    taken_grp = {group_key(e.get("company", "")) for e in entries}
    taken_grp.discard("")
    dead = {b.lower() for b in bounced}
    out, seen_dom, seen_grp = [], set(), set()
    why = Counter()
    for c in pool:
        em = (c.get("email") or "").lower()
        if not em or "@" not in em:
            why["no email"] += 1
            continue
        dom = em.split("@")[-1]
        company = (c.get("company") or "").lower().strip()
        if company.startswith("www."):
            company = company[4:]
        grp = group_key(company)
        if dom in ISP or em in dead or em in taken_em or dom in taken_dom:
            why["ISP/bounced/already planned"] += 1
            continue
        if dom in seen_dom or (grp and (grp in taken_grp or grp in seen_grp)):
            why["domain/company duplicate"] += 1
            continue
        if any(dom == x or dom.endswith("." + x) for x in EXCLUDE_DOMAINS):
            why["manufacturer/competitor"] += 1
            continue
        # zero-guessing: the address must sit on the company's own domain.
        # The miner's fallback path can pick up a third-party domain from a
        # contact page -- those are NOT acceptable without manual verification.
        if company and not (dom == company or dom.endswith("." + company)
                            or company.endswith("." + dom)):
            why["email not on company domain"] += 1
            continue
        if not (c.get("pain") or "").strip():
            why["no B-segment label"] += 1
            continue
        if not c.get("tz"):
            why["no timezone"] += 1
            continue
        try:
            ZoneInfo(c["tz"])                      # iron rule: tz must really resolve
        except Exception:
            why["bad timezone"] += 1
            continue
        seen_dom.add(dom)
        if grp:
            seen_grp.add(grp)
        out.append(c)
    print("PRE-QUALIFY: pool=%d passed=%d filtered=%s" %
          (len(pool), len(out), dict(why) if why else "{}"))
    return out


def day_minutes(entries):
    """BJT date -> sorted list of minute-of-day for every scheduled send."""
    out = {}
    for e in entries:
        dt = datetime.datetime.fromisoformat(e["bjt_send"])
        out.setdefault(dt.date(), []).append(dt.hour * 60 + dt.minute)
    for v in out.values():
        v.sort()
    return out


def slot_ok(bjt_dt, existing):
    """Recipient-local 08:00-11:59 is guaranteed by R.slot_time; check the rest."""
    if bjt_dt.hour == 0:                          # BJT midnight cliff
        return False
    mins = bjt_dt.hour * 60 + bjt_dt.minute
    return all(abs(mins - m) >= MIN_GAP_MIN for m in existing.get(bjt_dt.date(), []))


def build_new(entries, fresh, per_bjt, existing, max_fill):
    now = datetime.datetime.now(BJT)
    today = now.date()
    start = today + datetime.timedelta(days=1)
    days = R.next_workdays(start, LOOKAHEAD_DAYS)
    # Fill the EXISTING runway only. Extending it is schedule-replenish's job;
    # creating half-filled new days here would leave ragged 4/20 tails and
    # overlap with the replenisher's renumbering path.
    last_planned = max(datetime.datetime.fromisoformat(e["bjt_send"]).date()
                       for e in entries)
    ids = [int(m.group(1)) for e in entries
           for m in [re.fullmatch(r"sv(\d+)", str(e.get("id", "")))] if m]
    next_id = (max(ids) if ids else 0) + 1

    new, used, queue = [], set(), list(fresh)
    for day in days:
        for c in list(queue):
            if len(new) >= max_fill:
                break
            if c["country"] in R.GULF and day.weekday() == 4:   # Gulf Friday = weekend
                continue
            try:
                tz = ZoneInfo(c["tz"])
            except Exception:
                continue
            placed = False
            for seq in range(25):                                # 08:05 .. 11:54 local
                local_dt = datetime.datetime.combine(day, R.slot_time(seq), tzinfo=tz)
                bjt_dt = local_dt.astimezone(BJT)
                if bjt_dt.date() <= today:                        # never touch today/past
                    continue
                if bjt_dt.date() > last_planned:                  # never extend runway here
                    continue
                dkey = bjt_dt.date().isoformat()                 # per_bjt keys are ISO strings
                if per_bjt[dkey] >= DAILY_CAP:
                    continue
                if not slot_ok(bjt_dt, existing):
                    continue
                seg = R.classify(c)
                new.append({"id": f"sv{next_id:04d}", "email": c["email"],
                            "company": c.get("company", ""), "country": c.get("country", ""),
                            "pain": seg, "tz": c["tz"],
                            "bjt_send": bjt_dt.isoformat(), "local_send": local_dt.isoformat(),
                            "subject": R.build_subject(c, seg), "body": R.build_body(c, seg)})
                next_id += 1
                per_bjt[dkey] += 1
                existing.setdefault(bjt_dt.date(), []).append(bjt_dt.hour * 60 + bjt_dt.minute)
                existing[bjt_dt.date()].sort()
                used.add(c["email"].lower())
                queue.remove(c)
                placed = True
                break
            if placed and len(new) >= max_fill:
                break
        if len(new) >= max_fill:
            break
    return new, used


def assert_safe(original, merged, new, sent_ids):
    """Hard gates: nothing is written if any of these fail."""
    assert len(merged) == len(original) + len(new), "entry count mismatch"
    assert merged[:len(original)] == original, "existing entries were modified"
    old_by_id = {e["id"]: e["email"].lower() for e in original}
    new_by_id = {e["id"]: e["email"].lower() for e in merged}
    for sid in sent_ids:                       # already-sent ids must still map to same email
        assert sid in old_by_id, f"sent id {sid} missing"
        assert new_by_id.get(sid) == old_by_id[sid], f"sent id {sid} remapped -> duplicate send risk"
    assert len(new_by_id) == len(merged), "duplicate ids"
    after = Counter(e["bjt_send"][:10] for e in merged)
    over = {d: n for d, n in after.items() if n > DAILY_CAP}
    assert not over, f"BJT day(s) above cap: {over}"
    emails = [e["email"].lower() for e in merged]
    doms = [e.split("@")[-1] for e in emails]
    assert len(set(emails)) == len(emails), "duplicate email"
    assert len(set(doms)) == len(doms), "duplicate domain"
    for e in new:
        dt = datetime.datetime.fromisoformat(e["bjt_send"])
        assert dt.hour != 0, "midnight cliff"
        assert 8 <= datetime.datetime.fromisoformat(e["local_send"]).hour <= 11, "local window"
    raw = json.dumps({"entries": merged}, ensure_ascii=False).encode()
    nchunks = math.ceil(len(base64.b64encode(gzip.compress(raw, 9))) / CHUNK)
    assert nchunks <= 10, f"would need {nchunks} chunks (>10 supported)"
    return nchunks


def plan_health(entries, today, label):
    """Print the FULL future plan (every BJT day until runway end), not just gaps."""
    per = Counter(e["bjt_send"][:10] for e in entries)
    if not per:
        print(f"PLAN HEALTH [{label}]: EMPTY SCHEDULE")
        return [], 0
    last = max(per)
    d = today + datetime.timedelta(days=1)
    end = datetime.date.fromisoformat(last)
    rows, short, missing = [], [], 0
    while d <= end:
        key = d.isoformat()
        n = per.get(key, 0)
        # BJT Sat/Sun are never planning targets: they are either deliberately
        # empty (iron rule) or hold a few cross-midnight overflow sends whose
        # RECIPIENT local day is Friday (US West 09:00 -> BJT next-day 01:00).
        # Either way, no 20/day quota is owed on a BJT weekend.
        weekend_rest = d.weekday() >= 5
        flag = "OK" if n >= DAILY_CAP else ("WEEKEND" if weekend_rest else "SHORT")
        if flag == "SHORT":
            short.append((key, n))
            missing += DAILY_CAP - n
        rows.append((key, n, flag))
        d += datetime.timedelta(days=1)
    print(f"PLAN HEALTH [{label}] cap={DAILY_CAP}/BJT-day | future days={len(rows)} "
          f"| short_workdays={len(short)} | missing_sends={missing} "
          f"(~ = weekend, no sends by design)")
    for i in range(0, len(rows), 4):
        chunk = rows[i:i + 4]
        print("   " + "  ".join(
            f"{k[5:]}:{n:>2}/{DAILY_CAP}{'' if f == 'OK' else ('~' if f == 'WEEKEND' else '!')}"
            for k, n, f in chunk))
    return short, missing


def main():
    entries = read_schedule()
    if not entries:
        print("NO SCHEDULE READABLE - abort")
        return 1
    original = deepcopy(entries)
    ledger = fetch_ledger()
    sent_ids = set(ledger.get("sent_ids", []))
    bounced = ledger.get("bounced", []) or []

    per_bjt = Counter(e["bjt_send"][:10] for e in entries)
    today = datetime.datetime.now(BJT).date()
    print(f"schedule={len(entries)} cap={DAILY_CAP}/BJT-day dry_run={DRY_RUN}")
    print(f"today={today} already_sent_today={ledger.get('daily', {}).get(str(today), 0)}")
    short_before, missing_before = plan_health(entries, today, "BEFORE")
    if not short_before:
        print("PLAN COMPLETE: every future BJT day already at cap -> nothing to do")
        return 0
    print(f"FRONT-LOAD TARGET: {len(short_before)} workdays short, {missing_before} sends to place")

    pool = load_blob("pool.b64", [])
    fresh = usable_pool(pool, entries, bounced)
    print(f"pool={len(pool)} usable={len(fresh)}")
    if not fresh:
        print("PLAN INCOMPLETE: no qualified pooled leads -> nothing written "
              "(miner must background-check more domains first)")
        return 0

    existing = day_minutes(entries)
    new, used = build_new(entries, fresh, Counter(per_bjt), existing, MAX_FILL)
    print(f"would append {len(new)} entries:")
    by_day = Counter(e["bjt_send"][:10] for e in new)
    for d, n in sorted(by_day.items()):
        print(f"   {d}: +{n} -> {per_bjt[d] + n}/{DAILY_CAP}")
    if not new:
        print("no compliant slot could be placed -> nothing written")
        return 0

    merged = entries + new                    # append only; NEVER sort / renumber
    short_after, missing_after = plan_health(merged, today, "AFTER")
    if short_after:
        left = len(pool) - len(new)
        print(f"PLAN INCOMPLETE: {len(short_after)} future workdays still short by "
              f"{missing_after} sends | remaining pool={left} -> next mining run fills them")
    else:
        print("PLAN COMPLETE: entire runway at cap, every send pre-composed and ready")

    try:
        nchunks = assert_safe(original, merged, new, sent_ids)
        print(f"PRE-WRITE CHECKS PASS (chunks={nchunks})")
    except AssertionError as e:
        print(f"PRE-WRITE CHECK FAILED: {e} -> NOTHING WRITTEN")
        return 1

    if DRY_RUN:
        print("DRY RUN: no secret/blob written")
        return 0

    if not write_secrets(merged):
        print("secret write failed -> pool NOT consumed (schedule may be partially updated)")
        return 1
    remaining = [c for c in pool if c["email"].lower() not in used]
    save_blob("pool.b64", remaining, f"quota-guard: appended {len(new)} sends, pool left {len(remaining)}")
    print(f"DONE appended={len(new)} total={len(merged)} pool_left={len(remaining)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
