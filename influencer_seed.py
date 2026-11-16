# -*- coding: utf-8 -*-
"""Influencer seeding sender. Runs in sinovenus-mail-bridge so it can use the
LIVE SMTP_PASS secret. Reads schedule/ledger from the private influencer repo
via API, sends DUE emails, writes ledger back. No data stored in this repo."""
import os, json, smtplib, ssl, datetime, time, base64, urllib.request, urllib.error

BJT = datetime.timezone(datetime.timedelta(hours=8))
NOW = datetime.datetime.now(BJT)
SMTP_HOST = "smtp.exmail.qq.com"
SMTP_PORT = 465
FROM_ADDR = "jesse@sinovenusltd.com"          # same identity the live drip uses
SMTP_PASS = os.environ.get("SMTP_PASS", "")
PAT = os.environ.get("PAT_TOKEN", "")
INF = "usaimaker/sinovenus-influencer"
SEND_GAP = 25 * 60
MAX_PER_RUN = 5
LATE_WINDOW = 24 * 3600


def gh_get(path):
    u = "https://api.github.com/repos/%s/contents/%s" % (INF, path)
    r = urllib.request.Request(u, headers={"Authorization": "Bearer " + PAT,
        "User-Agent": "wb", "Accept": "application/vnd.github+json"})
    x = urllib.request.urlopen(r, timeout=40)
    d = json.loads(x.read().decode())
    return json.loads(base64.b64decode(d["content"]).decode()), d["sha"]


def gh_put(path, obj, sha):
    u = "https://api.github.com/repos/%s/contents/%s" % (INF, path)
    body = json.dumps({"message": "influencer ledger update", "sha": sha,
        "content": base64.b64encode(json.dumps(obj, ensure_ascii=False).encode()).decode()}).encode()
    r = urllib.request.Request(u, data=body, method="PUT", headers={
        "Authorization": "Bearer " + PAT, "User-Agent": "wb",
        "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
    for _ in range(3):
        try:
            x = urllib.request.urlopen(r, timeout=40)
            print("LEDGER_PUSH", x.status)
            return
        except urllib.error.HTTPError as e:
            if e.code == 409:
                _, sha = gh_get(path)
                continue
            print("LEDGER_PUSH_ERR", e.code)
            return


def build_msg(e):
    import email.utils
    body = ("From: %s\r\nTo: %s\r\nSubject: %s\r\nDate: %s\r\n"
            "Message-ID: <%s@sinovenusltd.com>\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n%s\r\n") % (
        FROM_ADDR, e["email"], e["subject"], email.utils.format_datetime(NOW),
        e["id"], e["body"])
    return body.encode("utf-8")


def main():
    sched, _ = gh_get("data/influencer_schedule.json")
    ledger, lsha = gh_get("data/influencer_ledger.json")
    sent = set(ledger.get("sent_ids", []))
    rows = sched["rows"]
    due = []
    for e in rows:
        if e["id"] in sent:
            continue
        t = datetime.datetime.fromisoformat(e["bjt_send"])
        if t <= NOW and (NOW - t).total_seconds() <= LATE_WINDOW:
            due.append(e)
    due.sort(key=lambda e: e["bjt_send"])
    print("now=%s rows=%d sent=%d due=%d" % (NOW.isoformat(), len(rows), len(sent), len(due)))
    if not due:
        return
    if not SMTP_PASS:
        print("NO SMTP_PASS")
        return
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    last = 0
    n = 0
    for e in due[:MAX_PER_RUN]:
        if n and time.time() - last < SEND_GAP:
            time.sleep(SEND_GAP - (time.time() - last))
        try:
            s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=60, context=ctx)
            s.login(FROM_ADDR, SMTP_PASS)
            s.sendmail(FROM_ADDR, [e["email"]], build_msg(e))
            s.quit()
            ledger.setdefault("sent_ids", []).append(e["id"])
            n += 1
            last = time.time()
            print("SENT", e["id"], e["name"])
            ledger["updated"] = NOW.isoformat()
            gh_put("data/influencer_ledger.json", ledger, lsha)
            _, lsha = gh_get("data/influencer_ledger.json")
        except Exception as ex:
            print("FAIL", e["id"], type(ex).__name__, str(ex)[:200])
            break
    print("DONE", n)


if __name__ == "__main__":
    main()
