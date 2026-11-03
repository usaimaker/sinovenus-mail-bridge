# -*- coding: utf-8 -*-
"""Probe which Wikidata SPARQL query shapes are servable (OFFSET / ORDER BY /
single-term vs multi-term). Prints one line per case: OK rows=.. ..s / FAIL .."""
import json, urllib.parse, urllib.request, ssl, time


def ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def q(terms, offset=0, order=False, limit=20):
    if isinstance(terms, str):
        flt = 'FILTER(CONTAINS(LCASE(?desc),"%s"))' % terms
    else:
        flt = 'FILTER(' + ' || '.join('CONTAINS(LCASE(?desc),"%s")' % t for t in terms) + ')'
    body = ('SELECT ?item ?website WHERE { ?item wdt:P856 ?website . ?item schema:description ?desc . '
            'FILTER(lang(?desc)="en") ' + flt + ' }')
    if order:
        body += ' ORDER BY ?item'
    body += ' LIMIT %d OFFSET %d' % (limit, offset)
    u = 'https://query.wikidata.org/sparql?query=' + urllib.parse.quote(body) + '&format=json'
    t0 = time.time()
    req = urllib.request.Request(u, headers={
        "User-Agent": "SinoVenusMailBot/1.0 (probe; mail@sinovenusltd.com)",
        "Accept": "application/json"})
    raw = urllib.request.urlopen(req, timeout=55, context=ctx()).read()
    d = json.loads(raw)
    rows = d.get("results", {}).get("bindings", [])
    return len(rows), round(time.time() - t0, 1)


MULTI = ("pet food", "pet supplies", "pet products", "pet industry", "pet care", "aquarium",
         "aquaculture", "aquatic", "ornamental fish", "fish farm", "hatchery", "reptile",
         "terrarium", "animal feed", "koi", "veterinary", "bird food", "live food")

CASES = [
    ("single aquarium off0", "aquarium", 0, False),
    ("single aquarium off100", "aquarium", 100, False),
    ("single aquarium orderby", "aquarium", 0, True),
    ("single petfood off0", "pet food", 0, False),
    ("single petfood off100", "pet food", 100, False),
    ("single reptile off0", "reptile", 0, False),
    ("multi off0", MULTI, 0, False),
    ("multi orderby", MULTI, 0, True),
]

for name, terms, off, order in CASES:
    try:
        n, el = q(terms, off, order)
        print(f"OK   {name}: rows={n} {el}s")
    except Exception as e:
        print(f"FAIL {name}: {type(e).__name__}: {e}")
    time.sleep(1)
