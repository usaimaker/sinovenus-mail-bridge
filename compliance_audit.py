# -*- coding: utf-8 -*-
"""Independent, READ-ONLY compliance auditor for the Sino Venus mail bridge.

Purpose (user requirement): continuous, scheduled AND unscheduled self-checks
so that no iron-rule violation and no latent risk can survive unnoticed.

It is deliberately a THIRD PARTY: it does not import sender.py, quota_guard.py
or any planner, so a bug shared by the planner and the sender cannot hide a
violation from this auditor. It never writes the ledger, the schedule Secrets,
the pool, the domains corpus or any workflow. The only file it writes is its
own audit_state.json (alert dedup + baselines).

Two layers:
  QUICK  - every run: chunk integrity, ledger/chain heartbeat, config baseline
           drift, secret/blob shape, plan completeness, recent failures.
  DEEP   - randomly (and always when QUICK found something, or when a recent
           workflow run failed, or on manual dispatch): re-derive EVERY iron
           rule for EVERY scheduled entry -- local window, weekend/Gulf,
           BJT midnight cliff, timezone offset, >=8min gap, daily cap,
           one-contact-per-company triple dedupe, B-segment/ISP/exclusion,
           bounce suppression, sent-id remap detection, message quality.

Timing is "scheduled + unscheduled": irregular cron points, a random pre-run
delay in the workflow, a random deep pass, plus automatic escalation to a deep
pass whenever anything looks wrong.
"""
import os, json, gzip, base64, datetime, urllib.request, urllib.error, time
import re, math, hashlib, secrets as _secrets, smtplib, ssl
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo

REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("PAT_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_USER = "jesse@sinovenusltd.com"
ALERT_TO = "alex@sinovenusltd.com"
BJT = datetime.timezone(datetime.timedelta(hours=8))
STATE_PATH = "audit_state.json"

DRY = os.environ.get("DRY_RUN", "0") == "1"
DEEP_ENV = os.environ.get("DEEP", "")          # "" = auto, "1" = force, "0" = quick only
DEEP_PROB = float(os.environ.get("DEEP_PROB", "0.34"))

DAILY_CAP = 20
MIN_GAP_MIN = 8
CHUNK_SIZE = 28000
MAX_CHUNKS = 10
OWNERSHIP_MAX_RATIO = 0.20     # >20% off-domain emails = systemic risk

ISP = {"gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com", "yahoo.com",
       "ymail.com", "aol.com", "icloud.com", "me.com", "mail.com", "gmx.net", "gmx.de",
       "web.de", "free.fr", "orange.fr", "wanadoo.fr", "tiscali.it", "libero.it", "virgilio.it",
       "terra.com.br", "uol.com.br", "bol.com.br", "qq.com", "163.com", "126.com", "sina.com",
       "naver.com", "daum.net", "hanmail.net", "yandex.ru", "mail.ru", "protonmail.com",
       "msn.com", "mac.com", "btinternet.com", "prodigy.net.mx"}

EXCLUDE_DOMAINS = {
    "vitapol.pl", "iqi-petfood.com", "daro.co.za", "plantex.ch", "gibbon.se",
    "vinhthinhbio.com", "vinhthinhbio.com.vn", "sera.de", "hagen.com",
    "exo-terra.com", "zoomed.com", "repashy.com", "omegaone.com",
    "hikari.com", "fluker.com",
}

GULF = {"Egypt", "Kuwait", "Saudi Arabia", "Oman", "Qatar", "Bahrain",
        "Jordan", "Iraq", "Lebanon", "United Arab Emirates", "Yemen", "Yemen, Rep."}

CORP = re.compile(r"[^a-z0-9]+")
CORP_SUFFIX = {"ltd", "limited", "inc", "incorporated", "llc", "gmbh", "bv", "nv",
               "sa", "srl", "sarl", "ab", "as", "oy", "aps", "plc", "co", "company",
               "group", "holdings", "international", "trading", "import", "export"}

# ---------------------------------------------------------------------------
# CONFIG BASELINE. These are the user's iron rules as they exist in the live
# repo. Any drift (including an "optimisation" by an AI or a human) is a
# VIOLATION and is reported. Values here are never used to CHANGE anything.
# ---------------------------------------------------------------------------
def _chunks_needles():
    return ["SCHEDULE_B64_%d: ${{ secrets.SCHEDULE_B64_%d }}" % (i, i)
            for i in range(1, 11)]

BASELINE = {
    ".github/workflows/bridge.yml": [
        "name: mail-bridge", "cron: '*/15 * * * *'", "DAILY_CAP: '20'",
        "GAP_MIN: '8'", "GAP_MAX: '11'", "RUN_BUDGET_MIN: '330'",
        "CHAIN_DISPATCH: '1'", "python -u sender.py",
        "group: mail-bridge-chain", "cancel-in-progress: false",
        "timeout-minutes: 355",
    ] + _chunks_needles(),
    ".github/workflows/quota-guard.yml": [
        "cron: '30 12 * * *'", "DAILY_CAP: '20'", "LOOKAHEAD_DAYS: '45'",
        "MAX_FILL: '300'", "group: schedule-plan", "DRY_RUN:",
    ] + _chunks_needles(),
    ".github/workflows/replenish.yml": [
        "cron: '10 19 * * *'", "EXTEND_RUNWAY: '1'", "MIN_RUNWAY_DAYS: '5'",
        "MAX_FILL: '300'", "group: schedule-plan", "python -u quota_guard.py",
    ] + _chunks_needles(),
    ".github/workflows/miner.yml": [
        "cron: '30 17 * * *'", "needs: mine", "group: schedule-plan",
        "DRY_RUN: '0'", "python quota_guard.py",
    ] + _chunks_needles(),
    ".github/workflows/health-watch.yml": [
        "cron: '0 */2 * * *'", "python -u health_watch.py", "group: health-watch",
    ],
    ".github/workflows/weekly-report.yml": ["cron: '0 18 * * 0'", "python weekly_report.py"],
    ".github/workflows/pbw-topup.yml": ["workflow_dispatch:", "python pbw_topup.py"],
    "sender.py": [
        'SMTP_HOST = "smtp.exmail.qq.com"', "SMTP_PORT = 465",
        'SMTP_USER = "jesse@sinovenusltd.com"',
        'DAILY_CAP = int(os.environ.get("DAILY_CAP", "20"))',
        'GAP_MIN = int(os.environ.get("GAP_MIN", "8"))',
        'GAP_MAX = int(os.environ.get("GAP_MAX", "11"))',
        'BUDGET_MIN = int(os.environ.get("RUN_BUDGET_MIN", "330"))',
        "SCHEDULE CHUNK HOLE", "alert(\"LEDGER_WRITE_FAILED\"",
        "def push_ledger", "NEVER fall back",
    ],
    "quota_guard.py": [
        "MIN_GAP_MIN = 8", "merged = entries + new", "def assert_safe",
        "def usable_pool", "PRE-QUALIFY", "sent_ids", "def plan_health",
    ],
    "cloud_replenish.py": [
        "def write_secrets", "write_secrets: VERIFIED chunks=",
        "def read_schedule", "Read-only diagnostic", "nothing written",
    ],
    "guard.py": ["?status=in_progress&per_page=100", "fail-closed", "ledger_age_hours"],
    "health_watch.py": ["WORKFLOW_RUN_FAILED", "SECRET_CHUNKS_BROKEN", "def send_alert"],
    "miner.py": ["strict=True", "domains.b64"],
    "pbw_topup.py": ["strict=True"],
    "weekly_report.py": ["strict=True"],
}


def now():
    return datetime.datetime.now(BJT)


def mask(email):
    try:
        u, d = str(email).split("@", 1)
        return f"{u[:2]}***@{d[:3]}***"
    except Exception:
        return "***"


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
            time.sleep(1 + i)
    raise last


def read_text(path):
    d = api(f"https://api.github.com/repos/{REPO}/contents/{path}")
    return base64.b64decode(d["content"]).decode("utf-8", "replace")


def read_json_file(path):
    d = api(f"https://api.github.com/repos/{REPO}/contents/{path}")
    return json.loads(base64.b64decode(d["content"]).decode("utf-8", "replace")), d.get("sha")


def write_state(state, sha):
    url = f"https://api.github.com/repos/{REPO}/contents/{STATE_PATH}"
    body = {"message": "compliance-audit: read-only self-check state",
            "content": base64.b64encode(json.dumps(state, ensure_ascii=False, indent=1).encode()).decode()}
    if sha:
        body["sha"] = sha
    try:
        api(url, method="PUT", data=json.dumps(body).encode(), tries=2)
        print("  audit_state.json updated")
    except Exception as e:
        print("  state write failed:", type(e).__name__)


def send_alert(codes, lines):
    print("ALERT:", ",".join(codes))
    if not SMTP_PASS:
        print("  SMTP_PASS missing - alert email skipped")
        return
    try:
        import email.utils as eu
        body = ("[SinoVenus][COMPLIANCE AUDIT] 自检发现 %d 项问题\n" % len(codes)
                + "时间 (BJT): %s\n\n" % now().isoformat()
                + "\n".join(lines)
                + "\n\n这是内部告警，未发送给任何客户；系统本身未做任何修改。\n"
                + "请查看仓库 compliance-audit 工作流日志获取完整明细。\n")
        msg = (f"From: {SMTP_USER}\r\nTo: {ALERT_TO}\r\n"
               f"Subject: [SinoVenus][AUDIT] {','.join(codes[:3])}\r\n"
               f"Date: {eu.format_datetime(now())}\r\n"
               f"Content-Type: text/plain; charset=utf-8\r\n\r\n").encode() + body.encode("utf-8")
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
        print("  alert email sent to alex@")
    except Exception as e:
        print("  alert email failed:", type(e).__name__, e)


class Audit:
    def __init__(self):
        self.items = defaultdict(list)
        self.level = {}
        self.passes = []

    def add(self, code, level, detail):
        self.level.setdefault(code, level)
        self.items[code].append(detail)

    def ok(self, name):
        self.passes.append(name)

    def results(self):
        out = []
        for code in sorted(self.items):
            ds = self.items[code]
            out.append({"code": code, "level": self.level[code], "count": len(ds),
                        "detail": " | ".join(str(x) for x in ds[:3])})
        return out


def group_key(company):
    toks = [t for t in CORP.split((company or "").lower()) if t and t not in CORP_SUFFIX]
    return " ".join(toks[:3])


def parse_dt(s):
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BJT)
    return dt


def load_schedule(a):
    got = {}
    for i in range(1, 12):
        p = os.environ.get(f"SCHEDULE_B64_{i}")
        if p and p.strip():
            got[i] = p.strip()
    if not got:
        a.add("SCHEDULE_UNREADABLE", "VIOLATION", "no SCHEDULE_B64_* env values injected")
        return [], 0, []
    last = max(got)
    holes = [i for i in range(1, last + 1) if i not in got]
    if holes:
        a.add("SCHEDULE_CHUNK_HOLE", "VIOLATION", f"holes {holes} (last={last})")
        return [], last, holes
    try:
        blob = "".join(got[i] for i in range(1, last + 1))
        entries = json.loads(gzip.decompress(base64.b64decode(blob))).get("entries", [])
    except Exception as e:
        a.add("SCHEDULE_CORRUPT", "VIOLATION", f"gzip/json decode failed: {type(e).__name__}")
        return [], last, []
    if not isinstance(entries, list) or not entries:
        a.add("SCHEDULE_EMPTY", "VIOLATION", "decoded entries empty")
        return [], last, []
    a.ok("chunk integrity %d chunks" % last)
    nch = math.ceil(len(blob) / CHUNK_SIZE)
    if nch > MAX_CHUNKS:
        a.add("CHUNK_OVER_LIMIT", "VIOLATION", f"would need {nch} chunks (max 10)")
    elif nch >= 9:
        a.add("CHUNK_NEAR_LIMIT", "RISK", f"{nch}/10 chunks used - plan near Secret limit")
    return entries, last, []


# ---------------------------------------------------------------- QUICK layer
def quick_checks(a, entries, nchunks, ledger, ctx):
    # 1) config baseline drift
    drift = []
    for path, needles in BASELINE.items():
        txt = ctx["texts"].get(path)
        if txt is None:
            drift.append(f"{path}: UNREADABLE")
            continue
        missing = [n for n in needles if n not in txt]
        if missing:
            drift.append(f"{path}: missing {missing[:2]}")
    # the retired planner must stay retired: no write_secrets call in main()
    cr = ctx["texts"].get("cloud_replenish.py", "")
    if cr:
        tail = cr.split("def main(", 1)[-1]
        active = [l for l in tail.splitlines()
                  if "write_secrets(" in l and not l.strip().startswith(("#", "\"", "'"))]
        if active:
            drift.append("cloud_replenish.py main() calls write_secrets -> renumbering risk")
    if drift:
        a.add("CONFIG_DRIFT", "VIOLATION", "; ".join(drift[:3]))
    else:
        a.ok("config baseline (%d files)" % len(BASELINE))

    # 2) ledger shape + heartbeat
    if not ledger or not isinstance(ledger.get("sent_ids"), list):
        a.add("LEDGER_UNREADABLE", "VIOLATION", "ledger.sent_ids missing/malformed")
    else:
        upd = ledger.get("updated", "")
        try:
            age = (now() - parse_dt(upd)).total_seconds() / 3600
            if age > 6:
                a.add("LEDGER_STALE", "RISK", f"ledger.updated {age:.1f}h old ({upd})")
        except Exception:
            a.add("LEDGER_BAD_TIMESTAMP", "RISK", f"updated={upd!r}")
        for d, n in (ledger.get("daily") or {}).items():
            if n > DAILY_CAP:
                a.add("DAILY_OVER_CAP", "VIOLATION", f"{d} sent {n}>{DAILY_CAP}")
        a.ok("ledger shape / daily cap / heartbeat")

    # 3) sent-id -> email mapping must never drift (renumbering bomb detector)
    by_id = {e.get("id"): e for e in entries if isinstance(e, dict)}
    sent_ids = (ledger or {}).get("sent_ids", []) or []
    missing = [s for s in sent_ids if s not in by_id]
    if missing:
        a.add("SENT_ID_MISSING", "VIOLATION",
              f"{len(missing)} sent ids absent from plan (e.g. {missing[:3]})")
    mapping = {s: (by_id[s].get("email") or "").lower() for s in sent_ids if s in by_id}
    digest = hashlib.sha256(json.dumps(mapping, sort_keys=True).encode()).hexdigest()
    prev = ctx["state"].get("sent_id_digest")
    if prev and prev != digest:
        a.add("SENT_ID_REMAP", "VIOLATION",
              "sent id -> email mapping CHANGED since last audit: risk of re-sending "
              "already-contacted leads")
    ctx["sent_digest"] = digest
    if len(by_id) != len(entries):
        a.add("DUP_ID", "VIOLATION", f"{len(entries) - len(by_id)} duplicate ids")
    if not missing:
        a.ok("sent-id mapping / id uniqueness")

    # 4) plan completeness on future BJT workdays
    per = Counter()
    for e in entries:
        try:
            per[parse_dt(e["bjt_send"]).astimezone(BJT).date().isoformat()] += 1
        except Exception:
            a.add("BAD_TIMESTAMP", "RISK", f"id={e.get('id')} unparseable bjt_send")
    today = now().date()
    future = sorted(d for d in per if d > today.isoformat())
    workdays = [d for d in future if datetime.date.fromisoformat(d).weekday() < 5]
    short = [(d, per[d]) for d in workdays if per[d] < DAILY_CAP]
    over = [(d, per[d]) for d in future if per[d] > DAILY_CAP]
    if over:
        a.add("PLAN_OVER_CAP", "VIOLATION", f"{over[:3]}")
    if short:
        a.add("PLAN_SHORT", "RISK",
              f"{len(short)} future workdays below {DAILY_CAP}: {short[:5]}")
    else:
        a.ok("every future BJT workday at cap")
    if len(workdays) < 10:
        a.add("RUNWAY_LOW", "RISK",
              f"only {len(workdays)} future workdays with sends (last={future[-1] if future else '-'})")
    else:
        a.ok("runway depth (%d workdays)" % len(workdays))
    if entries:
        unsent_overdue = 0
        for e in entries:
            if e.get("id") in sent_ids:
                continue
            try:
                if parse_dt(e["bjt_send"]) < now() - datetime.timedelta(minutes=30):
                    unsent_overdue += 1
            except Exception:
                pass
        if unsent_overdue > 5:
            a.add("OVERDUE_UNSENT", "RISK",
                  f"{unsent_overdue} scheduled sends are >30min overdue and unsent "
                  f"(sender chain may be stalled)")

    # 5) Secret chunk set
    names = ctx.get("secret_names")
    if names is None:
        a.add("SECRETS_UNREADABLE", "RISK", "cannot list Actions secrets")
    else:
        have = sorted(int(x.rsplit("_", 1)[1]) for x in names
                      if x.startswith("SCHEDULE_B64_") and x.rsplit("_", 1)[1].isdigit())
        if have != list(range(1, len(have) + 1)) or not have:
            a.add("SECRET_CHUNK_HOLE", "VIOLATION", f"secret chunk set {have}")
        elif have != list(range(1, nchunks + 1)):
            a.add("SECRET_CHUNK_MISMATCH", "VIOLATION",
                  f"secrets 1..{len(have)} but env decoded {nchunks} chunks")
        else:
            a.ok("Secret chunk set 1..%d" % nchunks)

    # 6) corpus / pool shape (never decrypt; DATA_KEY is write-only to us)
    sizes = ctx.get("sizes", {})
    prev_sizes = ctx["state"].get("blob_sizes", {})
    for blob, floor in (("domains.b64", 100000), ("pool.b64", 1000),
                        ("mined.b64", 1000), ("discover.b64", 10)):
        n = sizes.get(blob)
        if n is None:
            a.add("BLOB_MISSING", "RISK", f"{blob} absent from repo")
        elif n < floor:
            a.add("BLOB_TOO_SMALL", "VIOLATION",
                  f"{blob}={n}B < {floor}B - corpus may have been overwritten with empty data")
    old = prev_sizes.get("domains.b64")
    if old and sizes.get("domains.b64") and sizes["domains.b64"] < old * 0.8:
        a.add("CORPUS_SHRINK", "VIOLATION",
              f"domains.b64 shrank {old} -> {sizes['domains.b64']}B")
    ctx["blob_sizes"] = {k: sizes.get(k) for k in
                         ("domains.b64", "pool.b64", "mined.b64", "discover.b64")
                         if sizes.get(k) is not None}
    a.ok("corpus/pool shape")

    # 7) Actions health + unresolved alerts from health-watch
    runs = ctx.get("runs", [])
    if not runs:
        a.add("ACTIONS_UNREADABLE", "RISK", "no Actions run data available")
    else:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
        bad = []
        for r in runs:
            if r.get("conclusion") != "failure":
                continue
            try:
                t = datetime.datetime.strptime((r.get("created_at") or "")[:19],
                                               "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=datetime.timezone.utc)
                if t >= cutoff:
                    bad.append(f"{r.get('name')}#{r.get('id')}")
            except Exception:
                pass
        if bad:
            a.add("WORKFLOW_RUN_FAILED", "RISK", ", ".join(bad[:4]))
            ctx["recent_failure"] = True
        alive = [r for r in runs if r.get("name") in ("mail-bridge", "Mail Bridge")
                 and r.get("status") == "in_progress"]
        if not alive:
            bridge = [r for r in runs if r.get("name") in ("mail-bridge", "Mail Bridge")]
            if bridge:
                st = bridge[0].get("run_started_at") or bridge[0].get("created_at")
                try:
                    t = datetime.datetime.strptime(st[:19], "%Y-%m-%dT%H:%M:%S").replace(
                        tzinfo=datetime.timezone.utc)
                    age = (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 3600
                    if age > 4:
                        a.add("CHAIN_STALLED", "RISK", f"newest bridge run {age:.1f}h old, none live")
                except Exception:
                    pass
        else:
            a.ok("live sender chain")
    hs = ctx.get("health_state") or {}
    today_s = today.isoformat()
    if isinstance(hs, dict) and today_s in set(str(v) for v in hs.values()):
        a.add("UNRESOLVED_HEALTH_ALERT", "RISK",
              "health-watch raised an alert today that has not been cleared")
    return a


# ----------------------------------------------------------------- DEEP layer
def deep_checks(a, entries, ledger):
    sent_ids = set((ledger or {}).get("sent_ids", []) or [])
    bounced = {str(x).lower() for x in ((ledger or {}).get("bounced", []) or [])}
    seen_em, seen_dom, seen_grp, seen_sub = {}, {}, {}, {}
    per_day = defaultdict(list)
    words = []
    bad_tz = 0
    ownership_bad = 0
    ownership_total = 0
    today = now().date()

    for e in entries:
        eid = e.get("id", "?")
        required = ("id", "email", "company", "country", "pain", "tz",
                    "bjt_send", "local_send", "subject", "body")
        miss = [k for k in required if not e.get(k)]
        if miss:
            a.add("MISSING_FIELDS", "VIOLATION", f"{eid}: {miss}")
            continue
        if not re.fullmatch(r"sv\d+", str(eid)):
            a.add("BAD_ID", "VIOLATION", f"{eid}: id format")
        em = e["email"].lower().strip()
        dom = em.split("@")[-1] if "@" in em else ""
        company = (e.get("company") or "").lower().strip()
        if company.startswith("www."):
            company = company[4:]

        # one contact per company: email + domain + normalised group
        for label, store, key in (("DUP_EMAIL", seen_em, em),
                                  ("DUP_DOMAIN", seen_dom, dom),
                                  ("DUP_GROUP", seen_grp, group_key(company or e.get("company", "")))):
            if key and key in store:
                a.add(label, "VIOLATION", f"{eid} and {store[key]} share {mask(key) if '@' in str(key) else str(key)[:28]}")
            elif key:
                store[key] = eid

        # B-segment / zero-guessing / exclusion / suppression
        if dom in ISP:
            a.add("ISP_EMAIL", "VIOLATION", f"{eid}: {mask(em)}")
        if any(dom == x or dom.endswith("." + x) for x in EXCLUDE_DOMAINS):
            a.add("EXCLUDED_MAKER", "VIOLATION", f"{eid}: {dom}")
        if em in bounced:
            a.add("BOUNCED_IN_PLAN", "RISK", f"{eid}: {mask(em)} is a known dead address")
        ownership_total += 1
        ok_owner = (dom == company or (company and dom.endswith("." + company))
                    or (dom and company.endswith("." + dom)))
        if not ok_owner:
            ownership_bad += 1
        try:
            ZoneInfo(e["tz"])
            if e["tz"] == "America/Vancouver":
                bad_tz += 1
        except Exception:
            bad_tz += 1
            a.add("BAD_TZ", "VIOLATION", f"{eid}: {e['tz']} does not resolve")

        # time rules
        try:
            bj = parse_dt(e["bjt_send"]).astimezone(BJT)
            lo = parse_dt(e["local_send"])
        except Exception as ex:
            a.add("BAD_TIMESTAMP", "VIOLATION", f"{eid}: {type(ex).__name__}")
            continue
        if not (8 <= lo.hour <= 11):
            a.add("LOCAL_WINDOW", "VIOLATION", f"{eid}: local {lo.hour:02d}:{lo.minute:02d}")
        if bj.hour == 0:
            a.add("MIDNIGHT_CLIFF", "VIOLATION", f"{eid}: BJT {bj.isoformat()}")
        if abs((lo.astimezone(BJT) - bj).total_seconds()) > 120:
            a.add("TZ_OFFSET_MISMATCH", "RISK",
                  f"{eid}: local->BJT {lo.astimezone(BJT).isoformat()} != {bj.isoformat()}")
        weekend = {4, 5} if e.get("country") in GULF else {5, 6}
        if lo.weekday() in weekend:
            a.add("LOCAL_WEEKEND", "VIOLATION",
                  f"{eid}: {e.get('country')} local {lo.date()} weekday={lo.weekday()}")
        per_day[bj.date().isoformat()].append((bj.hour * 60 + bj.minute, eid))

        # message quality
        subj, body = e["subject"], e["body"]
        if subj in seen_sub:
            a.add("DUP_SUBJECT", "RISK", f"{eid} and {seen_sub[subj]}")
        else:
            seen_sub[subj] = eid
        if re.search(r"<[a-zA-Z/][^>]*>", body) or "<html" in body.lower():
            a.add("HTML_IN_BODY", "VIOLATION", f"{eid}: HTML markup in plain-text body")
        if re.search(r"\{[A-Za-z_]\w*\}|\bTODO\b|\bXXX\b|\bNone\b", body + " " + subj):
            a.add("PLACEHOLDER_IN_BODY", "VIOLATION", f"{eid}: unresolved placeholder")
        w = len(re.findall(r"[A-Za-z0-9'’\-]+", body))
        words.append(w)
        if w < 100 or w > 170:
            a.add("BODY_LENGTH", "RISK", f"{eid}: {w} words (expected ~120-150)")
        if "open to a short call" not in body.lower():
            a.add("MISSING_CTA", "RISK", f"{eid}: no call-to-action")
        if "jesse" not in body.lower() or "sinovenusltd.com" not in body.lower():
            a.add("MISSING_SIGNATURE", "RISK", f"{eid}: signature incomplete")

    # gaps (only a LOWER bound: the sender enforces the 8-11 min runtime gap)
    for day, slots in per_day.items():
        slots.sort()
        for i in range(1, len(slots)):
            gap = slots[i][0] - slots[i - 1][0]
            if 0 < gap < MIN_GAP_MIN:
                a.add("GAP_UNDER_8", "VIOLATION",
                      f"{day}: {slots[i-1][1]}/{slots[i][1]} only {gap}min apart")
            elif gap == 0:
                a.add("GAP_ZERO", "VIOLATION", f"{day}: {slots[i][1]} shares a slot")
        if len(slots) > DAILY_CAP:
            a.add("DAILY_OVER_CAP", "VIOLATION", f"{day}: {len(slots)} sends")

    if ownership_total and ownership_bad / ownership_total > OWNERSHIP_MAX_RATIO:
        a.add("DOMAIN_OWNERSHIP_DUBIOUS", "RISK",
              f"{ownership_bad}/{ownership_total} emails are not on the company's own domain")
    print("DEEP STATS: entries=%d body_words(min/med/max)=%d/%d/%d "
          "off_domain=%d/%d bad_tz=%d future_workdays=%d" %
          (len(entries), min(words) if words else 0,
           sorted(words)[len(words) // 2] if words else 0,
           max(words) if words else 0, ownership_bad, ownership_total, bad_tz,
           len([d for d in per_day if d > today.isoformat()
                and datetime.date.fromisoformat(d).weekday() < 5])))
    a.ok("deep per-entry iron-rule sweep")
    return a


def main():
    print("=" * 72)
    print("COMPLIANCE AUDIT | BJT %s | dry=%s | deep_env=%r" %
          (now().isoformat(), DRY, DEEP_ENV))
    state, state_sha = {}, None
    if not DRY:
        try:
            state, state_sha = read_json_file(STATE_PATH)
        except Exception:
            pass

    ctx = {"state": state, "texts": {}, "sizes": {}, "runs": [],
           "secret_names": None, "health_state": {}, "sent_digest": None,
           "blob_sizes": {}, "recent_failure": False}
    a = Audit()

    # ---- gather (all reads are best-effort; a failed read is itself a risk) ----
    for path in BASELINE:
        try:
            ctx["texts"][path] = read_text(path)
        except Exception as e:
            print("  read failed", path, type(e).__name__)
    try:
        root = api(f"https://api.github.com/repos/{REPO}/contents/")
        ctx["sizes"] = {x["name"]: x["size"] for x in root if x.get("type") == "file"}
    except Exception as e:
        a.add("REPO_UNREADABLE", "RISK", f"cannot list repo root: {type(e).__name__}")
    try:
        ledger = json.loads(read_text("ledger.json"))
    except Exception as e:
        a.add("LEDGER_UNREADABLE", "VIOLATION", f"cannot read ledger: {type(e).__name__}")
        ledger = {}
    try:
        ctx["secret_names"] = {x["name"] for x in
                               api(f"https://api.github.com/repos/{REPO}/actions/secrets").get("secrets", [])}
    except Exception:
        pass
    try:
        ctx["runs"] = api(f"https://api.github.com/repos/{REPO}/actions/runs"
                          f"?per_page=40").get("workflow_runs", [])
    except Exception:
        pass
    try:
        ctx["health_state"], _ = read_json_file("health_state.json")
    except Exception:
        pass

    entries, nchunks, _ = load_schedule(a)
    quick_checks(a, entries, nchunks, ledger, ctx)

    quick_bad = [r for r in a.results() if r["level"] != "INFO"]
    if DEEP_ENV == "1":
        deep = True
        why = "forced by dispatch input"
    elif DEEP_ENV == "0":
        deep = False
        why = "quick only (dispatch input)"
    else:
        roll = _secrets.SystemRandom().random()
        deep = roll < DEEP_PROB or bool(quick_bad) or bool(ctx["recent_failure"])
        why = ("random=%.3f<%.2f" % (roll, DEEP_PROB) if roll < DEEP_PROB
               else "escalated: quick findings=%d / recent_failure=%s" %
               (len(quick_bad), ctx["recent_failure"]))
    print("DEEP LAYER: %s (%s)" % ("ON" if deep else "OFF", why))
    if deep and entries:
        deep_checks(a, entries, ledger)

    results = a.results()
    print("\n---- CHECK RESULTS (%d passes, %d findings) ----" % (len(a.passes), len(results)))
    for p in a.passes:
        print("  PASS  ", p)
    for r in results:
        print("  %-8s %-26s x%-4d %s" %
              (r["level"], r["code"], r["count"], r["detail"][:170]))
    notable = [r for r in results if r["level"] != "INFO"]
    print("SUMMARY: entries=%d chunks=%d deep=%s findings=%d (violations=%d risks=%d)" %
          (len(entries), nchunks, deep, len(notable),
           sum(1 for r in notable if r["level"] == "VIOLATION"),
           sum(1 for r in notable if r["level"] == "RISK")))

    if DRY:
        print("DRY RUN: no alert email, no state write, no dispatch")
        return 0

    # ---- alert dedup: one email per reason per BJT day ----
    today_s = now().date().isoformat()
    alerted = state.get("alerted", {}) if isinstance(state, dict) else {}
    alerted = {k: v for k, v in alerted.items() if v == today_s}
    fresh = [r for r in notable if alerted.get(r["code"]) != today_s]
    if fresh:
        lines = ["[%s] %s x%d: %s" % (r["level"], r["code"], r["count"], r["detail"])
                 for r in fresh]
        send_alert([r["code"] for r in fresh], lines)
        for r in fresh:
            alerted[r["code"]] = today_s
    else:
        print("ALERT: none (no new findings, or already alerted today)")

    new_state = {
        "last_run": now().isoformat(),
        "last_deep": deep,
        "deep_reason": why,
        "entries": len(entries),
        "chunks": nchunks,
        "passes": len(a.passes),
        "findings": [{"code": r["code"], "level": r["level"], "count": r["count"]}
                     for r in results],
        "sent_id_digest": ctx.get("sent_digest") or state.get("sent_id_digest"),
        "blob_sizes": ctx.get("blob_sizes") or state.get("blob_sizes", {}),
        "alerted": alerted,
    }
    write_state(new_state, state_sha)
    print("AUDIT DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
