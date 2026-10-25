# -*- coding: utf-8 -*-
"""GitHub Actions bridge sender.
Reads schedule from chunked SCHEDULE_B64_* secrets (gzip+base64), ledger from ledger.json,
sends DUE entries (recipient local 08:00-11:59 already encoded in bjt_send), maintains gap,
and writes ledger back via GitHub contents API. Prints only IDs/counts (no recipient leakage).
"""
import os, json, gzip, base64, smtplib, ssl, datetime, time, sys

BJT = datetime.timezone(datetime.timedelta(hours=8))
SMTP_HOST = "smtp.exmail.qq.com"; SMTP_PORT = 465
SMTP_USER = "jesse@sinovenusltd.com"
SMTP_PASS = os.environ.get("SMTP_PASS", "")
NOW = datetime.datetime.now(BJT)
SEND_GAP = 9 * 60          # seconds between sends within a run (gap rule)
MAX_PER_RUN = 4            # cap per tick to avoid runaway after any delay
LATE_WINDOW = 24 * 60 * 60 # honor "never skip": send up to 24h late

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

def parse_bjt(s):
    return datetime.datetime.fromisoformat(s)

def build_msg(e):
    import email.utils
    body = e["body"]
    subject = e["subject"]
    msg = (
        f"From: {SMTP_USER}\r\n"
        f"To: {e['email']}\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {email.utils.format_datetime(NOW)}\r\n"
        f"Message-ID: <{e['id']}@sinovenusltd.com>\r\n"
        f"MIME-Version: 1.0\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Transfer-Encoding: 8bit\r\n"
        f"\r\n{body}\r\n"
    )
    return msg.encode("utf-8")

def main():
    entries = load_schedule()
    if not entries:
        print("NO SCHEDULE LOADED")
        return
    ledger_path = os.path.join(os.environ.get("GITHUB_WORKSPACE", "."), "ledger.json")
    try:
        ledger = json.load(open(ledger_path, encoding="utf-8"))
    except Exception:
        ledger = {"sent_ids": [], "sent_emails": [], "updated": ""}
    sent = set(ledger.get("sent_ids", []))
    if "sent_emails" in ledger:
        del ledger["sent_emails"]   # never persist recipient emails (repo is public)

    due = [e for e in entries if e["id"] not in sent and parse_bjt(e["bjt_send"]) <= NOW]
    due = [e for e in due if (NOW - parse_bjt(e["bjt_send"])).total_seconds() <= LATE_WINDOW]
    due.sort(key=lambda e: e["bjt_send"])

    print(f"now(BJT)={NOW.isoformat()} schedule={len(entries)} sent={len(sent)} due={len(due)}")
    if not due:
        ledger["updated"] = NOW.isoformat()
        json.dump(ledger, open(ledger_path, "w", encoding="utf-8"), indent=1)
        push_ledger(ledger)
        return

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    last_ts = 0
    count = 0
    for e in due[:MAX_PER_RUN]:
        if count > 0:
            since = time.time() - last_ts
            if since < SEND_GAP:
                time.sleep(SEND_GAP - since)
        try:
            s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=60)
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [e["email"]], build_msg(e))
            s.quit()
            sent.add(e["id"])
            ledger["sent_ids"].append(e["id"])
            last_ts = time.time()
            count += 1
            print(f"SENT {e['id']} -> {e['email']} local={e['local_send'][11:16]}")
        except Exception as ex:
            print(f"FAIL {e['id']}: {type(ex).__name__} {ex}")
            break

    ledger["updated"] = NOW.isoformat()
    json.dump(ledger, open(ledger_path, "w", encoding="utf-8"), indent=1)
    push_ledger(ledger)
    print(f"DONE this_run={count} total_sent={len(ledger['sent_ids'])}")

def push_ledger(ledger):
    tok = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not tok or not repo:
        return
    import urllib.request, urllib.error
    url = f"https://api.github.com/repos/{repo}/contents/ledger.json"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}",
                 "Accept": "application/vnd.github.v3+json"})
    sha = None
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
            sha = data.get("sha")
    except Exception:
        pass
    content = base64.b64encode(json.dumps(ledger, ensure_ascii=False).encode()).decode()
    body = json.dumps({"message": "update ledger", "content": content,
                       "sha": sha} if sha else {"message": "update ledger", "content": content}).encode()
    req2 = urllib.request.Request(url, data=body, headers={"Authorization": f"Bearer {tok}",
                  "Accept": "application/vnd.github.v3+json",
                  "Content-Type": "application/json"}, method="PUT")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req2, timeout=30) as r:
                print("LEDGER PUSH ok", r.status)
                return
        except urllib.error.HTTPError as he:
            if he.code == 409:  # sha conflict -> re-fetch sha and retry
                try:
                    with urllib.request.urlopen(req, timeout=30) as r:
                        sha = json.loads(r.read()).get("sha")
                except Exception:
                    pass
                body = json.dumps({"message": "update ledger", "content": content, "sha": sha}).encode()
                req2 = urllib.request.Request(url, data=body, headers=req2.headers, method="PUT")
                continue
            else:
                print("LEDGER PUSH err", he.code); return
        except Exception as ex:
            print("LEDGER PUSH err", ex); return

if __name__ == "__main__":
    main()
