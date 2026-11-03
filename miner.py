# -*- coding: utf-8 -*-
"""Cloud miner: harvest corporate emails from the domain corpus, gate by industry,
validate DNS, and append qualified leads to the encrypted pool file.

Runs in GitHub Actions. Data files in the repo are XOR+base64 obfuscated with
DATA_KEY (stored as a secret), so the public repo never shows raw domains or emails.
"""
import os, re, json, time, ssl, base64, socket, urllib.request, urllib.error, subprocess

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

BATCH = int(os.environ.get("MINE_BATCH", "180"))
REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
DATA_KEY = os.environ.get("DATA_KEY", "")

FREE_MAIL = {
    "gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com", "yahoo.com", "ymail.com",
    "aol.com", "prodigy.net.mx", "btinternet.com", "icloud.com", "me.com", "mail.com", "gmx.net",
    "gmx.de", "web.de", "free.fr", "orange.fr", "wanadoo.fr", "tiscali.it", "libero.it", "virgilio.it",
    "terra.com.br", "uol.com.br", "bol.com.br", "qq.com", "163.com", "126.com", "sina.com",
    "naver.com", "daum.net", "hanmail.net", "yandex.ru", "mail.ru", "rambler.ru", "protonmail.com",
    "msn.com", "mac.com",
}
NOISE = ("cloudflare", "wixpress", "sentry.io", "gstatic", "googleapis", "jquery", "bootstrap",
         "fontawesome", "amazonaws", "facebook.com", "fbcdn", "twitter.com", "instagram", "linkedin",
         "youtube", "schema.org", "w3.org", "shopify.com", "wp.com", "wordpress.org", "adobe.com",
         "doubleclick")
GENERIC_LOCAL = {"noreply", "no-reply", "donotreply", "webmaster", "postmaster", "abuse", "privacy",
                 "jobs", "career", "careers", "hr", "unsubscribe", "test", "example", "sentry",
                 "wordpress", "nobody", "root", "hostmaster"}

# industry gate: at least one of these must appear on the site
CORE_SEG = {
    "aquarium": ("aquarium", "aquatic", "aquaristic", "marine", "reef", "fish food", "koi",
                 "tropical fish", "ornamental fish", "gammarus", "aquascap", "aquaristik"),
    "reptile": ("reptile", "terrarium", "gecko", "bearded dragon", "exotic pet", "vivarium",
                "reptilien", "lizard", "snake", "tortoise"),
    "bird": ("aviary", "parrot", "canary", "budgerigar", "finch", "cage bird", "bird food",
             "vogel", "poultry"),
    "feed": ("animal feed", "compound feed", "aquafeed", "fish meal", "soybean meal", "feed mill",
             "livestock", "pet food manufacturer", "futtermittel", "tierfutter"),
    "pet": ("pet shop", "pet store", "pet supplies", "pet product", "pet food", "pet wholesale",
            "pet trade", "tierbedarf", "animalerie", "dierenshop", "mascotas", "zoomarket"),
}
TRADE_SEG = ("wholesale", "distributor", "import", "supplier", "b2b", "trade only", "reseller",
             "großhandel", "grosshandel", "grossiste", "mayorista", "distribuidor")

# country inference from ccTLD
TLD_COUNTRY = {
    "de": ("Germany", "Europe/Berlin"), "nl": ("Netherlands", "Europe/Amsterdam"),
    "be": ("Belgium", "Europe/Brussels"), "fr": ("France", "Europe/Paris"),
    "it": ("Italy", "Europe/Rome"), "es": ("Spain", "Europe/Madrid"),
    "pt": ("Portugal", "Europe/Lisbon"), "at": ("Austria", "Europe/Vienna"),
    "ch": ("Switzerland", "Europe/Zurich"), "dk": ("Denmark", "Europe/Copenhagen"),
    "se": ("Sweden", "Europe/Stockholm"), "no": ("Norway", "Europe/Oslo"),
    "fi": ("Finland", "Europe/Helsinki"), "pl": ("Poland", "Europe/Warsaw"),
    "cz": ("Czech Rep.", "Europe/Prague"), "sk": ("Slovakia", "Europe/Bratislava"),
    "hu": ("Hungary", "Europe/Budapest"), "ro": ("Romania", "Europe/Bucharest"),
    "gr": ("Greece", "Europe/Athens"), "ie": ("Ireland", "Europe/Dublin"),
    "uk": ("United Kingdom", "Europe/London"), "tr": ("Turkey", "Europe/Istanbul"),
    "ru": ("Russia", "Europe/Moscow"), "ua": ("Ukraine", "Europe/Kiev"),
    "au": ("Australia", "Australia/Sydney"), "nz": ("New Zealand", "Pacific/Auckland"),
    "ca": ("Canada", "America/Toronto"), "us": ("United States", "America/New_York"),
    "mx": ("Mexico", "America/Mexico_City"), "br": ("Brazil", "America/Sao_Paulo"),
    "ar": ("Argentina", "America/Argentina/Buenos_Aires"), "cl": ("Chile", "America/Santiago"),
    "co": ("Colombia", "America/Bogota"), "pe": ("Peru", "America/Lima"),
    "ec": ("Ecuador", "America/Guayaquil"), "za": ("South Africa", "Africa/Johannesburg"),
    "ae": ("United Arab Emirates", "Asia/Dubai"), "sa": ("Saudi Arabia", "Asia/Riyadh"),
    "kw": ("Kuwait", "Asia/Kuwait"), "qa": ("Qatar", "Asia/Qatar"),
    "il": ("Israel", "Asia/Jerusalem"), "in": ("India", "Asia/Kolkata"),
    "th": ("Thailand", "Asia/Bangkok"), "vn": ("Vietnam", "Asia/Ho_Chi_Minh"),
    "my": ("Malaysia", "Asia/Kuala_Lumpur"), "sg": ("Singapore", "Asia/Singapore"),
    "id": ("Indonesia", "Asia/Jakarta"), "ph": ("Philippines", "Asia/Manila"),
    "jp": ("Japan", "Asia/Tokyo"), "kr": ("South Korea", "Asia/Seoul"),
    "cn": ("China", "Asia/Shanghai"), "hk": ("Hong Kong, China", "Asia/Hong_Kong"),
    "tw": ("Taiwan, China", "Asia/Taipei"),
}


# ---------------------------------------------------------------- obfuscation
def xor(data: bytes) -> bytes:
    k = DATA_KEY.encode() or b"x"
    return bytes(b ^ k[i % len(k)] for i, b in enumerate(data))


def load_blob(path, default):
    if not (REPO and TOKEN):
        return default
    try:
        d = json.loads(gh(f"https://api.github.com/repos/{REPO}/contents/{path}"))
        raw = xor(base64.b64decode(d["content"]))            # file body is xor(json)
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        print(f"load {path} failed: {type(e).__name__}: {e}")
        return default


def save_blob(path, obj, msg):
    if not (REPO and TOKEN):
        return False
    content = base64.b64encode(xor(json.dumps(obj, ensure_ascii=False).encode())).decode()
    url = f"https://api.github.com/repos/{REPO}/contents/{path}"
    for attempt in range(5):
        try:
            sha = None
            try:
                sha = json.loads(gh(url))["sha"]
            except Exception:
                sha = None
            payload = {"message": msg, "content": content}
            if sha:
                payload["sha"] = sha
            gh(url, data=json.dumps(payload).encode(), method="PUT")
            return True
        except urllib.error.HTTPError as he:
            if he.code in (409, 422) and attempt < 4:
                time.sleep(2)
                continue
            print("save err", he.code)
            return False
        except Exception as e:
            print("save err", type(e).__name__)
            time.sleep(2)
    return False


def gh(url, data=None, method="GET", tries=3):
    last = None
    for i in range(tries):
        try:
            hdr = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}
            if data is not None:
                hdr["Content-Type"] = "application/json"
            return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=hdr,
                                                                method=method), timeout=40).read()
        except Exception as e:
            last = e
            time.sleep(2 + i)
    raise last


# ---------------------------------------------------------------- fetch
def ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def fetch(url, timeout=8, maxb=350000):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    r = urllib.request.urlopen(req, timeout=timeout, context=ctx())
    return r.read(maxb)


def strip_text(h):
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    h = h.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", h)


def resolve(domain):
    """fast DNS pre-check so dead domains cost ~0s instead of 4 tcp timeouts"""
    for host in ("www." + domain, domain):
        try:
            socket.gethostbyname(host)
            return host
        except Exception:
            continue
    return None


def analyze(html, domain):
    """extract email + segment signals from one page"""
    text = strip_text(html).lower()
    segs = {s for s, kws in CORE_SEG.items() if any(k in text for k in kws)}
    trade = any(k in text for k in TRADE_SEG)
    ems = {m.group(0).lower() for m in
           re.finditer(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", html)}
    ems = {e for e in ems if not any(x in e for x in NOISE)}
    same = {e for e in ems if e.split("@")[-1].endswith(domain)}
    pool = same or ems
    pool = {e for e in pool if e.split("@")[-1] not in FREE_MAIL}
    pool = {e for e in pool if e.split("@")[0] not in GENERIC_LOCAL}
    return {"segs": segs, "trade": trade, "emails": pool, "chars": len(text)}


def harvest(domain):
    host = resolve(domain)
    if not host:
        return None
    best = None
    for path in ("", "/contact", "/contact-us"):
        try:
            html = fetch("https://" + host + path, timeout=6).decode("utf-8", "ignore")
        except Exception:
            try:
                html = fetch("http://" + host + path, timeout=6).decode("utf-8", "ignore")
            except Exception:
                continue
        a = analyze(html, domain)
        if best is None:
            best = a
        else:
            best["segs"] |= a["segs"]
            best["trade"] = best["trade"] or a["trade"]
            best["emails"] |= a["emails"]
        # homepage already gives a usable signal -> stop early
        if a["emails"] and a["segs"]:
            break
        if path == "" and a["emails"] and a["trade"]:
            break
    if not best or not best["emails"]:
        return None

    def rank(e):
        lp = e.split("@")[0]
        for i, k in enumerate(("info", "sales", "contact", "office", "trade", "wholesale",
                               "admin", "enquiries", "export", "order")):
            if lp.startswith(k):
                return i
        return 99
    return {"email": sorted(best["emails"], key=rank)[0],
            "segments": sorted(best["segs"]), "trade": best["trade"], "chars": best["chars"]}


def mx_ok(domain):
    try:
        import dns.resolver  # noqa
        return True
    except Exception:
        pass
    try:
        out = subprocess.run(["python3", "-c",
                              f"import socket;print(socket.gethostbyname('{domain}'))"],
                             capture_output=True, timeout=10)
        return out.returncode == 0 and len(out.stdout.strip()) > 6
    except Exception:
        return False


def country_of(domain):
    tld = domain.rsplit(".", 1)[-1]
    if tld == "uk" or domain.endswith(".co.uk") or domain.endswith(".org.uk"):
        return TLD_COUNTRY["uk"]
    return TLD_COUNTRY.get(tld, ("United States", "America/New_York"))


def _wd_get(url, tries=3, timeout=30):
    """Wikidata GET with retry (proxy/TLS can be flaky)."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "SinoVenusMailBot/1.0 (lead discovery; mail@sinovenusltd.com)",
                "Accept": "application/json"})
            return urllib.request.urlopen(req, timeout=timeout, context=ctx()).read()
        except Exception as e:
            last = e
            time.sleep(1 + i)
    raise last


# Discovery tuning. The Wikidata description is only used to (a) keep the item in
# the pet/aquatic/feed industry and (b) drop retail stores and manufacturers.
# Downstream the miner still gates each site by its own content, so we do NOT
# require the description to literally say "distributor" (that yielded ~0 rows).
IND_TERMS = ("pet food", "pet supplies", "pet products", "pet industry", "pet care",
             "aquarium", "aquaculture", "aquatic", "ornamental fish", "fish farm",
             "hatchery", "reptile", "terrarium", "animal feed", "koi", "veterinary",
             "bird food", "pet wholesale", "pet distributor", "live food")
B2B_TERMS = ("distribut", "wholesal", "importer", "import ", "supplier", "supply ",
             "exporter", "export ", "trade", "b2b", "dealer", "reseller", "stockist")
RETAIL_EXCL = ("retail", "pet store", "pet shop", "pet supermarket", "chain of stores",
               "online shop", "e-commerce", "ecommerce", "webshop", "high street")
MFG_EXCL = ("manufacturer", "produces", "producer of", "maker of", "brand of",
            "fabricat", "brewing")


# Industry+role full-text search phrases (Wikidata list=search).
DISC_QUERIES = [
    "pet food distributor", "pet food wholesaler", "pet products distributor",
    "pet supplies wholesaler", "pet supplies importer", "aquarium wholesaler",
    "aquatic products distributor", "ornamental fish supplier", "fish farm",
    "aquaculture company", "hatchery", "reptile products distributor",
    "terrarium products", "animal feed supplier", "veterinary products distributor",
    "bird food supplier", "koi farm", "live food wholesale", "pet food importer",
    "aquarium products company",
]


def _mw(params):
    """Call the Wikidata MediaWiki API (fast full-text search + entity fetch)."""
    import urllib.parse
    u = "https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode(params)
    return json.loads(_wd_get(u, tries=3, timeout=25))


def discover_wikidata(after="", max_new=150):
    """Self-sustaining lead discovery via Wikidata's full-text SEARCH API.

    SPARQL was a dead end here: FILTER(CONTAINS(LCASE(?desc),...)) is a full table
    scan -- even a single term took 24-46s and every OFFSET>0 or ORDER BY hit the
    60s server timeout, so it could never paginate. Instead we use
    action=query&list=search (Elasticsearch-backed, fast, supports sroffset) to find
    items matching industry+role phrases, then wbgetentities to pull each item's
    official website (P856). Retail stores and manufacturers are dropped by their
    description; the miner's own site-content gate still makes the final call.

    `after` is a resumable cursor "query_index:sroffset" persisted in discover.b64.
    """
    import urllib.parse

    if max_new <= 0:
        return [], after
    try:
        qi, off = [int(x) for x in (str(after) or "0:0").split(":")]
    except Exception:
        qi, off = 0, 0

    out, seen = [], set()
    pages = 0
    while len(out) < max_new and pages < 24:
        if qi >= len(DISC_QUERIES):
            qi, off = 0, 0          # wrap for the next cycle
            break
        term = DISC_QUERIES[qi]
        try:
            d = _mw({"action": "query", "list": "search", "srsearch": term,
                     "srlimit": "50", "sroffset": str(off), "srnamespace": "0",
                     "format": "json"})
        except Exception as e:
            print("SEARCH failed '%s' off=%d: %s %s" % (term, off, type(e).__name__, e))
            qi += 1
            off = 0
            pages += 1
            continue
        hits = (d.get("query", {}) or {}).get("search", []) or []
        ids = [h.get("title", "") for h in hits if str(h.get("title", "")).startswith("Q")]
        if ids:
            try:
                ent = _mw({"action": "wbgetentities", "ids": "|".join(ids),
                           "props": "claims|descriptions", "languages": "en",
                           "format": "json"})
                for qid, e in (ent.get("entities", {}) or {}).items():
                    web = None
                    for c in (e.get("claims", {}) or {}).get("P856", []) or []:
                        try:
                            web = c["mainsnak"]["datavalue"]["value"]
                            if web:
                                break
                        except Exception:
                            pass
                    if not web:
                        continue
                    desc = (((e.get("descriptions", {}) or {}).get("en", {}) or {})
                            .get("value", "") or "").lower()
                    dom = re.sub(r"^https?://", "", web.lower()).split("/")[0].split(":")[0].strip(".")
                    if dom.startswith("www."):
                        dom = dom[4:]
                    if not dom or "." not in dom or dom in seen:
                        continue
                    if dom.endswith("wikidata.org") or dom.endswith("wikipedia.org"):
                        continue
                    if any(k in desc for k in RETAIL_EXCL):
                        continue
                    if any(k in desc for k in MFG_EXCL):
                        continue
                    seen.add(dom)
                    out.append(dom)
            except Exception as e:
                print("ENTITIES failed '%s': %s %s" % (term, type(e).__name__, e))
        print("SEARCH '%s' off=%d hits=%d kept=%d" % (term, off, len(hits), len(out)))
        pages += 1
        if len(hits) < 50:
            qi += 1
            off = 0
        else:
            off += 50
    next_after = "%d:%d" % (qi, off)
    print("SEARCH total=%d candidates, next=%s" % (len(out), next_after))
    return out[:max_new], next_after


def main():
    if not DATA_KEY:
        print("NO DATA_KEY - abort")
        return 1
    domains = load_blob("domains.b64", [])
    pool = load_blob("pool.b64", [])
    print(f"corpus={len(domains)} pool={len(pool)}")
    if not domains:
        print("corpus empty - nothing to mine")
        return 0

    taken = set()
    for p in pool:
        if "email" in p:
            taken.add(p["email"].lower())
    led = {}
    try:
        led = json.loads(gh(f"https://api.github.com/repos/{REPO}/contents/ledger.json"))
        led = json.loads(base64.b64decode(led["content"]).decode())
        for b in led.get("bounced", []) or []:
            taken.add(b.lower())
        mined = set(led.get("mined_domains", []) or [])
    except Exception:
        mined = set()

    # ---- self-top-up: discover new pet/aquatic/feed company domains from Wikidata ----
    try:
        disc = load_blob("discover.b64", {"after": ""})
        prev_after = disc.get("after", "") or ""
        cands, new_after = discover_wikidata(prev_after)
        pool_doms = {p["email"].split("@")[-1].lower() for p in pool if "email" in p}
        bounced = {b.lower() for b in (led.get("bounced", []) or [])}
        existing = set(domains) | mined | pool_doms | bounced
        fresh = [d for d in cands if d not in existing and "." in d and not d.endswith(".com.com")]
        if fresh:
            domains = list(domains) + fresh
            save_blob("domains.b64", domains, f"discovery: +{len(fresh)} domains (corpus={len(domains)})")
        # always persist the cursor so a page of all-duplicates still advances
        if new_after != prev_after:
            disc["after"] = new_after
            save_blob("discover.b64", disc, "discovery cursor advanced")
        print(f"DISCOVERY: +{len(fresh)} new domains (corpus={len(domains)}), cursor ok")
    except Exception as e:
        print("DISCOVERY skipped:", type(e).__name__, e)

    batch = [d for d in domains if d not in mined][:BATCH]
    print(f"batch={len(batch)} already_mined={len(mined)}")

    from concurrent.futures import ThreadPoolExecutor, as_completed
    added, tried = [], 0
    results = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(harvest, d): d for d in batch}
        for f in as_completed(futs):
            dom = futs[f]
            mined.add(dom)
            tried += 1
            try:
                r = f.result()
            except Exception:
                continue
            if r:
                results.append((dom, r))

    for dom, r in results:
        if not r["segments"]:
            continue
        if not r["trade"] and "pet" not in r["segments"] and "feed" not in r["segments"]:
            continue
        dom_email = r["email"].split("@")[-1]
        if dom_email in taken:
            continue
        country, tz = country_of(dom_email)
        if country == "China":
            continue
        added.append({"email": r["email"], "company": dom, "country": country, "tz": tz,
                      "pain": ",".join(r["segments"])})
        taken.add(r["email"].lower())
        print(f"  + {dom} -> {r['email'][:3]}***@{dom_email.split('.')[0][:2]}*** seg={r['segments']}")

    print(f"\ntried={tried} new_leads={len(added)} hit_rate={(len(added)/max(tried,1))*100:.1f}%")

    if added:
        pool.extend(added)
        save_blob("pool.b64", pool, f"miner: +{len(added)} leads (pool={len(pool)})")
    # shrink the corpus by what we tried
    remaining = [d for d in domains if d not in mined]
    if tried:
        save_blob("domains.b64", remaining, f"miner: consumed {tried} domains ({len(remaining)} left)")
    print(f"corpus remaining={len(remaining)} pool={len(pool)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
