"""Unlock HDHive resources via Playwright + browser session cookies."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, TimeoutError as PwTimeout
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

try:
    from playwright_stealth import Stealth

    _STEALTH = Stealth()
except Exception:  # pragma: no cover
    _STEALTH = None

# 115 share links that appear after unlock
SHARE_115_RE = re.compile(
    r"https?://(?:115|115cdn|anxia)\.com/s/[A-Za-z0-9]+"
    r"(?:\?[^\s\"'<>]*password=[A-Za-z0-9]{4}|"
    r"#[A-Za-z0-9]{4})?",
    re.I,
)
NEARBY_PWD_RE = re.compile(
    r"(?:密码|提取码|访问码)\s*[:：]?\s*([A-Za-z0-9]{4})",
    re.I,
)
POINTS_RE = re.compile(r"(?:需要使用|消耗|需)\s*(\d+)\s*积分|(\d+)\s*积分")
UNLOCK_BTN_TEXTS = ("解锁", "积分解锁", "确认解锁", "立即解锁", "使用积分解锁")

# Homepage announcement often has a ~10s countdown before dismiss is allowed.
# Live UI uses forms like: "我知道了 (10S)" / "我知道了 (9S)" (S = seconds).
ANNOUNCE_DISMISS_RE = re.compile(
    r"(知道了|我知道了|已阅读|关闭|确认|同意|继续)(?:\s*[（(]?\s*\d+\s*[Ss秒]\s*[)）]?)?"
)
ANNOUNCE_COUNTDOWN_RE = re.compile(r"(\d+)\s*[Ss秒]")
LOADING_HINTS = (
    "LOADING",
    "请稍后",
    "正在检测浏览器安全能力",
    "清理缓存",
    "正在清理缓存",
    "应用资源版本不一致",
)


@dataclass
class UnlockResult:
    resource_url: str
    share_urls: list[str]
    points_cost: Optional[int]
    skipped: bool = False
    skip_reason: str = ""
    already_unlocked: bool = False
    page_title: str = ""


def parse_cookie_header(cookie_header: str, domain: str = ".hdhive.com") -> list[dict]:
    """Turn a Cookie header string into Playwright cookie dicts."""
    cookies = []
    http_only_names = {"token", "refresh_token", "hdh_sa_token"}
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": "/",
                "secure": True,
                "httpOnly": name in http_only_names,
                "sameSite": "Lax",
            }
        )
    return cookies


def normalize_resource_url(url: str, base: str = "https://hdhive.com") -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("empty resource url")
    if url.startswith("/"):
        return base.rstrip("/") + url
    if not url.startswith("http"):
        if "/" not in url:
            return f"{base.rstrip('/')}/resource/{url}"
        return f"{base.rstrip('/')}/{url.lstrip('/')}"
    return url


def _extract_points_from_text(text: str) -> Optional[int]:
    m = POINTS_RE.search(text or "")
    if not m:
        return None
    for g in m.groups():
        if g is not None:
            return int(g)
    return None


def _body_text(page: Page, limit: int = 2000) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)[:limit]
    except Exception:
        try:
            return page.content()[:limit]
        except Exception:
            return ""


def _is_loading_text(text: str) -> bool:
    t = text or ""
    return any(h in t for h in LOADING_HINTS) or t.strip() in ("", "LOADING")


def dismiss_announcement(page: Page, max_wait_s: float = 15.0) -> bool:
    """
    Handle homepage/site announcement overlays.

    HDHive often shows a notice modal whose primary button is disabled until a
    ~10 second countdown finishes (e.g. "知道了 (10秒)" → "知道了").
    """
    deadline = time.time() + max_wait_s
    dismissed = False

    while time.time() < deadline:
        # 1) Prefer countdown / dismiss buttons inside dialogs
        clicked = page.evaluate(
            """() => {
              const labels = /知道了|我知道了|已阅读|关闭公告|确认|同意|继续/;
              // Live site: "我知道了 (10S)" — S means seconds
              const countdown = /\\d+\\s*[Ss秒]/;
              const buttons = [...document.querySelectorAll('button, [role="button"], a')];
              // Prefer enabled non-countdown dismiss buttons
              for (const b of buttons) {
                const t = (b.innerText || b.textContent || '').replace(/\\s+/g, ' ').trim();
                if (!labels.test(t)) continue;
                if (countdown.test(t)) continue;
                if (b.disabled) continue;
                const st = getComputedStyle(b);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                b.click();
                return {ok: true, text: t, phase: 'enabled'};
              }
              // Report countdown state for logging
              for (const b of buttons) {
                const t = (b.innerText || b.textContent || '').replace(/\\s+/g, ' ').trim();
                if (labels.test(t) && (countdown.test(t) || b.disabled)) {
                  return {ok: false, text: t, phase: 'countdown', disabled: !!b.disabled};
                }
              }
              // Close icon buttons inside MUI dialog
              const dialogs = [...document.querySelectorAll('[role="dialog"], .MuiDialog-root, .MuiModal-root')];
              for (const d of dialogs) {
                const close = d.querySelector('button[aria-label*="close" i], button[aria-label*="关闭"], .MuiIconButton-root');
                if (close) {
                  const t = (close.innerText || close.getAttribute('aria-label') || 'icon-close');
                  // only click pure close icons, not action buttons
                  if (!labels.test(close.innerText || '')) {
                    close.click();
                    return {ok: true, text: String(t), phase: 'icon'};
                  }
                }
              }
              return {ok: false, phase: 'none'};
            }"""
        )
        if clicked and clicked.get("ok"):
            logger.info("dismissed announcement via %s (%s)", clicked.get("phase"), clicked.get("text"))
            dismissed = True
            page.wait_for_timeout(500)
            # some sites chain two modals
            continue

        if clicked and clicked.get("phase") == "countdown":
            logger.info("announcement countdown active: %s", clicked.get("text"))
            page.wait_for_timeout(1000)
            continue

        # 2) No dialog buttons — if overlay text still looks like 公告, wait a bit
        body = _body_text(page, 800)
        if re.search(r"公告|通知|Notice|我知道了", body) and re.search(r"\d+\s*[Ss秒]", body):
            logger.info("announcement still visible with countdown, waiting…")
            page.wait_for_timeout(1000)
            continue

        break

    return dismissed


def wait_for_site_ready(page: Page, timeout_s: float = 45.0) -> None:
    """Wait past loading / security check / homepage announcement (~10s)."""
    deadline = time.time() + timeout_s
    saw_content = False
    security_loops = 0

    while time.time() < deadline:
        text = _body_text(page, 500)
        url = page.url

        # security interstitial often on /login?reason=secure-session
        if ("正在检测浏览器安全能力" in text) or (
            "请稍后" in text and "安全" in text
        ):
            security_loops += 1
            if security_loops <= 3 or security_loops % 5 == 0:
                logger.info("security check in progress… (%s)", security_loops)
            page.wait_for_timeout(1000)
            # after a long stuck security screen, try reloading once
            if security_loops == 12:
                logger.warning("security check stuck; reloading page")
                try:
                    page.reload(wait_until="domcontentloaded")
                except Exception:
                    pass
            if security_loops >= 25:
                raise RuntimeError(
                    "HDHive security check did not finish. "
                    "Try: python3 cli.py want '…' --headed"
                )
            continue

        security_loops = 0

        if _is_loading_text(text) or "清理缓存" in text or "版本不一致" in text:
            logger.info("site loading / cache update…")
            page.wait_for_timeout(1000)
            continue

        # hard block
        if "未通过安全检测" in text:
            raise RuntimeError(
                "HDHive blocked this browser (安全检测失败). "
                "Try headed mode: python3 cli.py unlock URL --headed"
            )

        saw_content = True
        # dismiss announcement (10s countdown common on homepage)
        dismiss_announcement(page, max_wait_s=min(14.0, max(2.0, deadline - time.time())))
        # if still stuck on pure announce overlay, keep waiting a little
        text2 = _body_text(page, 400)
        if re.search(r"知道了\s*[（(]?\s*\d+\s*[Ss秒]", text2):
            page.wait_for_timeout(1000)
            continue
        logger.info("site ready url=%s", url)
        return

    if not saw_content:
        raise RuntimeError("HDHive page did not become ready in time")
    # best-effort dismiss once more
    dismiss_announcement(page, max_wait_s=3.0)


def _normalize_share_url(link: str) -> str:
    link = (link or "").strip().rstrip("\\").rstrip("'\"<>")
    # decode common HTML escapes
    link = link.replace("&amp;", "&")
    return link


def _collect_115_links(page: Page) -> list[str]:
    found: list[str] = []

    def add(link: str) -> None:
        link = _normalize_share_url(link)
        if not link or not SHARE_115_RE.search(link):
            return
        # keep only the matched URL (drop trailing junk)
        m = SHARE_115_RE.search(link)
        if m:
            link = m.group(0)
        if link not in found:
            found.append(link)

    try:
        for href in page.eval_on_selector_all(
            "a[href*='115.com/s/'], a[href*='115cdn.com/s/'], a[href*='anxia.com/s/']",
            "els => els.map(e => e.href)",
        ):
            add(href)
    except Exception:
        pass

    # Visible text often contains the raw share URL after unlock
    try:
        text = page.locator("body").inner_text(timeout=3000)
        for m in SHARE_115_RE.finditer(text):
            add(m.group(0))
    except Exception:
        pass

    html = page.content()
    for m in SHARE_115_RE.finditer(html):
        link = m.group(0)
        if "password=" not in link.lower() and "#" not in link:
            window = html[m.end() : m.end() + 80]
            pm = NEARBY_PWD_RE.search(window) or NEARBY_PWD_RE.search(
                html[max(0, m.start() - 40) : m.end() + 80]
            )
            if pm:
                link = f"{link}?password={pm.group(1)}"
        add(link)
    return found


def _find_unlock_button(page: Page):
    for label in UNLOCK_BTN_TEXTS:
        loc = page.get_by_role("button", name=re.compile(label))
        if loc.count() > 0:
            return loc.first
        loc = page.locator(f"button:has-text('{label}')")
        if loc.count() > 0:
            return loc.first
        loc = page.locator(f"[role='button']:has-text('{label}')")
        if loc.count() > 0:
            return loc.first
    return None


def _has_token_cookie(page: Page, context: Optional[BrowserContext] = None) -> bool:
    """token is often HttpOnly — document.cookie cannot see it; use browser context."""
    ctx = context
    if ctx is None:
        try:
            ctx = page.context
        except Exception:
            ctx = None
    if ctx is not None:
        try:
            for c in ctx.cookies():
                if c.get("name") == "token" and c.get("value"):
                    return True
        except Exception:
            pass
    # fallback for non-httpOnly installs
    try:
        return bool(
            page.evaluate(
                """() => {
                    const m = document.cookie.match(/(?:^|;\\s*)token=([^;]+)/);
                    return !!(m && m[1]);
                }"""
            )
        )
    except Exception:
        return False


class HDHiveBrowser:
    def __init__(
        self,
        cookie_header: str,
        base_url: str = "https://hdhive.com",
        headless: bool = True,
        max_points: int = 4,
        navigation_timeout_ms: int = 60000,
    ):
        if not cookie_header or not cookie_header.strip():
            raise ValueError("HDHIVE_COOKIE is empty")
        self.cookie_header = cookie_header.strip()
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.max_points = max_points
        self.navigation_timeout_ms = navigation_timeout_ms
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    def __enter__(self) -> "HDHiveBrowser":
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self) -> None:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        self._context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
            viewport={"width": 1365, "height": 900},
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        )
        if _STEALTH is not None:
            try:
                _STEALTH.apply_stealth_sync(self._context)
            except Exception as e:
                logger.warning("playwright-stealth failed: %s", e)

        self._context.set_default_timeout(self.navigation_timeout_ms)
        host = urlparse(self.base_url).hostname or "hdhive.com"
        domains = {host, f".{host.lstrip('.')}", "hdhive.com", ".hdhive.com", "www.hdhive.com"}
        cookies = []
        for domain in domains:
            cookies.extend(parse_cookie_header(self.cookie_header, domain=domain))
        seen = set()
        unique = []
        for c in cookies:
            key = (c["name"], c["domain"])
            if key in seen:
                continue
            seen.add(key)
            unique.append(c)
        self._context.add_cookies(unique)
        logger.info("loaded %s cookie entries for HDHive session", len(unique))

    def close(self) -> None:
        try:
            if self._context:
                self._context.close()
        finally:
            self._context = None
        try:
            if self._browser:
                self._browser.close()
        finally:
            self._browser = None
        try:
            if self._pw:
                self._pw.stop()
        finally:
            self._pw = None

    def new_page(self) -> Page:
        if not self._context:
            raise RuntimeError("browser not started")
        return self._context.new_page()

    def _goto(self, page: Page, url: str, attempts: int = 3) -> None:
        last: Optional[Exception] = None
        for i in range(attempts):
            try:
                page.goto(url, wait_until="commit", timeout=min(45000, self.navigation_timeout_ms))
                return
            except Exception as e:
                last = e
                logger.warning("goto failed (%s/%s) %s: %s", i + 1, attempts, url, e)
                page.wait_for_timeout(1200)
        if last:
            raise last

    def verify_session(self) -> dict:
        page = self.new_page()
        try:
            self._goto(page, f"{self.base_url}/")
            wait_for_site_ready(page, timeout_s=50.0)
            has_token = _has_token_cookie(page, self._context)
            body = _body_text(page, 400)
            # Prefer UI signals: Premium/nav present + not forced login
            on_login = "/login" in (page.url or "")
            looks_logged_out = on_login or bool(
                re.search(r"^\s*登录\b|注册账号", body)
                and not re.search(r"积分|签到|退出登录|个人中心", body)
            )
            # After security handshake cookies may refresh — re-check context
            if not has_token and self._context:
                has_token = any(
                    c.get("name") == "token" and c.get("value")
                    for c in self._context.cookies()
                )
            return {
                "ok": bool(has_token) and not looks_logged_out and not on_login,
                "has_token_cookie": has_token,
                "looks_logged_out": looks_logged_out,
                "title": page.title(),
                "url": page.url,
                "body_preview": body.replace("\n", " ")[:200],
                "announcement_handled": True,
            }
        finally:
            page.close()

    def unlock_resource(self, resource_url: str, debug_dir: Optional[str] = None) -> UnlockResult:
        url = normalize_resource_url(resource_url, self.base_url)
        page = self.new_page()
        points_cost: Optional[int] = None
        try:
            logger.info("opening %s", url)
            self._goto(page, url)
            wait_for_site_ready(page, timeout_s=50.0)

            title = ""
            try:
                title = page.title()
            except Exception:
                pass

            if not _has_token_cookie(page, self._context):
                raise RuntimeError(
                    "HDHive session cookie not accepted (token missing after navigation). "
                    "Re-copy cookies from a logged-in browser tab."
                )

            # announcement can also appear on resource pages — wait full countdown
            dismiss_announcement(page, max_wait_s=14.0)
            page.wait_for_timeout(800)

            links = _collect_115_links(page)
            if links:
                logger.info("already unlocked, found %s 115 link(s)", len(links))
                return UnlockResult(
                    resource_url=url,
                    share_urls=links,
                    points_cost=0,
                    already_unlocked=True,
                    page_title=title,
                )

            body_text = _body_text(page, 4000)
            points_cost = _extract_points_from_text(body_text)
            already = any(h in body_text for h in ("已解锁", "复制链接", "分享链接", "资源链接"))
            if points_cost is not None and points_cost > self.max_points and not already:
                return UnlockResult(
                    resource_url=url,
                    share_urls=[],
                    points_cost=points_cost,
                    skipped=True,
                    skip_reason=f"points {points_cost} > max {self.max_points}",
                    page_title=title,
                )

            btn = _find_unlock_button(page)
            if btn is not None:
                logger.info("clicking unlock button (points=%s)", points_cost)
                try:
                    btn.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                # Prefer staying on HDHive; open share links in a new tab if any
                try:
                    with page.context.expect_page(timeout=3000) as new_page_info:
                        try:
                            btn.click(timeout=5000)
                        except PwTimeout:
                            btn.dispatch_event("click")
                    popup = new_page_info.value
                    popup.wait_for_load_state("domcontentloaded")
                    page.wait_for_timeout(1000)
                    # if popup is a 115 share URL, capture it
                    if re.search(r"115|anxia", popup.url, re.I):
                        add_url = popup.url
                        pwd_m = re.search(r"password=([A-Za-z0-9]{4})", add_url, re.I)
                        if not pwd_m:
                            try:
                                pt = popup.locator("body").inner_text(timeout=2000)
                                pm = NEARBY_PWD_RE.search(pt)
                                if pm:
                                    add_url = f"{add_url.split('?')[0]}?password={pm.group(1)}"
                            except Exception:
                                pass
                        links = _collect_115_links(popup) or [add_url]
                        popup.close()
                        if links:
                            return UnlockResult(
                                resource_url=url,
                                share_urls=links,
                                points_cost=points_cost,
                                already_unlocked=False,
                                page_title=title,
                            )
                    else:
                        popup.close()
                except PwTimeout:
                    # no popup — same-tab click
                    try:
                        btn.click(timeout=5000)
                    except Exception:
                        try:
                            btn.dispatch_event("click")
                        except Exception:
                            pass
                for confirm in ("确认", "确定", "继续", "解锁"):
                    try:
                        c = page.get_by_role("button", name=re.compile(f"^{confirm}$"))
                        if c.count() > 0 and c.first.is_visible():
                            c.first.click(timeout=2000)
                    except Exception:
                        pass
                page.wait_for_timeout(3000)
                # if navigation left HDHive for 115 share page
                if re.search(r"115|anxia", page.url, re.I) and "/s/" in page.url:
                    share = page.url
                    try:
                        pt = page.locator("body").inner_text(timeout=2000)
                        pm = NEARBY_PWD_RE.search(pt) or re.search(
                            r"password=([A-Za-z0-9]{4})", share, re.I
                        )
                        if pm and "password=" not in share.lower():
                            code = pm.group(1) if pm.lastindex else pm.group(0)
                            if len(str(code)) == 4:
                                share = f"{share.split('?')[0]}?password={code}"
                    except Exception:
                        pass
                    return UnlockResult(
                        resource_url=url,
                        share_urls=[share],
                        points_cost=points_cost,
                        already_unlocked=False,
                        page_title=title,
                    )
            else:
                logger.info("no unlock button found; waiting for links to appear")
                page.wait_for_timeout(2000)

            deadline = time.time() + 25
            links = []
            while time.time() < deadline:
                dismiss_announcement(page, max_wait_s=1.0)
                links = _collect_115_links(page)
                if links:
                    break
                page.wait_for_timeout(1000)

            if debug_dir and not links:
                from pathlib import Path

                Path(debug_dir).mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(Path(debug_dir) / "unlock_fail.png"), full_page=True)
                Path(debug_dir, "unlock_fail.html").write_text(page.content(), encoding="utf-8")
                logger.warning("debug dump written to %s", debug_dir)

            if not links:
                raise RuntimeError(
                    "no 115 share link found after unlock attempt "
                    f"(points_cost={points_cost}). UI may have changed or resource is not 115."
                )

            return UnlockResult(
                resource_url=url,
                share_urls=links,
                points_cost=points_cost,
                already_unlocked=btn is None,
                page_title=title,
            )
        finally:
            page.close()
