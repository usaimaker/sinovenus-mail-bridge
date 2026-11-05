# -*- coding: utf-8 -*-
"""Independent, read-only health watch for the Sino Venus mail bridge.

Runs on its own cron. It NEVER touches the ledger, the schedule Secrets, the
sender, the pool or the chain. It only reads repo/Actions state and emails
alex@ once per reason per BJT day when something is wrong, so a silent stop is
noticed within ~2h instead of days.

Dedup state lives in health_state.json (its own file, its own commits), so it
cannot collide with the sender's ledger writes.
"""
import os, json, base64, datetime, urllib.request, urllib.error, time, smtplib, ssl

REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("PAT_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_USER = "jesse@sinovenusltd.com"
ALERT_TO = "alex@sinovenusltd.com"
DAILY_CAP = int(os.environ.get("DAILY_CAP", "20"))
BJT = datetime.timezone(datetime.timedelta(hours=8))
STATE_PATH = "health_state.json"


def now():
    return datetime.datetime.now(BJT)


def api(url, method="GET", data=None, tries=3):
    last = None
    for i in range(tries):
        try:
            hdr = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}
            if data is not None:
                hdr["Content-Type"] = "application/json"
            return json.loads(urllib.request.urlopen(
                urllib.request.Request(url, data=data, headers=hdr, method=method),
                timeout=30).read())
        except Exception as e:
            last = e
            time.sleep(2 + i)
    raise last


def read_state():
    try:
        d = api(f"https://api.github.com/repos/{REPO}/contents/{STATE_PATH}")
        return json.loads(base64.b64decode(d["content"]).decode()), d.get("sha")
    except Exception:
        return {}, None


def write_state(state, sha):
    url = f"https://api.github.com/repos/{REPO}/contents/{STATE_PATH}"
    body = {"message": "health-watch: alert dedup state",
            "content": base64.b64encode(json.dumps(state, ensure_ascii=False).encode()).decode()}
    if sha:
        body["sha"] = sha
    try:
        api(url, method="PUT", data=json.dumps(body).encode(), tries=2)
    except Exception as e:
        print("state write failed:", type(e).__name__)


def send_alert(reason, detail):
    print(f"ALERT[{reason}]: {detail}")
    if not SMTP_PASS:
        print("  SMTP_PASS missing - email skipped")
        return
    try:
        import email.utils as eu
        msg = (f"From: {SMTP_USER}\r\nTo: {ALERT_TO}\r\n"
               f"Subject: [SinoVenus][HEALTH] {reason}\r\n"
               f"Date: {eu.format_datetime(now())}\r\n"
               f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
               f"{detail}\r\nTime (BJT): {now().isoformat()}\r\n").encode("utf-8")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        s = smtplib.SMTP_SSL("smtp.exmail.qq.com", 465, context=ctx, timeout=45)
        try:
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [ALERT_TO], msg)
        finally:
            try:
                s.quit()
            except Exception:
                pass
        print("  alert email sent")
    except Exception as e:
        print("  alert email failed:", type(e).__name__, e)


def main():
    today = now().date()
    tkey = today.isoformat()
    state, sha = read_state()
    problems = []

    # 1) ledger heartbeat, daily floor, runway
    ledger = None
    try:
        d = api(f"https://api.github.com/repos/{REPO}/contents/ledger.json")
        ledger = json.loads(base64.b64decode(d["content"]).decode())
    except Exception as e:
        problems.append(("LEDGER_UNREADABLE",
                         f"health-watch cannot read ledger.json: {type(e).__name__}"))
    if ledger:
        upd = ledger.get("updated", "")
        try:
            age_h = (now() - datetime.datetime.fromisoformat(upd)).total_seconds() / 3600
            if age_h > 6:
                problems.append(("LEDGER_STALE",
                                 f"ledger.updated is {age_h:.1f}h old ({upd}) - "
                                 f"the send chain may be dead"))
        except Exception:
            problems.append(("LEDGER_BAD_TIMESTAMP",
                             f"ledger.updated unparseable: {upd!r}"))
        # BJT Sat/Sun are not quota days (they only hold US-West Friday overflow).
        if today.weekday() < 5 and now().hour >= 20:
            n = (ledger.get("daily") or {}).get(tkey, 0)
            if n < DAILY_CAP:
                problems.append(("DAILY_SHORT",
                                 f"BJT workday {tkey} sent {n}/{DAILY_CAP} by 20:00 "
                                 f"- below the daily floor"))
        rw = ledger.get("runway") or {}
        if isinstance(rw.get("days"), int) and rw["days"] < 5:
            problems.append(("RUNWAY_LOW",
                             f"only {rw['days']} runway days left "
                             f"(last {rw.get('last_day')})"))

    # 2) Actions: is the sender chain alive, did any recent run fail?
    try:
        runs = api(f"https://api.github.com/repos/{REPO}/actions/workflows/bridge.yml/runs"
                   f"?per_page=10").get("workflow_runs", [])
        alive = [r for r in runs if r.get("status") == "in_progress"]
        if not alive and runs:
            latest = runs[0]
            started = latest.get("run_started_at") or latest.get("created_at")
            t = datetime.datetime.strptime(started[:19], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=datetime.timezone.utc)
            age_h = (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 3600
            if age_h > 4:
                problems.append(("CHAIN_STALLED",
                                 f"no live mail-bridge run; latest run started "
                                 f"{age_h:.1f}h ago (id {latest.get('id')})"))
        for r in runs[:5]:
            if r.get("conclusion") == "failure":
                problems.append(("BRIDGE_RUN_FAILED",
                                 f"run {r.get('id')} failed at {r.get('created_at')}"))
                break
    except Exception as e:
        problems.append(("ACTIONS_UNREADABLE",
                         f"cannot read Actions runs: {type(e).__name__}"))

    # 3) Secret chunk SET (values are write-only, but holes must be visible)
    try:
        sec = api(f"https://api.github.com/repos/{REPO}/actions/secrets").get("secrets", [])
        names = {x["name"] for x in sec}
        have = sorted(int(x.rsplit("_", 1)[1]) for x in names
                      if x.startswith("SCHEDULE_B64_")
                      and x.rsplit("_", 1)[1].isdigit())
        if not have:
            problems.append(("SECRET_CHUNKS_MISSING",
                             "no SCHEDULE_B64_* secrets exist - sender will abort"))
        elif have != list(range(1, max(have) + 1)):
            problems.append(("SECRET_CHUNKS_BROKEN",
                             f"chunk set has holes: {have} - sender will refuse the plan"))
    except Exception as e:
        problems.append(("SECRETS_UNREADABLE",
                         f"cannot list secrets: {type(e).__name__}"))

    print(f"HEALTH: {len(problems)} problem(s), BJT {now().isoformat()}")
    fired = 0
    for reason, detail in problems:
        if state.get(reason) == tkey:
            print(f"  {reason}: already alerted today, skipped")
            continue
        send_alert(reason, detail)
        state[reason] = tkey
        fired += 1
    if problems:
        state = {k: v for k, v in state.items() if v == tkey}
        write_state(state, sha)
    print(f"HEALTH DONE: {fired} new alert(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
