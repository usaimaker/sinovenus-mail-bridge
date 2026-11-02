# -*- coding: utf-8 -*-
"""Guard: decide whether this run should actually start the chain.

- Self-chained runs (chain=1) always proceed.
- Cron watchdog runs proceed only if no other mail-bridge run is alive right now,
  so we never stack parallel senders.
"""
import os, json, urllib.request, urllib.error, datetime, sys

TOKEN = os.environ.get("GH_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
CHAIN = os.environ.get("CHAIN", "0")


def get(url):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def main():
    if CHAIN == "1":
        print("chain=1 -> proceed")
        return 0
    try:
        d = get(f"https://api.github.com/repos/{REPO}/actions/runs?status=in_progress&per_page=20")
        alive = [r for r in d.get("workflow_runs", [])
                 if str(r["id"]) != str(RUN_ID) and r.get("name") == "mail-bridge"]
        if alive:
            print(f"another run alive ({len(alive)}), watchdog yields: {[r['id'] for r in alive]}")
            return 1
        print("no live run -> watchdog starts chain")
        return 0
    except Exception as e:
        print("guard check failed, proceeding:", type(e).__name__, e)
        return 0


if __name__ == "__main__":
    sys.exit(main())
