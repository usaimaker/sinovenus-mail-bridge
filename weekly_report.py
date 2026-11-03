# -*- coding: utf-8 -*-
"""Weekly mail-out health report for the Sino Venus cloud bridge.

Runs in GitHub Actions (cron Monday 02:00 BJT). Reads the public ledger plus the
encrypted corpus/pool blobs, computes health stats, and emails a Chinese summary
to alex@sinovenusltd.com. A small XOR blob (report_state.b64) tracks last-week
figures so the report can show week-over-week deltas (bounce trend, send trend).
"""
import os, re, json, base64, ssl, smtplib, datetime, time, urllib.request, urllib.error

SMTP_HOST = "smtp.exmail.qq.com"
SMTP_PORT = 465
SMTP_USER = "jesse@sinovenusltd.com"
SMTP_PASS = os.environ.get("SMTP_PASS", "")
TO = "alex@sinovenusltd.com"

REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
DATA_KEY = os.environ.get("DATA_KEY", "")


def xor(b):
    k = DATA_KEY.encode() or b"x"
    return bytes(c ^ k[i % len(k)] for i, c in enumerate(b))


def gh(url, data=None, method="GET", tries=4):
    last = None
    for i in range(tries):
        try:
            hdr = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}
            if data is not None:
                hdr["Content-Type"] = "application/json"
            return urllib.request.urlopen(
                urllib.request.Request(url, data=data, headers=hdr, method=method), timeout=40).read()
        except Exception as e:
            last = e
            time.sleep(2 + i)
    raise last


def load_blob(path, default):
    try:
        d = json.loads(gh(f"https://api.github.com/repos/{REPO}/contents/{path}"))
        return json.loads(xor(base64.b64decode(d["content"])).decode("utf-8"))
    except Exception as e:
        print("load", path, "failed:", type(e).__name__)
        return default


def save_blob(path, obj, msg):
    content = base64.b64encode(xor(json.dumps(obj, ensure_ascii=False).encode())).decode()
    url = f"https://api.github.com/repos/{REPO}/contents/{path}"
    for _ in range(5):
        try:
            sha = None
            try:
                sha = json.loads(gh(url))["sha"]
            except Exception:
                sha = None
            body = {"message": msg, "content": content}
            if sha:
                body["sha"] = sha
            gh(url, data=json.dumps(body).encode(), method="PUT")
            return True
        except urllib.error.HTTPError as he:
            if he.code in (409, 422):
                time.sleep(2)
                continue
            return False
        except Exception:
            time.sleep(2)
    return False


def fetch_ledger():
    d = json.loads(gh(f"https://api.github.com/repos/{REPO}/contents/ledger.json"))
    return json.loads(base64.b64decode(d["content"]).decode())


def send_mail(subject, body):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    msg = (f"From: {SMTP_USER}\r\nTo: {TO}\r\nSubject: {subject}\r\n"
           f"Date: {datetime.datetime.now().strftime('%a, %d %b %Y %H:%M:%S +0800')}\r\n"
           f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n")
    s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=60)
    try:
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, [TO], msg.encode("utf-8"))
    finally:
        try:
            s.quit()
        except Exception:
            pass


def main():
    if not (REPO and TOKEN and SMTP_PASS):
        print("missing env (REPO/TOKEN/SMTP_PASS) - abort")
        return 1
    led = fetch_ledger()
    sent = led.get("sent_ids", [])
    daily = led.get("daily", {})
    bounced = led.get("bounced", [])
    runway = led.get("runway", {})
    disc = led.get("wd_offset") or load_blob("discover.b64", {}).get("after", "") or "START"

    corpus = load_blob("domains.b64", [])
    pool = load_blob("pool.b64", [])

    total_sent = len(sent)
    # last 7 calendar days in BJT
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    last7 = [(today - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    week_rows = "\n".join(f"  {d}: {daily.get(d, 0)} 封" for d in last7)
    week_sent = sum(daily.get(d, 0) for d in last7)

    # week-over-week delta from report_state
    st = load_blob("report_state.b64", {})
    prev_sent = st.get("sent", 0)
    prev_bounced = st.get("bounced", 0)
    d_sent = total_sent - prev_sent
    d_bounced = len(bounced) - prev_bounced

    status = "正常 ✅" if total_sent > 0 else "暂无发送，检查链路 ⚠️"
    if runway.get("days", 99) is not None and runway.get("days", 99) < 5:
        status = "runway 偏低，需补料 ⚠️"

    body = (
        f"Sino Venus 邮件外联 · 云端周报\n"
        f"生成时间：{today.strftime('%Y-%m-%d %H:%M')} (BJT)\n"
        f"================================\n"
        f"✅ 累计发送：{total_sent} 封（自上报周报以来 +{d_sent}）\n"
        f"📅 近 7 天发送：{week_sent} 封\n{week_rows}\n\n"
        f"📦 排程 runway：{runway.get('days','?')} 天（至 {runway.get('last_day','?')}），剩余 {runway.get('left','?')} 封\n"
        f"🗂 语料剩余：{len(corpus)} 域名 | 候选池：{len(pool)} 条\n"
        f"🔎 Wikidata 自动发现续爬点：{disc}\n\n"
        f"🚫 退信累计：{len(bounced)} 个（上周 {prev_bounced}，本周 Δ{d_bounced}）\n"
        f"🩺 健康检查：{status}\n\n"
        f"— 全自动运行（发送 / 挖矿 / 补货 / 自动发现 / 退信自愈），无需人工介入 —"
    )
    subject = f"[SinoVenus] 邮件外联周报 {today.strftime('%Y-%m-%d')}｜累计 {total_sent} 封"
    try:
        send_mail(subject, body)
        print("weekly report emailed to", TO)
    except Exception as e:
        print("send failed:", type(e).__name__, e)
        return 1

    save_blob("report_state.b64", {"sent": total_sent, "bounced": len(bounced),
                                   "week": today.strftime("%G-W%V")}, "weekly report state")
    print("report_state updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
