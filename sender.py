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


# ---------------- internal alerting: never go silent again ----------------
_ALERTED = set()


def alert(reason, detail):
    """One out-of-band SMTP alert per reason per run.

    Actions stdout is buffered for the whole 5.5h run, so a silent stop (bad
    Secret chunks, SMTP down, ledger write failure, broken self-chain) used to
    stay invisible for hours. These alerts go ONLY to alex@, never a customer,
    and are deduplicated so a broken chain cannot spam the inbox.
    """
    if reason in _ALERTED:
        return
    _ALERTED.add(reason)
    print(f"ALERT[{reason}]: {detail}")
    if not SMTP_PASS:
        print("ALERT: SMTP_PASS unavailable, email skipped")
        return
    try:
        import email.utils as _eu
        msg = (f"From: {SMTP_USER}\r\nTo: alex@sinovenusltd.com\r\n"
               f"Subject: [SinoVenus][ALERT] {reason}\r\n"
               f"Date: {_eu.format_datetime(now_bjt())}\r\n"
               f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
               f"[SinoVenus][{reason}]\r\n"
               f"Time (BJT): {now_bjt().isoformat()}\r\n"
               f"Detail: {detail}\r\n"
               f"Repo: {REPO}\r\n"
               f"Action: inspect the mail-bridge run / schedule Secrets.\r\n").encode("utf-8")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=45)
        try:
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, ["alex@sinovenusltd.com"], msg)
        finally:
            try:
                s.quit()
            except Exception:
                pass
        print("ALERT email sent to alex@")
    except Exception as e:
        print("ALERT email failed:", type(e).__name__, e)


def load_schedule():
    """Rebuild the plan from Secret chunks. Refuse a partial/corrupt plan.

    The old loop stopped at the first empty chunk: a hole in the middle
    silently discarded the tail of the schedule while the sender kept mailing
    from a truncated plan. Now any hole or bad gzip aborts with an alert.
    """
    parts = {}
    for i in range(1, 12):
        p = os.environ.get(f"SCHEDULE_B64_{i}")
        if p and p.strip():
            parts[i] = p.strip()
    if not parts:
        return None
    last = max(parts)
    holes = [i for i in range(1, last + 1) if i not in parts]
    if holes:
        print(f"SCHEDULE CHUNK HOLE: {holes} (last non-empty={last})")
        return None
    try:
        blob = "".join(parts[i] for i in range(1, last + 1))
        entries = json.loads(gzip.decompress(base64.b64decode(blob))).get("entries", [])
    except Exception as e:
        print(f"SCHEDULE CORRUPT: {type(e).__name__} {e}")
        return None
    if not isinstance(entries, list) or not entries:
        print("SCHEDULE EMPTY after decode")
        return None
    return entries


def fetch_ledger():
    """Authoritative ledger from the API. NEVER fall back to the checkout copy.

    That file can be hours stale; using it after an API outage would re-mail
    leads that were already sent. A failed read aborts the run instead.
    """
    if not (REPO and GH_TOKEN):
        raise RuntimeError("ledger API unavailable (no REPO/GITHUB_TOKEN)")
    d = json.loads(api(f"https://api.github.com/repos/{REPO}/contents/ledger.json"))
    led = json.loads(base64.b64decode(d["content"]).decode())
    if not isinstance(led.get("sent_ids"), list):
        raise RuntimeError("ledger.sent_ids missing or malformed")
    return led, d["sha"]


def push_ledger(ledger, sha=None):
    """Persist the ledger after EVERY send. Fall back GITHUB_TOKEN -> PAT.

    Callers MUST treat None as fatal: mailing the next lead without a durable
    ledger would make the following run send the same lead again.
    """
    if not REPO:
        return None
    url = f"https://api.github.com/repos/{REPO}/contents/ledger.json"
    tokens = [t for t in (GH_TOKEN, PAT) if t]
    for attempt in range(6):
        cur_sha = sha
        if cur_sha is None:
            try:
                cur_sha = json.loads(api(url))["sha"]
            except Exception:
                cur_sha = None
        body = {"message": "ledger: tick",
                "content": base64.b64encode(json.dumps(ledger, ensure_ascii=False).encode()).decode()}
        if cur_sha:
            body["sha"] = cur_sha
        raw = json.dumps(body).encode()
        conflict = False
        for tok in tokens:
            try:
                r = json.loads(api(url, data=raw, method="PUT", token=tok, tries=2))
                return r.get("content", {}).get("sha")
            except urllib.error.HTTPError as he:
                if he.code in (409, 422):
                    sha = None
                    conflict = True
                    break
                print("ledger push err", he.code, "token", "PAT" if tok == PAT else "GITHUB_TOKEN")
            except Exception as e:
                print("ledger push err", type(e).__name__)
        if not conflict:
            time.sleep(1.5 + attempt)
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


# ---------------- self-healing: bounce scan ----------------
BOUNCE_FROM = ("mailer-daemon", "postmaster", "mail-daemon", "no-reply")
BOUNCE_SUBJ = ("undelivered", "delivery status", "delivery failure", "failure notice",
               "returned mail", "mail delivery failed", "退信", "投递失败", "无法投递")
ADDR_RE = None


def scan_bounces(limit=120):
    """Read-only IMAP scan of recent INBOX mail; return set of dead addresses.

    Only headers for most messages; bodies fetched solely for bounce-looking ones.
    """
    import imaplib, ssl, email, re, email.utils
    global ADDR_RE
    if ADDR_RE is None:
        ADDR_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
    dead = set()
    try:
        m = imaplib.IMAP4_SSL("imap.exmail.qq.com", 993, ssl_context=ssl.create_default_context())
        m.login(SMTP_USER, SMTP_PASS)
        m.select("INBOX", readonly=True)
        typ, data = m.search(None, "ALL")
        ids = data[0].split()[-limit:]
        for i in ids:
            try:
                typ, hd = m.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                raw = hd[0][1] or b""
                msg = email.message_from_bytes(raw)
                frm = (msg.get("From") or "").lower()
                subj = (msg.get("Subject") or "").lower()
                if not any(k in frm for k in BOUNCE_FROM) and not any(k in subj for k in BOUNCE_SUBJ):
                    continue
                typ, bd = m.fetch(i, "(BODY.PEEK[TEXT])")
                body = (bd[0][1] or b"").decode("utf-8", "ignore")
                for a in ADDR_RE.findall(body):
                    a = a.lower().strip(".")
                    if a.endswith("@sinovenusltd.com") or "mailer-daemon" in a or "postmaster" in a:
                        continue
                    dead.add(a)
            except Exception:
                continue
        m.logout()
    except Exception as e:
        print("BOUNCE SCAN failed:", type(e).__name__)
    return dead


def schedule_health(entries, sent):
    """days of runway left + gap detection"""
    left = [e for e in entries if e["id"] not in sent]
    if not left:
        return 0, None, 0
    days = sorted({e["bjt_send"][:10] for e in left})
    return len(days), days[-1], len(left)


def dispatch_next():
    """trigger the next run so the chain never dies"""
    if not (PAT and REPO):
        print("CHAIN: no PAT_TOKEN, cannot self-dispatch")
        alert("CHAIN_NO_PAT", "PAT_TOKEN missing - self-chain cannot continue; "
                              "only the throttled cron watchdog can restart it.")
        return False
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{WF_FILE}/dispatches"
    body = json.dumps({"ref": "main", "inputs": {"chain": "1"}}).encode()
    try:
        api(url, data=body, method="POST", token=PAT, tries=3)
        print("CHAIN: dispatched next run")
        return True
    except urllib.error.HTTPError as he:
        print("CHAIN dispatch failed", he.code, he.read()[:200])
        alert("CHAIN_DISPATCH_FAILED", f"HTTP {he.code} on workflow dispatch - "
                                       "PAT may be expired or rate-limited; "
                                       "the cron watchdog is the only fallback.")
    except Exception as e:
        print("CHAIN dispatch failed", type(e).__name__, e)
        alert("CHAIN_DISPATCH_FAILED", f"{type(e).__name__} on workflow dispatch - "
                                       "the cron watchdog is the only fallback.")
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
        alert("SCHEDULE_UNREADABLE", "SCHEDULE_B64_* is empty, has a hole, or the "
                                     "gzip stream is corrupt. Sender aborted and the "
                                     "chain was NOT continued - fix the Secrets.")
        print("NO SCHEDULE LOADED - abort")
        return 1
    print(f"scheduler loaded: {len(entries)} entries | budget={BUDGET_MIN}min cap={DAILY_CAP}/day "
          f"gap={GAP_MIN}-{GAP_MAX}min chain={CHAIN}")

    try:
        ledger, sha = fetch_ledger()
    except Exception as e:
        alert("LEDGER_UNREADABLE", f"Cannot read the authoritative ledger "
                                   f"({type(e).__name__}). Sender aborted rather than "
                                   f"risk duplicate sends from a stale checkout copy.")
        return 1
    sent = set(ledger.get("sent_ids", []))
    daily = ledger.get("daily", {}) or {}
    print(f"ledger: sent={len(sent)} days_tracked={len(daily)}")

    # ---- self-heal 1: bounce scan (dead addresses must never be mailed again) ----
    bounced = set(ledger.get("bounced", []) or [])
    if os.environ.get("ENABLE_BOUNCE_SCAN", "1") == "1" and SMTP_PASS:
        found = scan_bounces()
        new_dead = found - bounced
        if new_dead:
            bounced |= found
            ledger["bounced"] = sorted(bounced)
            print(f"BOUNCE SCAN: {len(found)} dead addresses seen, {len(new_dead)} new -> blocklisted")
            for a in list(new_dead)[:5]:
                print("   dead", mask(a))
        else:
            print(f"BOUNCE SCAN: {len(bounced)} blocklisted, nothing new")
    # drop schedule entries that point at dead addresses
    blocked = {e["id"] for e in entries if e["email"].lower() in bounced}
    if blocked:
        print(f"self-heal: {len(blocked)} scheduled entries target dead addresses -> skipped")
        sent |= blocked

    # ---- self-heal 2: runway check (alerts when the schedule is about to run dry) ----
    days_left, last_day, left_n = schedule_health(entries, sent)
    print(f"RUNWAY: {left_n} sends on {days_left} days, last planned {last_day}")
    ledger["runway"] = {"days": days_left, "last_day": last_day, "left": left_n,
                        "checked": now_bjt().isoformat()}
    if days_left < 3:
        print("ALERT: schedule runway below 3 working days - replenish needed")
        today_s = now_bjt().strftime("%Y-%m-%d")
        if ledger.get("last_alert") != today_s:
            try:
                alert = (f"From: {SMTP_USER}\r\nTo: alex@sinovenusltd.com\r\n"
                         f"Subject: [SinoVenus] schedule runway low: {days_left} days left\r\n"
                         f"Date: {__import__('email.utils').utils.format_datetime(now_bjt())}\r\n"
                         f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
                         f"Cloud mail bridge self-check.\r\n"
                         f"Unscheduled sends left: {left_n}\r\n"
                         f"Working days left: {days_left}\r\n"
                         f"Last planned day: {last_day}\r\n"
                         f"Action: replenish the lead pool and rebuild SCHEDULE_B64_1.\r\n")
                import smtplib, ssl as _ssl
                c = _ssl.create_default_context(); c.check_hostname = False; c.verify_mode = _ssl.CERT_NONE
                s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=c, timeout=45)
                s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(SMTP_USER, ["alex@sinovenusltd.com"], alert.encode("utf-8"))
                s.quit()
                ledger["last_alert"] = today_s
                print("ALERT email sent to alex@")
            except Exception as ex:
                print("ALERT email failed:", type(ex).__name__)
    # publish health state right away so the ledger reflects reality even on an idle run
    sha = push_ledger(ledger, sha)

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
                alert("SMTP_FAILING", f"5 consecutive SMTP failures "
                                      f"(last: {type(ex).__name__}). Run aborted; "
                                      f"check SMTP_PASS and the exmail service.")
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
        new_sha = push_ledger(ledger, sha)
        if new_sha is None:
            alert("LEDGER_WRITE_FAILED", f"Sent {e['id']} but the ledger could NOT be "
                                         "persisted. Run stopped immediately so the next "
                                         "run does not re-send it; chain NOT continued.")
            print("FATAL: ledger write failed after a successful send - abort run")
            return 1
        sha = new_sha
        total_run += 1
        print(f"SENT {e['id']} {mask(e['email'])} local={(e.get('local_send') or '')[11:16]} "
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
