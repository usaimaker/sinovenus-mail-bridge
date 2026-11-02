# -*- coding: utf-8 -*-
"""One-shot PBW trade-directory top-up. Runs in GitHub Actions with the real
DATA_KEY secret, so it reads/writes the obfuscated blobs correctly.

Appends curated B2B pet/aquatic/reptile/bird/feed WHOLESALER & DISTRIBUTOR
domains (scraped from Pet Business World trade directory via WebFetch) into the
domain corpus, deduped against existing corpus + mined + pool + bounced.
"""
import os, re, json, base64, time, urllib.request, urllib.error

REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
DATA_KEY = os.environ.get("DATA_KEY", "")

# Curated PBW wholesalers/distributors (clear B2B, no manufacturer brands,
# no ISP/retail). B-segment gated downstream by the miner.
PBW_DOMAINS = [
    "gaia-bio.co.uk",          # entomological livestock wholesaler (exotic pet)
    "glenkrag.com",            # largest pet aquatic/garden wholesaler, Ireland
    "gorpets.co.uk",           # pet beds/toys/accessories wholesaler
    "simonsrodents.co.uk",     # livefood / small animal / reptile wholesaler
    "wellingtonpet.com",       # cat/dog food wholesaler
    "bearts.co.uk",            # animal feed mill / wholesaler
    "millieswolfheart.co.uk",  # dog food brand wholesaler
    "pettradeinnovations.com", # master distributor, European pet trade
    "magnetsteel.com",         # B2B supplier to pet specialty retailers
    "aquascape.co.uk",         # aquatic livestock wholesaler
    "smyleepets.co.uk",        # pet food/accessory wholesaler
    "best-pets.co.uk",         # pet retail merchandise wholesaler
    "aquaone.co.uk",           # aquatic distribution (Kongs group)
    "starfisheries.co.uk",     # cold/tropical fish wholesaler
]


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
                urllib.request.Request(url, data=data, headers=hdr, method=method), timeout=60).read()
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
    sha = None
    try:
        sha = json.loads(gh(url))["sha"]
    except Exception:
        sha = None
    body = {"message": msg, "content": content}
    if sha:
        body["sha"] = sha
    for _ in range(5):
        try:
            gh(url, data=json.dumps(body).encode(), method="PUT")
            return True
        except urllib.error.HTTPError as he:
            if he.code in (409, 422):
                time.sleep(2)
                try:
                    sha = json.loads(gh(url))["sha"]
                    body["sha"] = sha
                except Exception:
                    pass
                continue
            return False
        except Exception:
            time.sleep(2)
    return False


def main():
    if not (REPO and TOKEN and DATA_KEY):
        print("missing env - abort")
        return 1
    domains = load_blob("domains.b64", [])
    pool = load_blob("pool.b64", [])
    print(f"corpus={len(domains)} pool={len(pool)}")

    # gather already-known domains to avoid duplicates
    known = set(d.lower() for d in domains)
    known |= {p["email"].split("@")[-1].lower() for p in pool if "email" in p}
    try:
        led = json.loads(gh(f"https://api.github.com/repos/{REPO}/contents/ledger.json"))
        led = json.loads(base64.b64decode(led["content"]).decode())
        known |= {b.lower() for b in (led.get("bounced", []) or [])}
        known |= set(led.get("mined_domains", []) or [])
    except Exception:
        pass

    fresh = [d for d in PBW_DOMAINS if d.lower() not in known]
    if not fresh:
        print("PBW top-up: all domains already present, nothing to add")
        return 0
    merged = list(domains) + fresh
    ok = save_blob("domains.b64", merged, f"PBW top-up: +{len(fresh)} wholesaler domains (corpus={len(merged)})")
    print(f"PBW top-up: corpus {len(domains)} -> {len(merged)} (+{len(fresh)}) ok={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
