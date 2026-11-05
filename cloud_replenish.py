# -*- coding: utf-8 -*-
"""Cloud plumbing for the Sino Venus mail bridge.

This module owns the encrypted blob helpers and the SCHEDULE_B64_* Secret
writer. It is NO LONGER a schedule planner: planning lives in quota_guard.py
(append-only, pre-qualified, compliance-asserted). The old planner rebuilt and
RENUMBERED the whole schedule, which would have re-sent already-contacted
leads as soon as the runway ran low. See main() below.

Runs in GitHub Actions. Needs: SCHEDULE_B64_* env, DATA_KEY, PAT_TOKEN.
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


def load_blob(path, default, strict=False):
    """Read an encrypted repo blob.

    strict=True makes a read/decrypt failure fatal. Without it a wrong DATA_KEY
    or a transient API error silently yields `default`, and a later save_blob
    could overwrite a real pool/domain corpus with an empty one.
    """
    try:
        d = json.loads(api(f"https://api.github.com/repos/{REPO}/contents/{path}"))
        raw = xor(base64.b64decode(d["content"]))            # file body is xor(json)
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        print(f"load {path}: {type(e).__name__}: {e}")
        if strict:
            raise RuntimeError(f"cannot read {path} safely - refusing to continue")
        return default


def save_blob(path, obj, msg):
    content = base64.b64encode(xor(json.dumps(obj, ensure_ascii=False).encode())).decode()
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
    """Rebuild the schedule from Secret chunks - never accept a partial set.

    A hole in the middle of the chunk range (e.g. 1,2,4 present but 3 empty)
    used to break the loop early and silently drop the tail of the plan. Now a
    hole or a corrupt gzip stream is refused outright.
    """
    got = {}
    for i in range(1, 12):
        p = os.environ.get(f"SCHEDULE_B64_{i}")
        if p and p.strip():
            got[i] = p.strip()
    if not got:
        return []
    last = max(got)
    holes = [i for i in range(1, last + 1) if i not in got]
    if holes:
        print(f"read_schedule: HOLE in chunk set {holes} (last non-empty={last}) "
              f"-> refusing partial schedule")
        return []
    try:
        blob = "".join(got[i] for i in range(1, last + 1))
        return json.loads(gzip.decompress(base64.b64decode(blob))).get("entries", [])
    except Exception as e:
        print(f"read_schedule: schedule CORRUPT: {type(e).__name__} -> refusing partial schedule")
        return []


def _list_secret_names():
    try:
        sec = json.loads(api(f"https://api.github.com/repos/{REPO}/actions/secrets",
                             token=PAT, tries=3))
        return {s["name"] for s in sec.get("secrets", [])}
    except Exception as e:
        print("secret list failed:", type(e).__name__)
        return None


def write_secrets(entries):
    """Publish the schedule as chunks 1..N.

    GitHub Secrets are write-only: values cannot be read back, so we harden the
    *shape* of the chunk set instead of pretending we can verify content:
      1. remove stale high chunks FIRST and verify they are really gone (a
         leftover chunk gets concatenated by the reader and corrupts gzip);
      2. write every chunk in order with generous retries, never raising;
      3. re-list secrets and require exactly 1..N with no extras.
    Any doubt -> False. The caller must NOT consume the lead pool in that case.
    """
    from nacl import encoding, public
    try:
        raw = json.dumps({"entries": entries}, ensure_ascii=False).encode()
        b64 = base64.b64encode(gzip.compress(raw, 9)).decode()
        chunks = [b64[i:i + CHUNK] for i in range(0, len(b64), CHUNK)]
        if not chunks or len(chunks) > 10:
            print("write_secrets: bad chunk count", len(chunks))
            return False

        have = _list_secret_names()
        if have is None:
            print("write_secrets: cannot list secrets -> refusing to write")
            return False

        stale = [f"SCHEDULE_B64_{i}" for i in range(len(chunks) + 1, 11)
                 if f"SCHEDULE_B64_{i}" in have]
        for nm in stale:
            api(f"https://api.github.com/repos/{REPO}/actions/secrets/{nm}",
                method="DELETE", token=PAT, tries=4)
            print("  removed stale", nm)
        if stale:
            have = _list_secret_names()
            if have is None or any(nm in have for nm in stale):
                print("write_secrets: stale chunks still present -> refusing to write")
                return False

        pk = json.loads(api(f"https://api.github.com/repos/{REPO}/actions/secrets/public-key",
                            token=PAT, tries=4))
        pub = public.PublicKey(pk["key"].encode(), encoding.Base64Encoder())
        for i, c in enumerate(chunks, 1):
            sealed = public.SealedBox(pub).encrypt(c.encode())
            try:
                api(f"https://api.github.com/repos/{REPO}/actions/secrets/SCHEDULE_B64_{i}",
                    data=json.dumps({"encrypted_value": base64.b64encode(sealed).decode(),
                                     "key_id": pk["key_id"]}).encode(),
                    method="PUT", token=PAT, tries=6)
            except Exception as e:
                print(f"write_secrets: FAILED on chunk {i}/{len(chunks)}: {type(e).__name__} {e}")
                print("write_secrets: PARTIAL WRITE - schedule may be INCONSISTENT; "
                      "pool will NOT be consumed; health-watch will alert")
                return False
            print(f"  SCHEDULE_B64_{i} <- {len(c)} chars")

        have = _list_secret_names()
        if have is None:
            print("write_secrets: post-write verification unavailable -> reporting failure")
            return False
        expect = {f"SCHEDULE_B64_{i}" for i in range(1, len(chunks) + 1)}
        missing = expect - have
        extra = {f"SCHEDULE_B64_{i}" for i in range(len(chunks) + 1, 11)} & have
        if missing or extra:
            print(f"write_secrets: VERIFY FAILED missing={sorted(missing)} extra={sorted(extra)}")
            return False
        print(f"write_secrets: VERIFIED chunks=1..{len(chunks)}")
        return True
    except Exception as e:
        print("write_secrets: unexpected error -> NOTHING TRUSTED:", type(e).__name__, e)
        return False


def main():
    """Read-only diagnostic. NEVER writes a Secret, a blob or a schedule id.

    The historical planner in this module rebuilt the schedule, sorted it and
    renumbered every sv-id. ledger.sent_ids is an id->email map, so that would
    have re-mailed already-contacted leads the moment the runway fell below
    MIN_RUNWAY days. Its daily-cap counter was also keyed by ISO strings while
    lookups used date objects, so the cap never matched, and none of the
    B-segment / gap / group-dedupe / compliance gates existed. Planning is now
    done by quota_guard.py only, which is append-only and compliance-asserted.
    """
    entries = read_schedule()
    if not entries:
        print("DIAGNOSTIC: schedule NOT READABLE (chunk hole or corrupt gzip?)")
        return 1
    now = datetime.datetime.now(BJT)
    future = sorted({e["bjt_send"][:10] for e in entries
                     if datetime.datetime.fromisoformat(e["bjt_send"]) >= now})
    workdays = [d for d in future if datetime.date.fromisoformat(d).weekday() < 5]
    print(f"DIAGNOSTIC: schedule={len(entries)} entries | runway={len(workdays)} "
          f"future workdays | last={future[-1] if future else '-'}")
    print("DIAGNOSTIC: nothing written. schedule-replenish now runs quota_guard.py "
          "with EXTEND_RUNWAY=1 (single compliant planner).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
