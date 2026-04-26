#!/usr/bin/env python3
"""
probe_tubi.py — Sanity-check Tubi's ID ranges and URL structure
before running a full re-scan.

Three phases:
  1. Spot-check  : known IDs from both catalog ranges (1000XXXXX and short)
  2. Boundary    : sample above current maxima to find new ceiling
  3. Schema dump : show JSON-LD field names from a live hit

Usage:
  python3 probe_tubi.py
  python3 probe_tubi.py --boundary-step 100   # finer boundary sweep
  python3 probe_tubi.py --id 100012345         # probe a single ID
"""

import asyncio
import aiohttp
import argparse
import json
import re
from datetime import datetime

BASE = "https://tubitv.com/movies/{}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
FETCH_BYTES = 32_000          # enough to capture <head> JSON-LD
TIMEOUT = aiohttp.ClientTimeout(total=12)
CONCURRENCY = 8

# Confirmed IDs from current catalog (spread across both ranges)
SPOT_LONG  = [100010000, 100015247, 100027860, 100042692, 100053277]
SPOT_SHORT = [7908, 9551, 12411, 22248]

LONG_MAX   = 100053782        # highest ID seen in tubi_movies_final.csv
LONG_CAP   = 100_150_000      # ceiling for boundary sweep
SHORT_MAX  = 46970            # highest ID seen in tubi_movies_0_to_500k.csv
SHORT_CAP  = 55_000


# ── HTTP ──────────────────────────────────────────────────────────────

async def fetch(session, url):
    try:
        async with session.get(url, headers=HEADERS, allow_redirects=True) as r:
            body = b""
            async for chunk in r.content.iter_chunked(4096):
                body += chunk
                if len(body) >= FETCH_BYTES:
                    break
            return r.status, str(r.url), body.decode("utf-8", errors="ignore")
    except asyncio.TimeoutError:
        return None, url, "timeout"
    except Exception as e:
        return None, url, str(e)


# ── Parsing ───────────────────────────────────────────────────────────

GONE_MARKERS = ["content unavailable", "not currently available", "we're sorry"]

def parse_jsonld(html):
    m = re.search(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.S | re.I,
    )
    if not m:
        return None
    try:
        ld = json.loads(m.group(1).strip())
    except Exception:
        return None
    # Tubi now wraps the movie node in {"@context":…, "@graph":[{…}]}
    if "@graph" in ld and isinstance(ld["@graph"], list):
        for node in ld["@graph"]:
            if isinstance(node, dict) and node.get("@type") == "Movie":
                return node
        return ld["@graph"][0] if ld["@graph"] else None
    return ld


def classify(status, final_url, html):
    if status is None:
        err = html[:80].replace("\n", " ")
        return "ERR", err
    if status == 404:
        return "404", None
    if status not in (200, 301, 302):
        return f"HTTP{status}", None

    # Redirect off /movies/ means the URL scheme changed
    if "/movies/" not in final_url:
        return "REDIR", final_url

    html_lower = html.lower()
    if any(m in html_lower for m in GONE_MARKERS):
        return "GONE", None

    ld = parse_jsonld(html)
    if ld:
        return "HIT", ld

    if "tubitv" in html_lower:
        return "HIT-NOLD", None

    return "EMPTY", None


# ── Probe routines ────────────────────────────────────────────────────

async def probe_ids(session, ids):
    responses = await asyncio.gather(*[fetch(session, BASE.format(i)) for i in ids])
    return [(i, *classify(s, u, h)) for i, (s, u, h) in zip(ids, responses)]


async def probe_boundary(session, start, cap, step):
    candidates = list(range(start, cap, step))
    responses  = await asyncio.gather(*[fetch(session, BASE.format(i)) for i in candidates])
    hits = []
    for i, (s, u, h) in zip(candidates, responses):
        kind, _ = classify(s, u, h)
        if kind in ("HIT", "HIT-NOLD"):
            hits.append(i)
    return hits


# ── Display ───────────────────────────────────────────────────────────

W = 58

def hr(char="─"):
    print(char * W)

def section(title):
    print()
    hr()
    print(f"  {title}")
    hr()


def print_spot(results):
    schema = None
    for id_, kind, extra in results:
        url = BASE.format(id_)
        if kind == "HIT":
            name = extra.get("name", "?") if isinstance(extra, dict) else "?"
            slug_url = extra.get("url", "") if isinstance(extra, dict) else ""
            slug = slug_url.split("/")[-1] if slug_url else ""
            slug_note = f"  [{slug}]" if slug else ""
            print(f"  {id_}  ✓  {name!r:.40}{slug_note}")
            if schema is None:
                schema = extra
        elif kind == "HIT-NOLD":
            print(f"  {id_}  ✓  (live — no JSON-LD block found)")
        elif kind == "REDIR":
            print(f"  {id_}  →  redirected: {extra}")
        elif kind == "GONE":
            print(f"  {id_}  ✗  unavailable")
        elif kind == "404":
            print(f"  {id_}  ✗  404")
        else:
            print(f"  {id_}  ?  {kind}: {str(extra):.50}")
    return schema


def print_schema(ld):
    if not ld:
        print("  (no JSON-LD captured)")
        return
    for k, v in ld.items():
        if isinstance(v, list):
            if v and isinstance(v[0], dict):
                inner = v[0].get("name") or v[0].get("@type") or "?"
                preview = f"[{len(v)} objects — e.g. {inner!r}]"
            else:
                preview = f"{v[:4]}{'…' if len(v) > 4 else ''}"
        elif isinstance(v, dict):
            preview = "{" + ", ".join(list(v.keys())[:5]) + "}"
        else:
            preview = str(v).replace("\n", " ")[:55]
        print(f"  {k:<22} {preview}")


# ── Entry point ───────────────────────────────────────────────────────

async def main(boundary_step, single_id):
    print(f"\nTubi probe  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"URL pattern: {BASE.format('<id>')}")

    sem = asyncio.Semaphore(CONCURRENCY)

    async def throttled_fetch(session, url):
        async with sem:
            return await fetch(session, url)

    connector = aiohttp.TCPConnector(limit=CONCURRENCY)
    async with aiohttp.ClientSession(connector=connector, timeout=TIMEOUT) as session:

        # Override fetch to use semaphore
        async def _fetch(url):
            async with sem:
                return await fetch(session, url)

        async def _probe_ids(ids):
            responses = await asyncio.gather(*[_fetch(BASE.format(i)) for i in ids])
            return [(i, *classify(s, u, h)) for i, (s, u, h) in zip(ids, responses)]

        async def _probe_boundary(start, cap, step):
            candidates = list(range(start, cap, step))
            responses  = await asyncio.gather(*[_fetch(BASE.format(i)) for i in candidates])
            return [i for i, (s, u, h) in zip(candidates, responses)
                    if classify(s, u, h)[0] in ("HIT", "HIT-NOLD")]

        # ── Single-ID mode
        if single_id is not None:
            section(f"Single probe: {single_id}")
            results = await _probe_ids([single_id])
            schema  = print_spot(results)
            if schema:
                section("JSON-LD fields")
                print_schema(schema)
            print()
            return

        # ── 1. Spot-check
        section(f"SPOT-CHECK  long range  (max known: {LONG_MAX})")
        long_results  = await _probe_ids(SPOT_LONG)
        schema = print_spot(long_results)

        section(f"SPOT-CHECK  short range  (max known: {SHORT_MAX})")
        short_results = await _probe_ids(SPOT_SHORT)
        s2 = print_spot(short_results)
        if schema is None:
            schema = s2

        # ── 2. Boundary sweep
        section(f"BOUNDARY  long range  {LONG_MAX}→{LONG_CAP}  step={boundary_step}")
        new_long = await _probe_boundary(LONG_MAX + 1, LONG_CAP, boundary_step)
        if new_long:
            print(f"  New IDs:  {new_long[0]} … {new_long[-1]}  ({len(new_long)} hits)")
        else:
            print(f"  No new IDs found up to {LONG_CAP:,}")

        section(f"BOUNDARY  short range  {SHORT_MAX}→{SHORT_CAP}  step={boundary_step}")
        new_short = await _probe_boundary(SHORT_MAX + 1, SHORT_CAP, boundary_step)
        if new_short:
            print(f"  New IDs:  {new_short[0]} … {new_short[-1]}  ({len(new_short)} hits)")
        else:
            print(f"  No new IDs found up to {SHORT_CAP:,}")

        # ── 3. Schema
        section("JSON-LD SCHEMA  (keys from one live hit)")
        print_schema(schema)

    print()
    hr("═")
    print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Probe Tubi ID ranges before re-scanning")
    ap.add_argument("--boundary-step", type=int, default=200,
                    help="Step size for boundary sweep (default: 200)")
    ap.add_argument("--id", type=int, default=None, metavar="ID",
                    help="Probe a single ID and dump its JSON-LD")
    args = ap.parse_args()
    asyncio.run(main(args.boundary_step, args.id))
