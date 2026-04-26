#!/usr/bin/env python3
"""
tubi_scanner.py — Fresh catalog scan of Tubi's movie ID ranges.

Sweeps the long ID range (100010000+) and optionally re-verifies the
known short-ID catalog. Parses JSON-LD (including the @graph wrapper
added ~early 2026), captures slugs and availability dates.

Usage:
  python3 scripts/tubi_scanner.py                   # full long-range sweep
  python3 scripts/tubi_scanner.py --resume          # continue from checkpoint
  python3 scripts/tubi_scanner.py --verify-short    # re-check known short IDs
  python3 scripts/tubi_scanner.py --end 100070000   # custom ceiling
  python3 scripts/tubi_scanner.py --concurrent 5    # adjust concurrency
  python3 scripts/tubi_scanner.py --dry-run         # probe 50 IDs, no output
"""

import asyncio
import aiohttp
import argparse
import csv
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────
ROOT            = Path(__file__).parent.parent
DATA_DIR        = ROOT / "data"
SHORT_CSV       = DATA_DIR / "tubi_movies_0_to_500k.csv"
CHECKPOINT_FILE = DATA_DIR / "scan_checkpoint.json"

# ── Tuning ────────────────────────────────────────────────────────────
BASE        = "https://tubitv.com/movies/{}"
HEADERS     = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
FETCH_BYTES      = 32_000
TIMEOUT          = aiohttp.ClientTimeout(total=12)
BATCH_SIZE       = 100        # IDs fetched per concurrent batch
CHECKPOINT_EVERY = 1_000      # save to disk every N IDs processed
JITTER           = (0.04, 0.14)  # per-request random stagger (seconds)

LONG_START  = 100_010_000
LONG_END    = 100_065_000     # well past probe ceiling of ~100_055_400
CONCURRENCY = 8

GONE_MARKERS = ["content unavailable", "not currently available", "we're sorry"]

CSV_FIELDS = [
    "ID", "Title", "Year", "Rating", "Genres", "Directors", "Actors",
    "Duration", "Description", "Slug", "AvailStart", "AvailEnd", "Source",
]

# ── Backoff state (asyncio is single-threaded; globals are safe) ──────
_backoff_until:  float = 0.0
_backoff_streak: int   = 0


# ── Parsing ───────────────────────────────────────────────────────────

def _iso_minutes(s):
    """PT1H37M20S → 97  (rounds down to whole minutes, ignores seconds)"""
    if not s:
        return None
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?", s)
    if not m or not any(m.groups()):
        return None
    h, mn = (int(x or 0) for x in m.groups())
    total = h * 60 + mn
    return total if total > 0 else None


def _year(s):
    m = re.match(r"(\d{4})", s or "")
    return m.group(1) if m else ""


def _names(lst):
    if not isinstance(lst, list):
        return str(lst) if lst else ""
    return "|".join(
        item["name"] for item in lst
        if isinstance(item, dict) and item.get("name")
    )


def _avail(action, key):
    try:
        return action["expectsAcceptanceOf"].get(key, "")
    except (TypeError, KeyError, AttributeError):
        return ""


def parse_movie(id_, html):
    """Return a CSV-row dict, or None if page has no parseable movie."""
    if any(m in html.lower() for m in GONE_MARKERS):
        return None

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

    # Unwrap @graph (present since ~early 2026)
    node = None
    if "@graph" in ld:
        for item in ld["@graph"]:
            if isinstance(item, dict) and item.get("@type") == "Movie":
                node = item
                break
    elif isinstance(ld, dict) and ld.get("@type") == "Movie":
        node = ld

    if not node or not node.get("name"):
        return None

    slug   = node.get("url", "").rstrip("/").split("/")[-1]
    action = node.get("potentialAction", {})
    genres = node.get("genre", [])

    return {
        "ID":          str(id_),
        "Title":       node.get("name", "").strip(),
        "Year":        _year(node.get("dateCreated", "")),
        "Rating":      node.get("contentRating", ""),
        "Genres":      "|".join(genres) if isinstance(genres, list) else str(genres),
        "Directors":   _names(node.get("director")),
        "Actors":      _names(node.get("actor")),
        "Duration":    str(_iso_minutes(node.get("duration")) or ""),
        "Description": node.get("description", "").strip(),
        "Slug":        slug,
        "AvailStart":  _avail(action, "availabilityStarts"),
        "AvailEnd":    _avail(action, "availabilityEnds"),
        "Source":      "jsonld",
    }


# ── HTTP ──────────────────────────────────────────────────────────────

async def fetch(session, id_):
    global _backoff_until, _backoff_streak

    # Pause if we're in a backoff window
    wait = _backoff_until - time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait + random.uniform(0, 2))

    await asyncio.sleep(random.uniform(*JITTER))

    try:
        async with session.get(
            BASE.format(id_), headers=HEADERS, allow_redirects=True
        ) as r:
            if r.status == 429:
                _backoff_streak += 1
                delay = min(30 * (2 ** (_backoff_streak - 1)), 300)
                _backoff_until = time.monotonic() + delay
                return id_, None, "429"

            _backoff_streak = max(0, _backoff_streak - 1)

            if r.status == 404:
                return id_, None, "404"
            if r.status != 200:
                return id_, None, f"http{r.status}"

            body = b""
            async for chunk in r.content.iter_chunked(4096):
                body += chunk
                if len(body) >= FETCH_BYTES:
                    break
            return id_, body.decode("utf-8", errors="ignore"), "ok"

    except asyncio.TimeoutError:
        return id_, None, "timeout"
    except Exception as e:
        return id_, None, f"err:{str(e)[:40]}"


# ── I/O ───────────────────────────────────────────────────────────────

def write_csv(rows, path):
    if not rows:
        return
    new_file = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(rows)


def load_checkpoint():
    if CHECKPOINT_FILE.exists():
        try:
            return json.loads(CHECKPOINT_FILE.read_text())
        except Exception:
            pass
    return {}


def save_checkpoint(data):
    data["updated"] = datetime.now().isoformat()
    CHECKPOINT_FILE.write_text(json.dumps(data, indent=2))


def load_short_ids():
    if not SHORT_CSV.exists():
        sys.exit(f"Short-ID CSV not found: {SHORT_CSV}")
    ids = []
    with open(SHORT_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                ids.append(int(row["ID"].strip().strip('"')))
            except (ValueError, KeyError):
                pass
    return ids


# ── Progress ──────────────────────────────────────────────────────────

def _eta_str(done, total, elapsed):
    if done == 0 or elapsed == 0:
        return "   ?"
    secs = (total - done) / (done / elapsed)
    if secs < 60:
        return f"{secs:3.0f}s"
    if secs < 3600:
        return f"{secs/60:3.0f}m"
    return f"{secs/3600:.1f}h"


def show_progress(done, total, found, errors, elapsed):
    pct  = done / total * 100 if total else 0
    eta  = _eta_str(done, total, elapsed)
    rate = done / elapsed if elapsed else 0
    line = (f"\r  [{done:>6,} /{total:,}]  {pct:4.1f}%"
            f"  +{found:<5,} found"
            f"  {errors} err"
            f"  {rate:4.1f}/s"
            f"  eta {eta}")
    print(line[:120], end="", flush=True)


# ── Core scan ─────────────────────────────────────────────────────────

async def scan(ids, csv_path, concurrent, dry_run):
    semaphore  = asyncio.Semaphore(concurrent)
    stats      = {"found": 0, "not_found": 0, "errors": 0, "last_id": ids[0]}
    start_time = time.monotonic()
    total      = len(ids)
    done       = 0
    prev_checkpoint_marker = 0

    connector = aiohttp.TCPConnector(limit=concurrent, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector, timeout=TIMEOUT) as session:

        async def probe(id_):
            async with semaphore:
                return await fetch(session, id_)

        for batch_start in range(0, total, BATCH_SIZE):
            batch   = ids[batch_start : batch_start + BATCH_SIZE]
            results = await asyncio.gather(*[probe(i) for i in batch])

            found_this_batch = []
            for id_, html, status in results:
                done += 1
                if html:
                    movie = parse_movie(id_, html)
                    if movie:
                        found_this_batch.append(movie)
                        stats["found"] += 1
                        # Print hit above the progress line
                        print(f"\n  + {id_}  {movie['Title'][:50]!r}")
                    else:
                        stats["not_found"] += 1
                elif status == "404":
                    stats["not_found"] += 1
                else:
                    stats["errors"] += 1

            if not dry_run and found_this_batch:
                write_csv(found_this_batch, csv_path)

            stats["last_id"] = batch[-1]
            elapsed = time.monotonic() - start_time
            show_progress(done, total, stats["found"], stats["errors"], elapsed)

            # Checkpoint when we cross a CHECKPOINT_EVERY boundary
            marker = done // CHECKPOINT_EVERY
            if not dry_run and marker > prev_checkpoint_marker:
                save_checkpoint({**stats, "csv": str(csv_path)})
                prev_checkpoint_marker = marker

    print()  # end progress line
    return stats


# ── Entry point ───────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Scan Tubi movie catalog")
    ap.add_argument("--resume",       action="store_true",
                    help="Continue from last saved checkpoint")
    ap.add_argument("--verify-short", action="store_true",
                    help="Re-verify known short-ID catalog only")
    ap.add_argument("--start",  type=int, default=LONG_START,
                    help=f"Long-range start ID (default: {LONG_START:,})")
    ap.add_argument("--end",    type=int, default=LONG_END,
                    help=f"Long-range end ID exclusive (default: {LONG_END:,})")
    ap.add_argument("--concurrent", type=int, default=CONCURRENCY,
                    help=f"Concurrent requests (default: {CONCURRENCY})")
    ap.add_argument("--dry-run", action="store_true",
                    help="Probe first 50 IDs only, write no files")
    args = ap.parse_args()

    # Build ID list
    if args.verify_short:
        ids   = load_short_ids()
        label = f"short-range re-verify  ({len(ids):,} known IDs)"
    else:
        cp           = load_checkpoint() if args.resume else {}
        resume_after = cp.get("last_id", args.start - 1)
        ids          = [i for i in range(args.start, args.end) if i > resume_after]
        label        = (f"long-range  {ids[0] if ids else '?':,} → {args.end - 1:,}")
        if args.resume and cp:
            prev_found = cp.get("found", 0)
            print(f"  Resuming after {resume_after:,}  ({prev_found:,} found so far)")

    if args.dry_run:
        ids = ids[:50]

    if not ids:
        print("Nothing to scan.")
        return

    stamp    = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode_tag = "short" if args.verify_short else "long"
    csv_path = DATA_DIR / f"tubi_scan_{mode_tag}_{stamp}.csv"

    print(f"\nTubi scanner  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  Range      : {label}")
    print(f"  IDs        : {len(ids):,}")
    print(f"  Concurrent : {args.concurrent}")
    if not args.dry_run:
        print(f"  Output     : {csv_path.name}")
    if args.dry_run:
        print("  [dry-run: no files written]")
    print()

    stats = asyncio.run(scan(ids, csv_path, args.concurrent, args.dry_run))

    print(f"  Done.  found {stats['found']:,}"
          f"  |  miss {stats['not_found']:,}"
          f"  |  errors {stats['errors']}")

    if not args.dry_run:
        if stats["found"]:
            print(f"  Output: {csv_path}")
        save_checkpoint({**stats, "csv": str(csv_path), "complete": True})
    print()


if __name__ == "__main__":
    main()
