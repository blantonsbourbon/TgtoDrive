"""Search HDHive by title → pick 115 resource → unlock → save.

Designed for agent invocation (Hermes): stable JSON on stdout, logs on stderr.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional
from urllib.parse import quote

import os

import requests

from hdhive_unlock import (
    HDHiveBrowser,
    UnlockResult,
    dismiss_announcement,
    wait_for_site_ready,
)
from pan115 import Pan115Client, parse_115_share

logger = logging.getLogger(__name__)

POINTS_IN_TEXT = re.compile(r"(\d+)\s*积分")
FREE_MARKERS = ("免费", "free")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


@dataclass
class SearchHit:
    title: str
    url: str
    media_type: str  # movie | tv | unknown
    year: Optional[int] = None
    tmdb_id: Optional[int] = None
    score: float = 0.0


@dataclass
class ResourceHit:
    resource_url: str
    provider: str  # 115 | other
    points: Optional[int]
    is_free: bool
    label: str = ""
    score: float = 0.0


@dataclass
class WantResult:
    ok: bool
    query: str
    movie: Optional[dict] = None
    resource: Optional[dict] = None
    unlock: Optional[dict] = None
    save: Optional[dict] = None
    candidates: list[dict] = field(default_factory=list)
    resources: list[dict] = field(default_factory=list)
    error: str = ""
    error_code: str = ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _score_title(query: str, title: str) -> float:
    q, t = _norm(query), _norm(title)
    if not q or not t:
        return 0.0
    if q == t:
        return 100.0
    if q in t or t in q:
        return 80.0 + min(len(q), 20) / 20.0
    # token overlap
    qt, tt = set(q.split()), set(re.split(r"[\s:：\-_/]+", t))
    if not qt:
        return 0.0
    overlap = len(qt & tt) / len(qt)
    return overlap * 60.0


def _parse_year(*texts: str) -> Optional[int]:
    for text in texts:
        m = YEAR_RE.search(text or "")
        if m:
            return int(m.group(0))
    return None


def search_via_tmdb_api(
    query: str,
    *,
    api_key: str,
    base_url: str = "https://hdhive.com",
    limit: int = 8,
) -> list[SearchHit]:
    """Optional fast path when TMDB_API_KEY is set (no HDHive search UI needed)."""
    if not api_key:
        return []
    try:
        r = requests.get(
            "https://api.themoviedb.org/3/search/multi",
            params={
                "api_key": api_key,
                "query": query,
                "language": "zh-CN",
                "include_adult": "false",
                "page": 1,
            },
            timeout=20,
        )
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
    except Exception as e:
        logger.warning("TMDB API search failed: %s", e)
        return []

    hits: list[SearchHit] = []
    for item in results:
        media = (item.get("media_type") or "").lower()
        if media not in ("movie", "tv"):
            continue
        tid = item.get("id")
        title = (item.get("title") or item.get("name") or "").strip()
        if not tid or not title:
            continue
        year = _parse_year(item.get("release_date") or item.get("first_air_date") or "")
        hits.append(
            SearchHit(
                title=title,
                url=f"{base_url.rstrip('/')}/tmdb/{media}/{tid}",
                media_type=media,
                year=year,
                tmdb_id=int(tid),
                score=_score_title(query, title) + (5 if media == "movie" else 0),
            )
        )
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]


def _goto(page, url: str, attempts: int = 3) -> None:
    last = None
    for i in range(attempts):
        try:
            # commit is more reliable than domcontentloaded on this SPA
            page.goto(url, wait_until="commit", timeout=45000)
            return
        except Exception as e:
            last = e
            logger.warning("goto failed (%s/%s) %s: %s", i + 1, attempts, url, e)
            page.wait_for_timeout(1500)
    raise last  # type: ignore[misc]


def search_titles(browser: HDHiveBrowser, query: str, limit: int = 8) -> list[SearchHit]:
    """Use HDHive search page (TMDB-backed) and collect movie/tv hits."""
    page = browser.new_page()
    hits: list[SearchHit] = []
    api_payloads: list[dict] = []

    def on_response(resp):
        try:
            url = resp.url
            if resp.status != 200:
                return
            if "/go-api/proxy/tmdb/3/search/" not in url and not re.search(
                r"/search/(multi|movie|tv)", url
            ):
                return
            data = resp.json()
            api_payloads.append({"url": url, "data": data})
            logger.info("captured search API: %s", url[:120])
        except Exception:
            return

    page.on("response", on_response)
    try:
        q = query.strip()
        # Warm up secure session on homepage first (search page alone often
        # sticks on 安全检测 when cold-starting).
        logger.info("search warmup: %s/", browser.base_url)
        _goto(page, f"{browser.base_url}/")
        wait_for_site_ready(page, timeout_s=50.0)
        dismiss_announcement(page, max_wait_s=14.0)

        search_url = f"{browser.base_url}/search?q={quote(q)}"
        logger.info("search: %s", search_url)
        _goto(page, search_url)
        wait_for_site_ready(page, timeout_s=40.0)
        dismiss_announcement(page, max_wait_s=10.0)

        # Ensure query is applied in the SPA search box if present
        try:
            box = page.locator(
                "input[type='search'], input[placeholder*='搜索'], "
                "input[placeholder*='Search'], input:not([type='hidden'])"
            )
            if box.count() > 0:
                box.first.fill(q, timeout=3000)
                page.keyboard.press("Enter")
                page.wait_for_timeout(1500)
        except Exception:
            pass

        # Wait for results to appear
        deadline = time.time() + 20
        while time.time() < deadline:
            # prefer network JSON
            for pack in api_payloads:
                data = pack.get("data") or {}
                # unwrap common envelopes
                body = data.get("data") if isinstance(data, dict) and "data" in data else data
                results = []
                if isinstance(body, dict):
                    results = body.get("results") or body.get("items") or []
                    if not results and body.get("id"):
                        results = [body]
                if isinstance(results, list) and results:
                    break
            else:
                results = []
            if results:
                break
            # DOM fallback
            links = page.evaluate(
                """() => [...document.querySelectorAll('a[href]')]
                    .map(a => ({href:a.href, text:(a.innerText||a.getAttribute('aria-label')||'').replace(/\\s+/g,' ').trim()}))
                    .filter(x => /\\/tmdb\\/(movie|tv)\\/\\d+/.test(x.href) || /\\/(movie|tv)\\/[\\w-]{8,}/.test(x.href))
                """
            )
            if links:
                break
            page.wait_for_timeout(500)

        # Build hits from API first
        for pack in api_payloads:
            data = pack.get("data") or {}
            body = data.get("data") if isinstance(data, dict) and "data" in data else data
            results = []
            if isinstance(body, dict):
                results = body.get("results") or []
                if not results and body.get("id"):
                    results = [body]
            if not isinstance(results, list):
                continue
            for item in results:
                if not isinstance(item, dict):
                    continue
                media = (item.get("media_type") or "").lower()
                if media not in ("movie", "tv"):
                    # infer
                    if item.get("title"):
                        media = "movie"
                    elif item.get("name"):
                        media = "tv"
                    else:
                        continue
                if media not in ("movie", "tv"):
                    continue
                tid = item.get("id")
                title = (item.get("title") or item.get("name") or "").strip()
                if not tid or not title:
                    continue
                year = _parse_year(item.get("release_date") or item.get("first_air_date") or "")
                url = f"{browser.base_url}/tmdb/{media}/{tid}"
                hits.append(
                    SearchHit(
                        title=title,
                        url=url,
                        media_type=media,
                        year=year,
                        tmdb_id=int(tid),
                        score=_score_title(query, title),
                    )
                )

        # DOM fallback / merge
        if not hits:
            links = page.evaluate(
                """() => [...document.querySelectorAll('a[href]')]
                    .map(a => ({href:a.href, text:(a.innerText||'').replace(/\\s+/g,' ').trim().slice(0,120)}))
                    .filter(x => /\\/tmdb\\/(movie|tv)\\/\\d+/.test(x.href) || /\\/(movie|tv)\\/[a-f0-9-]{16,}/i.test(x.href))
                """
            )
            seen = set()
            for item in links or []:
                href = item.get("href") or ""
                if href in seen:
                    continue
                seen.add(href)
                text = item.get("text") or ""
                media = "movie"
                tid = None
                m = re.search(r"/tmdb/(movie|tv)/(\d+)", href)
                if m:
                    media, tid = m.group(1), int(m.group(2))
                elif "/tv/" in href:
                    media = "tv"
                title = text.split("  ")[0].strip() or text[:80] or query
                hits.append(
                    SearchHit(
                        title=title or query,
                        url=href,
                        media_type=media,
                        year=_parse_year(text),
                        tmdb_id=tid,
                        score=_score_title(query, title or query),
                    )
                )

        # prefer movies when query looks like a film; boost exact-ish titles
        for h in hits:
            if h.media_type == "movie":
                h.score += 5
        hits.sort(key=lambda h: h.score, reverse=True)

        # dedupe by url
        uniq: list[SearchHit] = []
        seen_u = set()
        for h in hits:
            if h.url in seen_u:
                continue
            seen_u.add(h.url)
            uniq.append(h)
        return uniq[:limit]
    finally:
        page.close()


def list_115_resources(browser: HDHiveBrowser, movie_url: str) -> list[ResourceHit]:
    """Open movie/tmdb detail page and list 115 resources."""
    page = browser.new_page()
    resources: list[ResourceHit] = []
    try:
        # ensure session is warm
        _goto(page, f"{browser.base_url}/")
        wait_for_site_ready(page, timeout_s=40.0)
        dismiss_announcement(page, max_wait_s=8.0)

        logger.info("open detail: %s", movie_url)
        _goto(page, movie_url)
        wait_for_site_ready(page, timeout_s=50.0)
        dismiss_announcement(page, max_wait_s=12.0)
        page.wait_for_timeout(1500)

        # Prefer 115 tab if present
        try:
            tab = page.get_by_role("button", name=re.compile(r"115"))
            if tab.count() > 0:
                tab.first.click(timeout=3000)
                page.wait_for_timeout(1200)
        except Exception:
            try:
                page.get_by_text(re.compile(r"115网盘|115")).first.click(timeout=2000)
                page.wait_for_timeout(1000)
            except Exception:
                pass

        # Wait for resource links
        deadline = time.time() + 15
        while time.time() < deadline:
            n = page.evaluate("() => document.querySelectorAll('a[href*=\"/resource/\"]').length")
            if n and int(n) > 0:
                break
            page.wait_for_timeout(500)

        rows = page.evaluate(
            """() => {
              const out = [];
              const links = [...document.querySelectorAll('a[href*="/resource/"]')];
              for (const a of links) {
                const href = a.href;
                // climb for card text
                let el = a;
                let text = '';
                for (let i=0; i<5 && el; i++) {
                  text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                  if (text.length > 20) break;
                  el = el.parentElement;
                }
                out.push({href, text: text.slice(0, 200)});
              }
              return out;
            }"""
        )

        for row in rows or []:
            href = row.get("href") or ""
            text = row.get("text") or ""
            if "/resource/" not in href:
                continue
            provider = "115" if "/resource/115/" in href or "115" in text else "other"
            is_free = any(m in text.lower() or m in text for m in FREE_MARKERS)
            pts = None
            pm = POINTS_IN_TEXT.search(text)
            if pm:
                pts = int(pm.group(1))
            elif is_free:
                pts = 0
            score = 0.0
            if provider == "115":
                score += 50
            if is_free or pts == 0:
                score += 30
            elif pts is not None:
                score += max(0, 20 - pts)
            if re.search(r"4K|2160|BluRay|REMUX|WEB", text, re.I):
                score += 5
            resources.append(
                ResourceHit(
                    resource_url=href,
                    provider=provider,
                    points=pts,
                    is_free=is_free or pts == 0,
                    label=text[:160],
                    score=score,
                )
            )

        # only 115 by default ranking first
        resources.sort(key=lambda r: (r.provider != "115", -r.score, r.points if r.points is not None else 999))
        return resources
    finally:
        page.close()


def pick_resource(
    resources: list[ResourceHit],
    max_points: int = 4,
    provider: str = "115",
) -> Optional[ResourceHit]:
    pool = [r for r in resources if (provider == "any" or r.provider == provider)]
    if not pool:
        pool = list(resources)
    eligible = []
    for r in pool:
        pts = 0 if r.is_free else (r.points if r.points is not None else 999)
        if pts <= max_points:
            eligible.append(r)
    if not eligible:
        return None
    eligible.sort(key=lambda r: (r.points if r.points is not None else 999, -r.score))
    return eligible[0]


def want_movie(
    query: str,
    *,
    hdhive_cookie: str,
    cookies_115: str = "",
    pid: str | int = "0",
    max_points: int = 4,
    headless: bool = True,
    save: bool = True,
    movie_index: int = 0,
    base_url: str = "https://hdhive.com",
    dry_run: bool = False,
    tmdb_id: Optional[int] = None,
    media_type: str = "movie",
) -> WantResult:
    """End-to-end: search → pick resource → unlock → optional 115 save."""
    query = (query or "").strip()
    if not query and not tmdb_id:
        return WantResult(ok=False, query=query, error="empty query", error_code="empty_query")

    try:
        with HDHiveBrowser(
            hdhive_cookie,
            base_url=base_url,
            headless=headless,
            max_points=max_points,
        ) as browser:
            candidates: list[SearchHit] = []
            if tmdb_id:
                mt = media_type if media_type in ("movie", "tv") else "movie"
                candidates = [
                    SearchHit(
                        title=query or f"{mt}:{tmdb_id}",
                        url=f"{base_url.rstrip('/')}/tmdb/{mt}/{tmdb_id}",
                        media_type=mt,
                        tmdb_id=int(tmdb_id),
                        score=100.0,
                    )
                ]
                logger.info("using explicit tmdb id %s (%s)", tmdb_id, mt)
            else:
                # Prefer TMDB API when key is available (stable); else HDHive UI search.
                tmdb_key = (
                    os.getenv("TMDB_API_KEY", "").strip()
                    or os.getenv("ENV_TMDB_API_KEY", "").strip()
                )
                candidates = search_via_tmdb_api(
                    query, api_key=tmdb_key, base_url=base_url, limit=10
                )
                if candidates:
                    logger.info("search via TMDB API: %s hits", len(candidates))
                else:
                    candidates = search_titles(browser, query, limit=10)
            if not candidates:
                return WantResult(
                    ok=False,
                    query=query,
                    error=f"no HDHive search results for {query!r}",
                    error_code="no_search_results",
                )

            if movie_index < 0 or movie_index >= len(candidates):
                movie_index = 0
            movie = candidates[movie_index]
            logger.info("picked movie: %s (%s) score=%.1f", movie.title, movie.url, movie.score)

            resources = list_115_resources(browser, movie.url)
            if not resources:
                return WantResult(
                    ok=False,
                    query=query,
                    movie=asdict(movie),
                    candidates=[asdict(c) for c in candidates],
                    error="no resources found on movie page",
                    error_code="no_resources",
                )

            chosen = pick_resource(resources, max_points=max_points, provider="115")
            if not chosen:
                return WantResult(
                    ok=False,
                    query=query,
                    movie=asdict(movie),
                    candidates=[asdict(c) for c in candidates],
                    resources=[asdict(r) for r in resources],
                    error=f"no 115 resource within max_points={max_points}",
                    error_code="points_exceeded",
                )

            if dry_run:
                return WantResult(
                    ok=True,
                    query=query,
                    movie=asdict(movie),
                    resource=asdict(chosen),
                    candidates=[asdict(c) for c in candidates],
                    resources=[asdict(r) for r in resources],
                    error="",
                    error_code="dry_run",
                )

            unlock: UnlockResult = browser.unlock_resource(chosen.resource_url)
            unlock_dict = {
                "resource_url": unlock.resource_url,
                "share_urls": unlock.share_urls,
                "points_cost": unlock.points_cost,
                "skipped": unlock.skipped,
                "skip_reason": unlock.skip_reason,
                "already_unlocked": unlock.already_unlocked,
            }
            if unlock.skipped:
                return WantResult(
                    ok=False,
                    query=query,
                    movie=asdict(movie),
                    resource=asdict(chosen),
                    unlock=unlock_dict,
                    candidates=[asdict(c) for c in candidates],
                    resources=[asdict(r) for r in resources],
                    error=unlock.skip_reason or "unlock skipped",
                    error_code="unlock_skipped",
                )
            if not unlock.share_urls:
                return WantResult(
                    ok=False,
                    query=query,
                    movie=asdict(movie),
                    resource=asdict(chosen),
                    unlock=unlock_dict,
                    candidates=[asdict(c) for c in candidates],
                    resources=[asdict(r) for r in resources],
                    error="unlock produced no 115 share urls",
                    error_code="unlock_failed",
                )

            share_url = None
            for u in unlock.share_urls:
                if parse_115_share(u):
                    share_url = u
                    break
            share_url = share_url or unlock.share_urls[0]

            save_info: dict[str, Any] = {
                "attempted": False,
                "saved": False,
                "share_url": share_url,
                "pid": str(pid),
            }
            if save:
                if not cookies_115:
                    return WantResult(
                        ok=False,
                        query=query,
                        movie=asdict(movie),
                        resource=asdict(chosen),
                        unlock=unlock_dict,
                        save=save_info,
                        candidates=[asdict(c) for c in candidates],
                        resources=[asdict(r) for r in resources],
                        error="ENV_115_COOKIES missing; unlock ok but cannot save",
                        error_code="missing_115_cookie",
                    )
                client = Pan115Client(cookies_115)
                ok = client.save_share_link(share_url, pid=pid)
                save_info = {
                    "attempted": True,
                    "saved": bool(ok),
                    "share_url": share_url,
                    "pid": str(pid),
                }
                if not ok:
                    return WantResult(
                        ok=False,
                        query=query,
                        movie=asdict(movie),
                        resource=asdict(chosen),
                        unlock=unlock_dict,
                        save=save_info,
                        candidates=[asdict(c) for c in candidates],
                        resources=[asdict(r) for r in resources],
                        error="115 save failed",
                        error_code="save_failed",
                    )

            return WantResult(
                ok=True,
                query=query,
                movie=asdict(movie),
                resource=asdict(chosen),
                unlock=unlock_dict,
                save=save_info,
                candidates=[asdict(c) for c in candidates],
                resources=[asdict(r) for r in resources],
            )
    except Exception as e:
        logger.exception("want_movie failed")
        return WantResult(
            ok=False,
            query=query,
            error=str(e),
            error_code="exception",
        )
