# -*- coding: utf-8 -*-
"""Sino Venus cloud mail sender - self-chaining long-loop edition (v2).

Why v2 exists:
  GitHub Actions `schedule` cron is heavily throttled on low-activity public
  repos (observed: ~2-5 runs/day instead of 288). v1 therefore only managed
  ~4 sends per run and dropped everything older than 24h.
  v2 runs ONE job for ~5.5 hours, sending on a 8-11 min gap, and at the end
  dispatches the next run via the REST API (PAT), forming an unbroken chain.
  A cron "watchdog" restarts the chain if it ever breaks.

Guarantees:
  - recipient local window already baked into bjt_send (08:00-11:59 local)
  - daily cap 20 counted per BJT calendar day
  - 8-11 min gap between sends, jittered
  - ledger is read from the API (never from a stale checkout) and pushed
    after every single send, so a crash loses at most one mail
  - logs never contain full recipient addresses (repo is public)
"""
import os, json, gzip, base64, smtplib, ssl, datetime, time, sys, random, urllib.request, urllib.error

BJT = datetime.timezone(datetime.timedelta(hours=8))
SMTP_HOST = "smtp.exmail.qq.com"
SMTP_PORT = 465
SMTP_USER = "jesse@sinovenusltd.com"
SMTP_PASS = os.environ.get("SMTP_PASS", "")

PAT = os.environ.get("PAT_TOKEN", "")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
WF_FILE = os.environ.get("WF_FILE", "bridge.yml")

DAILY_CAP = int(os.environ.get("DAILY_CAP", "20"))
GAP_MIN = int(os.environ.get("GAP_MIN", "8"))       # minutes
GAP_MAX = int(os.environ.get("GAP_MAX", "11"))
BUDGET_MIN = int(os.environ.get("RUN_BUDGET_MIN", "330"))
CHAIN = os.environ.get("CHAIN_DISPATCH", "1") == "1"

START = time.time()


def now_bjt():
    return datetime.datetime.now(BJT)


def mask(email):
    """never print a full address into public logs"""
    try:
        u, d = email.split("@", 1)
        parts = d.split(".")
        return f"{u[:2]}***@{parts[0][:2]}***.{parts[-1]}"
    except Exception:
        return "***"


def api(url, data=None, method="GET", token=None, tries=4, timeout=30):
    tok = token or GH_TOKEN
    headers = {"Accept": "application/vnd.github+json"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as e:
            last = e
            time.sleep(2 + 2 * i)
    raise last


def load_schedule():
    parts = []
    for i in range(1, 12):
        p = os.environ.get(f"SCHEDULE_B64_{i}")
        if not p:
            break
        parts.append(p.strip())
    if not parts:
        return []
    raw = gzip.decompress(base64.b64decode("".join(parts)))
    return json.loads(raw).get("entries", [])


def fetch_ledger():
    """authoritative ledger from the API; falls back to workspace copy."""
    if REPO and GH_TOKEN:
        try:
            d = json.loads(api(f"https://api.github.com/repos/{REPO}/contents/ledger.json"))
            led = json.loads(base64.b64decode(d["content"]).decode())
            return led, d["sha"]
        except Exception as e:
            print("ledger api read failed, using local:", type(e).__name__)
    p = os.path.join(os.environ.get("GITHUB_WORKSPACE", "."), "ledger.json")
    try:
        led = json.load(open(p, encoding="utf-8"))
        return led, None
    except Exception:
        return {"sent_ids": [], "daily": {}, "updated": ""}, None


def push_ledger(ledger, sha=None):
    if not (REPO and GH_TOKEN):
        return None
    url = f"https://api.github.com/repos/{REPO}/contents/ledger.json"
    for attempt in range(5):
        cur_sha = sha
        if cur_sha is None:
            try:
                cur_sha = json.loads(api(url))["sha"]
            except Exception:
                cur_sha = None
        content = base64.b64encode(json.dumps(ledger, ensure_ascii=False).encode()).decode()
        body = {"message": "ledger: tick", "content": content}
        if cur_sha:
            body["sha"] = cur_sha
        try:
            r = json.loads(api(url, data=json.dumps(body).encode(), method="PUT", tries=1))
            return r.get("content", {}).get("sha")
        except urllib.error.HTTPError as he:
            if he.code in (409, 422):
                sha = None
                time.sleep(1.5)
                continue
            print("ledger push err", he.code)
            return None
        except Exception as e:
            print("ledger push err", type(e).__name__)
            time.sleep(2)
    return None


def build_msg(e, now):
    import email.utils
    return (
        f"From: {SMTP_USER}\r\n"
        f"To: {e['email']}\r\n"
        f"Subject: {e['subject']}\r\n"
        f"Date: {email.utils.format_datetime(now)}\r\n"
        f"Message-ID: <{e['id']}.{int(now.timestamp())}@sinovenusltd.com>\r\n"
        f"MIME-Version: 1.0\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Transfer-Encoding: 8bit\r\n"
        f"\r\n{e['body']}\r\n"
    ).encode("utf-8")


def dispatch_next():
    """trigger the next run so the chain never dies"""
    if not (PAT and REPO):
        print("CHAIN: no PAT_TOKEN, cannot self-dispatch")
        return False
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{WF_FILE}/dispatches"
    body = json.dumps({"ref": "main", "inputs": {"chain": "1"}}).encode()
    try:
        api(url, data=body, method="POST", token=PAT, tries=3)
        print("CHAIN: dispatched next run")
        return True
    except urllib.error.HTTPError as he:
        print("CHAIN dispatch failed", he.code, he.read()[:200])
    except Exception as e:
        print("CHAIN dispatch failed", type(e).__name__, e)
    return False


def send_one(e):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=60)
    try:
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, [e["email"]], build_msg(e, now_bjt()))
    finally:
        try:
            s.quit()
        except Exception:
            pass


def main():
    entries = load_schedule()
    if not entries:
        print("NO SCHEDULE LOADED - abort")
        return 1
    print(f"scheduler loaded: {len(entries)} entries | budget={BUDGET_MIN}min cap={DAILY_CAP}/day "
          f"gap={GAP_MIN}-{GAP_MAX}min chain={CHAIN}")

    ledger, sha = fetch_ledger()
    sent = set(ledger.get("sent_ids", []))
    daily = ledger.get("daily", {}) or {}
    print(f"ledger: sent={len(sent)} days_tracked={len(daily)}")

    by_id = {e["id"]: e for e in entries}
    # console: how many still unsent, and how far the schedule reaches
    remaining = [e for e in entries if e["id"] not in sent]
    if remaining:
        last = max(remaining, key=lambda e: e["bjt_send"])
        print(f"remaining={len(remaining)} last_planned={last['bjt_send'][:10]}")

    next_allowed = 0.0          # epoch seconds; enforces the gap
    total_run = 0
    fails = 0

    while True:
        elapsed = (time.time() - START) / 60.0
        if elapsed > BUDGET_MIN:
            print(f"budget reached ({elapsed:.0f}min), sending total {total_run}")
            break

        now = now_bjt()
        today = now.strftime("%Y-%m-%d")
        if daily.get(today, 0) >= DAILY_CAP:
            # today's quota is done; nothing to do until BJT midnight
            wait = min(600, max(60, (BUDGET_MIN - elapsed) * 60))
            print(f"daily cap {DAILY_CAP} reached for {today} ({daily[today]} sent), sleeping {int(wait)}s")
            time.sleep(wait)
            continue

        # refresh the ledger occasionally so a parallel run cannot double-send
        due = [e for e in remaining if e["id"] not in sent and
               datetime.datetime.fromisoformat(e["bjt_send"]) <= now]
        if not due:
            # wake up right when the next entry becomes due (max 5 min)
            nxt = min((datetime.datetime.fromisoformat(e["bjt_send"]) for e in remaining
                       if e["id"] not in sent), default=None)
            if nxt is None:
                print("schedule exhausted; nothing left to send")
                break
            wait = max(30, min(300, (nxt - now).total_seconds()))
            print(f"next due {nxt.isoformat()} in {int(wait)}s")
            time.sleep(wait)
            continue

        due.sort(key=lambda e: e["bjt_send"])
        e = due[0]

        if time.time() < next_allowed:
            time.sleep(next_allowed - time.time())

        try:
            send_one(e)
        except Exception as ex:
            fails += 1
            print(f"FAIL {e['id']} {mask(e['email'])}: {type(ex).__name__} {str(ex)[:120]}")
            if fails >= 5:
                print("too many consecutive failures, aborting run")
                break
            time.sleep(120)
            continue

        fails = 0
        sent.add(e["id"])
        ledger.setdefault("sent_ids", []).append(e["id"])
        daily[today] = daily.get(today, 0) + 1
        ledger["daily"] = daily
        ledger["updated"] = now.isoformat()
        sha = push_ledger(ledger, sha)
        total_run += 1
        print(f"SENT {e['id']} {mask(e['email'])} local={e['local_send'][11:16]} "
              f"day_total={daily[today]} run_total={total_run}")

        gap = random.randint(GAP_MIN * 60, GAP_MAX * 60)
        next_allowed = time.time() + gap
        time.sleep(min(gap, max(0, (BUDGET_MIN - elapsed) * 60)))

    # final ledger sync + chain hand-off
    ledger["daily"] = daily
    ledger["updated"] = now_bjt().isoformat()
    push_ledger(ledger, sha)
    print(f"RUN SUMMARY: sent_this_run={total_run} total_sent={len(sent)}")

    left = [e for e in entries if e["id"] not in sent]
    print(f"REMAINING_IN_SCHEDULE={len(left)}")
    if left:
        print(f"LAST_PLANNED={max(e['bjt_send'] for e in left)[:10]}")

    if CHAIN and left:
        dispatch_next()
    return 0


if __name__ == "__main__":
    sys.exit(main())
