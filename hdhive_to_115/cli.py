#!/usr/bin/env python3
"""CLI: HDHive unlock (Playwright cookie session) → optional 115 save."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from hdhive_unlock import HDHiveBrowser
from hdhive_want import want_movie
from pan115 import Pan115Client, parse_115_share

ROOT = Path(__file__).resolve().parent


def _load_env() -> None:
    # project local secrets first
    load_dotenv(ROOT / ".env.local", override=True)
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(override=False)


def _setup_log(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    # Logs → stderr so agents can parse pure JSON on stdout
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def cmd_check(args: argparse.Namespace) -> int:
    cookie = os.getenv("HDHIVE_COOKIE", "")
    base = os.getenv("HDHIVE_BASE_URL", "https://hdhive.com")
    headless = os.getenv("HDHIVE_HEADLESS", "1") != "0"
    with HDHiveBrowser(cookie, base_url=base, headless=headless) as browser:
        info = browser.verify_session()
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0 if info.get("ok") else 2


def cmd_unlock(args: argparse.Namespace) -> int:
    cookie = os.getenv("HDHIVE_COOKIE", "")
    base = os.getenv("HDHIVE_BASE_URL", "https://hdhive.com")
    headless = os.getenv("HDHIVE_HEADLESS", "1") != "0"
    max_points = int(os.getenv("HDHIVE_MAX_POINTS", str(args.max_points)))
    if args.max_points is not None and args.max_points != 4:
        max_points = args.max_points
    # argparse default 4 may override env — prefer explicit flag, else env
    if "--max-points" in sys.argv:
        max_points = args.max_points
    else:
        max_points = int(os.getenv("HDHIVE_MAX_POINTS", "4"))

    debug_dir = str(ROOT / "debug") if args.debug else None
    with HDHiveBrowser(
        cookie,
        base_url=base,
        headless=headless and not args.headed,
        max_points=max_points,
    ) as browser:
        result = browser.unlock_resource(args.url, debug_dir=debug_dir)

    payload = {
        "resource_url": result.resource_url,
        "share_urls": result.share_urls,
        "points_cost": result.points_cost,
        "skipped": result.skipped,
        "skip_reason": result.skip_reason,
        "already_unlocked": result.already_unlocked,
        "page_title": result.page_title,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if result.skipped:
        return 3
    if not result.share_urls:
        return 1
    return 0


def cmd_save115(args: argparse.Namespace) -> int:
    cookies = os.getenv("ENV_115_COOKIES", "")
    pid = args.pid if args.pid is not None else os.getenv("HDHIVE_115_PID", "0")
    delay = float(os.getenv("TRANSFER_DELAY", "2"))
    client = Pan115Client(cookies)
    ok = client.save_share_link(args.url, pid=pid, delay=delay)
    print(json.dumps({"ok": ok, "share_url": args.url, "pid": str(pid)}, ensure_ascii=False))
    return 0 if ok else 1


def cmd_run(args: argparse.Namespace) -> int:
    """Unlock then save first 115 link."""
    cookie = os.getenv("HDHIVE_COOKIE", "")
    base = os.getenv("HDHIVE_BASE_URL", "https://hdhive.com")
    headless = os.getenv("HDHIVE_HEADLESS", "1") != "0"
    if "--max-points" in sys.argv:
        max_points = args.max_points
    else:
        max_points = int(os.getenv("HDHIVE_MAX_POINTS", "4"))
    pid = args.pid if args.pid is not None else os.getenv("HDHIVE_115_PID", "0")
    delay = float(os.getenv("TRANSFER_DELAY", "2"))
    cookies_115 = os.getenv("ENV_115_COOKIES", "")
    if not cookies_115:
        logging.error("ENV_115_COOKIES is required for `run` (or use `unlock` only)")
        return 2

    debug_dir = str(ROOT / "debug") if args.debug else None
    with HDHiveBrowser(
        cookie,
        base_url=base,
        headless=headless and not args.headed,
        max_points=max_points,
    ) as browser:
        result = browser.unlock_resource(args.url, debug_dir=debug_dir)

    if result.skipped:
        print(json.dumps({"skipped": True, "reason": result.skip_reason}, ensure_ascii=False, indent=2))
        return 3
    if not result.share_urls:
        print(json.dumps({"error": "no share urls"}, ensure_ascii=False))
        return 1

    # pick first valid 115 link
    share_url = None
    for u in result.share_urls:
        if parse_115_share(u):
            share_url = u
            break
    if not share_url:
        share_url = result.share_urls[0]

    client = Pan115Client(cookies_115)
    ok = client.save_share_link(share_url, pid=pid, delay=delay)
    out = {
        "resource_url": result.resource_url,
        "share_url": share_url,
        "points_cost": result.points_cost,
        "already_unlocked": result.already_unlocked,
        "saved": ok,
        "pid": str(pid),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="HDHive cookie unlock → 115 save (standalone, no TgtoDrive)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="Verify HDHive cookie session in headless browser")
    c.set_defaults(func=cmd_check)

    u = sub.add_parser("unlock", help="Unlock resource and print 115 share URLs")
    u.add_argument("url", help="HDHive resource URL or slug")
    u.add_argument("--max-points", type=int, default=4, help="Skip if unlock cost exceeds this")
    u.add_argument("--headed", action="store_true", help="Show browser window")
    u.add_argument("--debug", action="store_true", help="Dump screenshot/HTML on failure")
    u.set_defaults(func=cmd_unlock)

    s = sub.add_parser("save115", help="Save a 115 share URL to target folder")
    s.add_argument("url", help="115 share URL with password")
    s.add_argument("--pid", default=None, help="Target 115 folder CID")
    s.set_defaults(func=cmd_save115)

    r = sub.add_parser("run", help="Unlock HDHive resource then save to 115")
    r.add_argument("url", help="HDHive resource URL or slug")
    r.add_argument("--max-points", type=int, default=4)
    r.add_argument("--pid", default=None, help="Target 115 folder CID")
    r.add_argument("--headed", action="store_true")
    r.add_argument("--debug", action="store_true")
    r.set_defaults(func=cmd_run)

    w = sub.add_parser(
        "want",
        help="Agent entry: search HDHive by title, pick 115 resource, unlock, save",
    )
    w.add_argument("query", help='Movie/TV title, e.g. "Fury" or "狂怒"')
    w.add_argument("--max-points", type=int, default=None, help="Max unlock points (default env/4)")
    w.add_argument("--pid", default=None, help="Target 115 folder CID")
    w.add_argument("--index", type=int, default=0, help="Which search hit to use (0=best)")
    w.add_argument(
        "--tmdb-id",
        type=int,
        default=None,
        help="Skip title search; open HDHive /tmdb/{movie|tv}/ID directly",
    )
    w.add_argument(
        "--media-type",
        choices=["movie", "tv"],
        default="movie",
        help="Used with --tmdb-id (default movie)",
    )
    w.add_argument("--no-save", action="store_true", help="Only search+unlock, do not save to 115")
    w.add_argument("--dry-run", action="store_true", help="Search+pick only, no unlock/save")
    w.add_argument("--headed", action="store_true", help="Show browser window")
    w.set_defaults(func=cmd_want)

    return p


def cmd_want(args: argparse.Namespace) -> int:
    """Hermes-friendly: find movie by name → unlock → 115 save. JSON on stdout."""
    if args.max_points is not None:
        max_points = args.max_points
    else:
        max_points = int(os.getenv("HDHIVE_MAX_POINTS", "4"))
    pid = args.pid if args.pid is not None else os.getenv("HDHIVE_115_PID", "0")
    headless = os.getenv("HDHIVE_HEADLESS", "1") != "0" and not args.headed
    result = want_movie(
        args.query,
        hdhive_cookie=os.getenv("HDHIVE_COOKIE", ""),
        cookies_115=os.getenv("ENV_115_COOKIES", ""),
        pid=pid,
        max_points=max_points,
        headless=headless,
        save=not args.no_save and not args.dry_run,
        movie_index=args.index,
        base_url=os.getenv("HDHIVE_BASE_URL", "https://hdhive.com"),
        dry_run=args.dry_run,
        tmdb_id=args.tmdb_id,
        media_type=args.media_type,
    )
    # compact agent-friendly payload
    payload = {
        "ok": result.ok,
        "query": result.query,
        "movie": result.movie,
        "resource": result.resource,
        "unlock": result.unlock,
        "save": result.save,
        "error": result.error,
        "error_code": result.error_code,
        "candidates": result.candidates[:5],
        "resources": result.resources[:8],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if result.ok:
        return 0
    code = result.error_code
    if code in ("missing_115_cookie",):
        return 2
    if code in ("unlock_skipped", "points_exceeded"):
        return 3
    if code in ("no_search_results", "no_resources"):
        return 4
    return 1


def main(argv: list[str] | None = None) -> int:
    _load_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_log(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
