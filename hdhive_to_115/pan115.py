"""Minimal 115 share-link receive client (no TgtoDrive deps)."""

from __future__ import annotations

import logging
import re
import time
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

SHARE_RE = re.compile(
    r"https?://(?:115|115cdn|anxia)\.com/s/(?P<code>\w+)"
    r"(?:[?#][^\\s]*)?",
    re.I,
)
# password may be ?password=xxxx or #xxxx or 访问码:xxxx nearby
PASSWORD_RE = re.compile(
    r"(?:password=|访问码[:：\s]*|提取码[:：\s]*|#)(?P<pwd>[a-zA-Z0-9]{4})",
    re.I,
)


def parse_115_share(link: str) -> Optional[tuple[str, str]]:
    """Return (share_code, receive_code) or None."""
    text = (link or "").strip()
    m = SHARE_RE.search(text)
    if not m:
        return None
    share_code = m.group("code")
    receive_code = ""
    parsed = urlparse(m.group(0))
    qs = parse_qs(parsed.query)
    if "password" in qs and qs["password"]:
        receive_code = qs["password"][0]
    if not receive_code and parsed.fragment and re.fullmatch(r"[a-zA-Z0-9]{4}", parsed.fragment):
        receive_code = parsed.fragment
    if not receive_code:
        pm = PASSWORD_RE.search(text)
        if pm:
            receive_code = pm.group("pwd")
    if not share_code or not receive_code:
        logger.error("115 share link missing code or password: %s", text[:120])
        return None
    return share_code, receive_code


class Pan115Client:
    def __init__(self, cookies: str):
        if not cookies or not cookies.strip():
            raise ValueError("ENV_115_COOKIES / cookies is empty")
        self.cookies = cookies.strip()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": UA,
                "Cookie": self.cookies,
            }
        )
        self.user_id = self._get_userid()

    def _get_userid(self) -> str:
        url = "https://my.115.com/?ct=ajax&ac=get_user_aq"
        r = self.session.get(url, timeout=30)
        r.raise_for_status()
        data = r.json()
        if not data.get("state"):
            raise RuntimeError(f"115 auth failed: {data.get('error_msg') or data}")
        uid = str(data.get("data", {}).get("uid") or "")
        if not uid:
            raise RuntimeError("115 auth failed: empty uid")
        logger.info("115 logged in, uid=%s", uid)
        return uid

    def snap(self, share_code: str, receive_code: str) -> list[dict]:
        items: list[dict] = []
        offset = 0
        limit = 20
        while True:
            url = (
                "https://webapi.115.com/share/snap"
                f"?share_code={share_code}&offset={offset}"
                f"&limit={limit}&receive_code={receive_code}&cid="
            )
            r = self.session.get(url, timeout=30)
            r.raise_for_status()
            body = r.json()
            if not body.get("state"):
                raise RuntimeError(f"115 snap failed: {body.get('error') or body}")
            data = body.get("data") or {}
            page = data.get("list") or []
            items.extend(page)
            count = int(data.get("count") or 0)
            if len(items) >= count or not page:
                break
            offset = len(items)
        return items

    def receive(
        self,
        share_code: str,
        receive_code: str,
        file_ids: list[str],
        pid: str | int = "0",
        delay: float = 2.0,
    ) -> bool:
        if delay > 0:
            time.sleep(delay)
        payload = {
            "user_id": self.user_id,
            "share_code": share_code,
            "receive_code": receive_code,
            "file_id": ",".join(file_ids),
        }
        if str(pid) not in ("", "0"):
            payload["cid"] = str(pid)
        r = self.session.post(
            "https://webapi.115.com/share/receive",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=60,
        )
        r.raise_for_status()
        body = r.json()
        if body.get("state"):
            return True
        err = str(body.get("error") or body)
        if "无需重复接收" in err:
            logger.info("already received: %s", share_code)
            return True
        raise RuntimeError(f"115 receive failed: {err}")

    def save_share_link(self, share_url: str, pid: str | int = "0", delay: float = 2.0) -> bool:
        parsed = parse_115_share(share_url)
        if not parsed:
            raise ValueError(f"not a valid 115 share url: {share_url}")
        share_code, receive_code = parsed
        items = self.snap(share_code, receive_code)
        if not items:
            raise RuntimeError("115 share is empty")
        file_ids = []
        for item in items:
            fid = item.get("fid") or item.get("cid")
            if fid is not None:
                file_ids.append(str(fid))
        if not file_ids:
            raise RuntimeError("no file ids in 115 share")
        logger.info(
            "receiving %s files from %s to cid=%s",
            len(file_ids),
            share_code,
            pid,
        )
        return self.receive(share_code, receive_code, file_ids, pid=pid, delay=delay)
