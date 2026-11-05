# -*- coding: utf-8 -*-
"""Guard: decide whether this run should actually start the chain.

- Self-chained runs (chain=1) always proceed.
- Cron watchdog runs proceed only if no other mail-bridge run is alive right
  now, so we never stack parallel senders.

Hardened:
  * query the mail-bridge workflow's OWN runs (the old mixed `runs?per_page=20`
    feed could be crowded out by other workflows and miss a live sender);
  * retry transient API errors;
  * fail CLOSED on an inconclusive check, but use the ledger heartbeat as a
    second signal so a genuinely dead chain can still be restarted.
"""
import os, json, base64, urllib.request, urllib.error, datetime, sys, time

TOKEN = os.environ.get("GH_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
CHAIN = os.environ.get("CHAIN", "0")
WF = os.environ.get("WF_FILE", "bridge.yml")


def get(url):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def ledger_age_hours():
    """How long since the last durable ledger write (None = unknown)."""
    try:
        d = get(f"https://api.github.com/repos/{REPO}/contents/ledger.json")
        led = json.loads(base64.b64decode(d["content"]).decode())
        upd = datetime.datetime.fromisoformat(led["updated"])
        if upd.tzinfo is None:
            upd = upd.replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - upd).total_seconds() / 3600
    except Exception:
        return None


def main():
    if CHAIN == "1":
        print("chain=1 -> proceed")
        return 0
    url = (f"https://api.github.com/repos/{REPO}/actions/workflows/{WF}/runs"
           f"?status=in_progress&per_page=100")
    last = None
    for i in range(3):
        try:
            d = get(url)
            alive = [r for r in d.get("workflow_runs", [])
                     if str(r["id"]) != str(RUN_ID)]
            if alive:
                print(f"another mail-bridge run alive ({len(alive)}), "
                      f"watchdog yields: {[r['id'] for r in alive]}")
                return 1
            print("no live mail-bridge run -> watchdog starts chain")
            return 0
        except Exception as e:
            last = e
            print("guard check attempt", i + 1, "failed:", type(e).__name__)
            time.sleep(3 + 2 * i)

    # API inconclusive. A stale ledger means the chain is probably dead and
    # should be restarted; a fresh one means a sender is still active.
    age = ledger_age_hours()
    print("guard check inconclusive:", type(last).__name__ if last else "unknown",
          "| ledger age(h)=", f"{age:.2f}" if age is not None else "unknown")
    if age is not None and age > 3:
        print("ledger stale >3h -> chain likely dead, watchdog starts chain")
        return 0
    print("cannot prove the chain is dead -> fail-closed, watchdog yields")
    return 1


if __name__ == "__main__":
    sys.exit(main())
