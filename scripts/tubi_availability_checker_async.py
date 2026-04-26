#!/usr/bin/env python3
"""
Tubi Availability Checker (Async)
Checks movies concurrently to verify they're still available.

Usage:
  python3 scripts/tubi_availability_checker_async.py                        # 50 movies, 5 concurrent
  python3 scripts/tubi_availability_checker_async.py --all                  # all unchecked/stale movies
  python3 scripts/tubi_availability_checker_async.py --quick                # 10 movies, 3 concurrent
  python3 scripts/tubi_availability_checker_async.py --count 100 --concurrent 10
  python3 scripts/tubi_availability_checker_async.py --stats
"""

import asyncio
import aiohttp
import sqlite3
import sys
import os
import random
import argparse
from datetime import datetime
from typing import List, Tuple

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tubi.db")
LOG_PATH = os.path.join(os.path.dirname(DB_PATH), "availability_async.log")
CHECK_SIZE = 50000  # 50KB
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
MAX_CONCURRENT = 50

UNAVAILABLE_STRINGS = [
    'content unavailable',
    'not currently available',
    "We're Sorry",
]


async def check_movie_availability(session: aiohttp.ClientSession, url: str) -> Tuple[bool, str]:
    """Asynchronously check if a movie URL shows a playable movie."""
    try:
        headers = {
            'User-Agent': USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        }

        async with session.get(url, headers=headers) as response:
            if response.status == 404:
                return False, "404 not found"
            if not response.ok:
                return False, f"http {response.status}"

            content = b''
            async for chunk in response.content.iter_chunked(4096):
                content += chunk
                if len(content) >= CHECK_SIZE:
                    break

            html = content.decode('utf-8', errors='ignore')

            for marker in UNAVAILABLE_STRINGS:
                if marker in html:
                    return False, "unavailable"

            if 'tubi' in html.lower():
                return True, "available"

            return False, "unknown"

    except asyncio.TimeoutError:
        return False, "error: timeout"
    except aiohttp.ClientError as e:
        return False, f"error: connection - {str(e)}"
    except Exception as e:
        return False, f"error: {str(e)}"


def count_unchecked() -> int:
    """Count movies that are unchecked or stale (>7 days)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) FROM movies
        WHERE last_checked IS NULL
           OR datetime(last_checked) < datetime('now', '-7 days')
    """)
    total = cursor.fetchone()[0]
    conn.close()
    return total


def get_movies_to_check(count: int, strategy: str = "random") -> List[Tuple]:
    """Pull movie IDs and URLs from the database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    queries = {
        "random": """
            SELECT id, url, last_checked, check_count, fail_count
            FROM movies
            WHERE last_checked IS NULL
               OR datetime(last_checked) < datetime('now', '-7 days')
            ORDER BY random()
            LIMIT ?
        """,
        "suspect": """
            SELECT id, url, last_checked, check_count, fail_count
            FROM movies
            WHERE fail_count > 0
              AND (last_checked IS NULL OR datetime(last_checked) < datetime('now', '-1 days'))
            ORDER BY fail_count DESC, random()
            LIMIT ?
        """,
        "fresh": """
            SELECT id, url, last_checked, check_count, fail_count
            FROM movies
            WHERE last_checked IS NULL
            ORDER BY id DESC
            LIMIT ?
        """,
    }

    cursor.execute(queries[strategy], (count,))
    rows = [(row['id'], row['url']) for row in cursor.fetchall()]
    conn.close()
    return rows


def bulk_update(results: List[Tuple]):
    """Write a batch of results to the database and log in one pass."""
    if not results:
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.now().isoformat()

    available_ids   = [(now, mid) for mid, ok, _ in results if ok]
    unavailable_ids = [(now, mid) for mid, ok, _ in results if not ok]

    if available_ids:
        cursor.executemany("""
            UPDATE movies
            SET last_checked = ?, check_count = check_count + 1, available = 1
            WHERE id = ?
        """, available_ids)

    if unavailable_ids:
        cursor.executemany("""
            UPDATE movies
            SET last_checked = ?, check_count = check_count + 1,
                fail_count = fail_count + 1, available = 0
            WHERE id = ?
        """, unavailable_ids)

    conn.commit()
    conn.close()

    with open(LOG_PATH, 'a') as f:
        for movie_id, ok, message in results:
            icon = "✅" if ok else "❌"
            f.write(f"{now} | {icon} | {movie_id} | {message}\n")


async def process_movie(
    session: aiohttp.ClientSession,
    movie: Tuple,
    semaphore: asyncio.Semaphore,
) -> Tuple[int, bool, str]:
    """Check a single movie with concurrency control."""
    movie_id, url = movie[0], movie[1]
    async with semaphore:
        available, message = await check_movie_availability(session, url)
        return movie_id, available, message


async def async_run_check(count: int = 50, concurrent: int = MAX_CONCURRENT):
    """Main async check loop."""
    print(f"🎬 Checking {count} movies ({concurrent} concurrent)...")
    print("-" * 50)

    # Mix strategies, deduplicate by ID
    suspect = get_movies_to_check(count // 3, "suspect")
    fresh   = get_movies_to_check(count // 3, "fresh")
    rest    = get_movies_to_check(count - len(suspect) - len(fresh), "random")
    movies  = list({m[0]: m for m in suspect + fresh + rest}.values())
    random.shuffle(movies)

    if not movies:
        print("No movies to check (all recently checked).")
        return

    stats = {"available": 0, "unavailable": 0, "errors": 0}
    semaphore = asyncio.Semaphore(concurrent)
    pending_writes = []
    completed = 0

    timeout = aiohttp.ClientTimeout(total=30)
    connector = aiohttp.TCPConnector(limit=concurrent, ttl_dns_cache=300)

    start_time = datetime.now()

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        tasks = [process_movie(session, movie, semaphore) for movie in movies]

        for coro in asyncio.as_completed(tasks):
            try:
                movie_id, ok, message = await coro
            except Exception as e:
                print(f"⚠️  Task error: {e}")
                stats["errors"] += 1
                completed += 1
                continue

            completed += 1
            pending_writes.append((movie_id, ok, message))

            icon = "✅" if ok else "❌"
            print(f"[{completed:>5}/{len(movies)}] {icon} {movie_id} — {message}")

            if ok:
                stats["available"] += 1
            else:
                stats["unavailable"] += 1

            # Flush to DB every 100 results
            if len(pending_writes) >= 100:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, bulk_update, pending_writes.copy())
                pending_writes.clear()

    # Final flush
    if pending_writes:
        bulk_update(pending_writes)

    elapsed = (datetime.now() - start_time).total_seconds()
    rate = completed / elapsed if elapsed > 0 else 0

    print("-" * 50)
    print(f"📊 {stats['available']} available  |  {stats['unavailable']} unavailable  |  {stats['errors']} errors")
    print(f"⏱️  {elapsed:.1f}s  ({rate:.1f} checks/sec)")


def show_stats():
    """Display current availability stats."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM movies")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM movies WHERE available = 1")
    available = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM movies WHERE available = 0")
    unavailable = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM movies WHERE fail_count > 0")
    ever_failed = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(*) FROM movies
        WHERE last_checked IS NULL
           OR datetime(last_checked) < datetime('now', '-30 days')
    """)
    stale = cursor.fetchone()[0]

    cursor.execute("SELECT AVG(fail_count) FROM movies WHERE fail_count > 0")
    avg_fails = cursor.fetchone()[0] or 0

    conn.close()

    pct = lambda n: f"{n / total * 100:.1f}%" if total else "—"
    print(f"\n📊 Database Stats:")
    print(f"   Total movies   : {total}")
    print(f"   ✅ Available   : {available} ({pct(available)})")
    print(f"   ❌ Unavailable : {unavailable} ({pct(unavailable)})")
    print(f"   📋 Ever failed : {ever_failed}")
    print(f"   🔄 Stale (>30d): {stale} ({pct(stale)})")
    print(f"   📈 Avg failures: {avg_fails:.1f}")


def main():
    parser = argparse.ArgumentParser(description="Check Tubi movie availability (Async)")
    parser.add_argument("--count", "-c", type=int, default=50,
                        help="Number of movies to check (default: 50)")
    parser.add_argument("--concurrent", "-n", type=int, default=MAX_CONCURRENT,
                        help=f"Concurrent requests (default: {MAX_CONCURRENT})")
    parser.add_argument("--all", "-a", action="store_true",
                        help="Check all unchecked or stale movies")
    parser.add_argument("--quick", "-q", action="store_true",
                        help="Quick mode: 10 movies, 3 concurrent")
    parser.add_argument("--stats", "-s", action="store_true",
                        help="Show database stats and exit")
    args = parser.parse_args()

    if args.stats:
        show_stats()
        sys.exit(0)

    if args.quick:
        asyncio.run(async_run_check(count=10, concurrent=3))
    elif args.all:
        total = count_unchecked()
        print(f"📋 {total} movies to check...")
        asyncio.run(async_run_check(count=total, concurrent=args.concurrent))
    else:
        asyncio.run(async_run_check(count=args.count, concurrent=args.concurrent))


if __name__ == "__main__":
    main()