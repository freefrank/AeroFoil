import logging
import base64
import time
from urllib.parse import urljoin

import requests

from app.downloads.constants import DOWNLOADS_USER_AGENT


logger = logging.getLogger("downloads.resolver")


def _remaining_timeout_seconds(deadline, minimum=0.25):
    remaining = float(deadline - time.monotonic())
    if remaining <= 0:
        return 0.0
    return max(remaining, float(minimum))


def resolve_download_url(url, timeout=30, max_redirects=10):
    if not url:
        return "url", url
    if str(url).lower().startswith("magnet:"):
        return "magnet", url

    start = time.monotonic()
    timeout = max(float(timeout or 0), 0.1)
    deadline = start + timeout
    current_url = str(url)
    session = requests.Session()
    session.headers.update({"User-Agent": DOWNLOADS_USER_AGENT})

    try:
        for _ in range(max(int(max_redirects), 1)):
            per_request_timeout = _remaining_timeout_seconds(deadline)
            if per_request_timeout <= 0:
                break

            response = session.get(
                current_url,
                allow_redirects=False,
                timeout=per_request_timeout,
            )

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    break
                current_url = urljoin(current_url, location)
                if current_url.lower().startswith("magnet:"):
                    return "magnet", current_url
                continue

            response.raise_for_status()
            content_type = str(response.headers.get("Content-Type") or "").lower()
            content = response.content or b""
            if "application/x-bittorrent" in content_type or content.startswith(b"d8:announce"):
                return "torrent_content", content
            return "url", current_url
    except Exception as exc:
        logger.warning("Failed to resolve download URL %s: %s", current_url, exc)
        if current_url.lower().startswith("magnet:"):
            return "magnet", current_url
        return "url", url

    if current_url.lower().startswith("magnet:"):
        return "magnet", current_url
    return "url", current_url if current_url != url else url


def get_metainfo_base64(content):
    if not content:
        return None
    return base64.b64encode(content).decode("utf-8")
