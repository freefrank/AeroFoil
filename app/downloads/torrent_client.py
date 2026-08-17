import hashlib
import json
import os
import logging
import re
import time
import xmlrpc.client
import hmac
from urllib.parse import urlencode, urlparse

import requests

from app.downloads.constants import DOWNLOADS_USER_AGENT, UNSUPPORTED_CLIENT_TYPE_MESSAGE
from app.downloads.update_selection import (
    TORRENT_DLC_SELECTION_ERROR,
    TORRENT_UPDATE_SELECTION_ERROR,
    get_matching_dlc_indices,
    get_matching_update_indices,
    poll_update_file_names,
    preflight_has_matching_dlc,
    preflight_has_matching_update,
)
from app.downloads.versioning import (
    select_dlc_entry_ids,
    select_update_entry_ids,
    select_update_file_indices as shared_select_update_file_indices,
)
from app.downloads.resolver import get_metainfo_base64

logger = logging.getLogger("downloads.qbittorrent")
AEROFOIL_MANAGED_TAG = "aerofoil"
LEGACY_OWNFOIL_MANAGED_TAG = "ownfoil"
MANAGED_TAGS = (AEROFOIL_MANAGED_TAG, LEGACY_OWNFOIL_MANAGED_TAG)


def _normalize_labels(values):
    out = []
    for value in values or []:
        text = str(value or "").strip()
        if text:
            out.append(text)
    return out


def _parse_qbittorrent_tags(raw_tags):
    return _normalize_labels((raw_tags or "").split(","))


def _is_managed_label_value(label):
    text = str(label or "").strip()
    return text in MANAGED_TAGS


def _has_managed_qbittorrent_tag(raw_tags):
    return any(_is_managed_label_value(tag) for tag in _parse_qbittorrent_tags(raw_tags))


def _has_managed_transmission_labels(labels):
    return any(_is_managed_label_value(label) for label in _normalize_labels(labels))


def _fetch_qbittorrent_managed_items(session, base, timeout_seconds, extra_params=None):
    params_base = dict(extra_params or {})
    items = []
    seen_hashes = set()
    for managed_tag in MANAGED_TAGS:
        params = dict(params_base)
        params["tag"] = managed_tag
        resp = session.get(f"{base}/api/v2/torrents/info", params=params, timeout=timeout_seconds)
        if resp.status_code != 200:
            continue
        for item in (resp.json() or []):
            torrent_hash = str(item.get("hash") or "").strip().lower()
            if torrent_hash and torrent_hash in seen_hashes:
                continue
            if torrent_hash:
                seen_hashes.add(torrent_hash)
            items.append(item)
    return items


def _new_client_session(username=None, password=None):
    session = requests.Session()
    session.headers.update({"User-Agent": DOWNLOADS_USER_AGENT})
    if username or password:
        session.auth = (username or "", password or "")
    return session


def _transmission_request(session, base, payload, timeout_seconds):
    resp = session.post(
        f"{base}/transmission/rpc",
        json=payload,
        timeout=timeout_seconds,
    )
    if resp.status_code == 409:
        session_id = resp.headers.get("X-Transmission-Session-Id")
        if session_id:
            session.headers.update({"X-Transmission-Session-Id": session_id})
            resp = session.post(
                f"{base}/transmission/rpc",
                json=payload,
                timeout=timeout_seconds,
            )
    return resp


def _login_qbittorrent(session, base, username=None, password=None, timeout_seconds=10):
    # qBittorrent Web API requires Referer/Origin on authenticated API requests in newer releases.
    base_ref = str(base or "").strip().rstrip("/") + "/"
    if base_ref:
        session.headers.setdefault("Referer", base_ref)
        session.headers.setdefault("Origin", base_ref.rstrip("/"))
    if not username and not password:
        return True
    login_resp = session.post(
        f"{base}/api/v2/auth/login",
        data={"username": username or "", "password": password or ""},
        timeout=timeout_seconds,
    )
    if login_resp.status_code == 204:
        return True
    return login_resp.status_code == 200 and login_resp.text.strip() in ("Ok.", "")


def _rtorrent_xmlrpc_endpoints(url):
    parsed = urlparse(str(url or "").strip())
    scheme = parsed.scheme or "http"
    netloc = parsed.netloc
    path = parsed.path or ""
    if not netloc:
        return []
    base = f"{scheme}://{netloc}"
    if path and path != "/":
        return [f"{base}{path}"]
    return [f"{base}/RPC2", f"{base}/RPC1", f"{base}/plugins/rpc/rpc.php"]


def _rtorrent_xmlrpc(url, method, params=None, timeout_seconds=10, username=None, password=None, session=None):
    endpoints = _rtorrent_xmlrpc_endpoints(url)
    if not endpoints:
        return False, "rTorrent URL is invalid.", None
    payload = xmlrpc.client.dumps(tuple(params or []), methodname=method)
    headers = {"Content-Type": "text/xml", "User-Agent": DOWNLOADS_USER_AGENT}
    request_session = session or _new_client_session(username, password)
    last_error = "rTorrent returned an error."
    for endpoint in endpoints:
        try:
            resp = request_session.post(endpoint, data=payload.encode("utf-8"), headers=headers, timeout=timeout_seconds)
        except Exception as exc:
            last_error = f"rTorrent request failed: {exc}"
            continue
        if resp.status_code == 404:
            last_error = f"rTorrent returned {resp.status_code} at {endpoint}."
            continue
        if resp.status_code == 401:
            return False, f"rTorrent authentication failed ({resp.status_code}) at {endpoint}.", None
        if resp.status_code != 200:
            last_error = f"rTorrent returned {resp.status_code} at {endpoint}."
            continue
        try:
            parsed_params, _method = xmlrpc.client.loads(resp.content)
        except Exception as exc:
            last_error = f"rTorrent XML-RPC parse error at {endpoint}: {exc}"
            continue
        return True, None, parsed_params[0] if parsed_params else None
    return False, last_error, None


def _build_rtorrent_managed_label(category):
    category_text = str(category or "").strip()
    if category_text.lower() in MANAGED_TAGS:
        return AEROFOIL_MANAGED_TAG
    if category_text:
        return f"{AEROFOIL_MANAGED_TAG}:{category_text}"
    return AEROFOIL_MANAGED_TAG


def _build_rtorrent_add_commands(category=None, download_path=None):
    commands = []
    managed_label = _build_rtorrent_managed_label(category)
    if managed_label:
        commands.append(f"d.custom1.set={managed_label}")
    download_path_text = str(download_path or "").strip()
    if download_path_text:
        commands.append(f"d.directory.set={download_path_text}")
    return commands


def test_torrent_client(client_type, url, username=None, password=None, timeout_seconds=10):
    if not url:
        return False, "Client URL is required."
    client_type = (client_type or "").lower()
    if client_type == "qbittorrent":
        return _test_qbittorrent(url, username, password, timeout_seconds)
    if client_type == "transmission":
        return _test_transmission(url, username, password, timeout_seconds)
    if client_type == "deluge":
        return _test_deluge(url, password, timeout_seconds)
    if client_type == "rtorrent":
        return _test_rtorrent(url, username, password, timeout_seconds)
    if client_type == "utorrent":
        return _test_utorrent(url, username, password, timeout_seconds)
    if client_type == "aria2":
        return _test_aria2(url, password, timeout_seconds)
    if client_type == "rqbit":
        return _test_rqbit(url, username, password, timeout_seconds)
    if client_type == "flood":
        return _test_flood(url, username, password, timeout_seconds)
    if client_type == "vuze":
        return _test_transmission(url, username, password, timeout_seconds)
    if client_type == "hadouken":
        return _test_hadouken(url, username, password, timeout_seconds)
    if client_type == "tribler":
        return _test_tribler(url, password, timeout_seconds)
    if client_type == "blackhole":
        return _test_blackhole(url)
    if client_type == "downloadstation":
        return _test_downloadstation_torrent(url, username, password, timeout_seconds)
    if client_type == "freeboxdownload":
        return _test_freebox(url, username, password, timeout_seconds)
    return False, UNSUPPORTED_CLIENT_TYPE_MESSAGE


def add_torrent(client_type, url, username=None, password=None, download_url=None, torrent_content=None, category=None, download_path=None, timeout_seconds=15, expected_name=None, update_only=False, dlc_only=False, exclude_russian=False, expected_update_number=None, expected_version=None):
    if not download_url and not torrent_content:
        return False, "Download URL is required.", None
    client_type = (client_type or "").lower()
    if client_type == "qbittorrent":
        return _add_qbittorrent(url, username, password, download_url, torrent_content, category, download_path, timeout_seconds, expected_name, update_only, dlc_only, exclude_russian, expected_update_number, expected_version)
    if client_type == "transmission":
        return _add_transmission(url, username, password, download_url, torrent_content, category, download_path, timeout_seconds, expected_name, update_only, dlc_only, exclude_russian, expected_update_number, expected_version)
    if client_type == "deluge":
        return _add_deluge(url, password, download_url, torrent_content, category, download_path, timeout_seconds, update_only, dlc_only, exclude_russian, expected_update_number, expected_version)
    if client_type == "rtorrent":
        return _add_rtorrent(
            url,
            download_url,
            username,
            password,
            category,
            download_path,
            timeout_seconds,
            update_only,
            dlc_only,
            exclude_russian,
            expected_update_number,
            expected_version,
            expected_name,
        )
    if client_type == "utorrent":
        return _add_utorrent(
            url,
            username,
            password,
            download_url,
            category,
            download_path,
            timeout_seconds,
        )
    if client_type == "aria2":
        return _add_aria2(url, password, download_url, download_path, timeout_seconds)
    if client_type == "rqbit":
        return _add_rqbit(url, username, password, download_url, timeout_seconds)
    if client_type == "flood":
        return _add_flood(url, username, password, download_url, category, timeout_seconds)
    if client_type == "vuze":
        return _add_transmission(url, username, password, download_url, torrent_content, category, download_path, timeout_seconds, expected_name, update_only, dlc_only, exclude_russian, expected_update_number, expected_version)
    if client_type == "hadouken":
        return _add_hadouken(url, username, password, download_url, timeout_seconds)
    if client_type == "tribler":
        return _add_tribler(url, password, download_url, download_path, timeout_seconds)
    if client_type == "blackhole":
        return _add_blackhole(download_url, torrent_content, download_path)
    if client_type == "downloadstation":
        return _add_downloadstation_torrent(url, username, password, download_url, timeout_seconds)
    if client_type == "freeboxdownload":
        return _add_freebox(url, username, password, download_url, timeout_seconds)
    return False, UNSUPPORTED_CLIENT_TYPE_MESSAGE, None


def list_completed(client_type, url, username=None, password=None, category=None, download_path=None, timeout_seconds=15):
    client_type = (client_type or "").lower()
    if client_type == "qbittorrent":
        return _list_completed_qbittorrent(url, username, password, category, download_path, timeout_seconds)
    if client_type == "transmission":
        return _list_completed_transmission(url, username, password, category, download_path, timeout_seconds)
    if client_type == "deluge":
        return _list_completed_deluge(url, password, category, download_path, timeout_seconds)
    if client_type == "rtorrent":
        return _list_completed_rtorrent(url, username, password, category, download_path, timeout_seconds)
    if client_type == "utorrent":
        return _list_completed_utorrent(url, username, password, category, download_path, timeout_seconds)
    if client_type == "aria2":
        return _list_completed_aria2(url, password, download_path, timeout_seconds)
    if client_type == "rqbit":
        return _list_completed_rqbit(url, username, password, download_path, timeout_seconds)
    if client_type == "flood":
        return _list_completed_flood(url, username, password, category, download_path, timeout_seconds)
    if client_type == "vuze":
        return _list_completed_transmission(url, username, password, category, download_path, timeout_seconds)
    if client_type == "hadouken":
        return _list_completed_hadouken(url, username, password, category, download_path, timeout_seconds)
    if client_type == "tribler":
        return _list_completed_tribler(url, password, category, download_path, timeout_seconds)
    if client_type == "blackhole":
        return []
    if client_type == "downloadstation":
        return _list_completed_downloadstation_torrent(url, username, password, category, download_path, timeout_seconds)
    if client_type == "freeboxdownload":
        return _list_completed_freebox(url, username, password, category, download_path, timeout_seconds)
    return []


def list_active(client_type, url, username=None, password=None, category=None, download_path=None, timeout_seconds=15):
    client_type = (client_type or "").lower()
    if client_type == "qbittorrent":
        return _list_active_qbittorrent(url, username, password, category, download_path, timeout_seconds)
    if client_type == "transmission":
        return _list_active_transmission(url, username, password, category, download_path, timeout_seconds)
    if client_type == "deluge":
        return _list_active_deluge(url, password, category, download_path, timeout_seconds)
    if client_type == "rtorrent":
        return _list_active_rtorrent(url, username, password, category, download_path, timeout_seconds)
    if client_type == "utorrent":
        return _list_active_utorrent(url, username, password, category, download_path, timeout_seconds)
    if client_type == "aria2":
        return _list_active_aria2(url, password, download_path, timeout_seconds)
    if client_type == "rqbit":
        return _list_active_rqbit(url, username, password, download_path, timeout_seconds)
    if client_type == "flood":
        return _list_active_flood(url, username, password, category, download_path, timeout_seconds)
    if client_type == "vuze":
        return _list_active_transmission(url, username, password, category, download_path, timeout_seconds)
    if client_type == "hadouken":
        return _list_active_hadouken(url, username, password, category, download_path, timeout_seconds)
    if client_type == "tribler":
        return _list_active_tribler(url, password, category, download_path, timeout_seconds)
    if client_type == "blackhole":
        return []
    if client_type == "downloadstation":
        return _list_active_downloadstation_torrent(url, username, password, category, download_path, timeout_seconds)
    if client_type == "freeboxdownload":
        return _list_active_freebox(url, username, password, category, download_path, timeout_seconds)
    return []


def remove_torrent(client_type, url, torrent_hash, username=None, password=None, timeout_seconds=15, delete_files=False):
    if not torrent_hash:
        return False, "Torrent hash is required."
    client_type = (client_type or "").lower()
    if client_type == "qbittorrent":
        return _remove_qbittorrent(url, username, password, torrent_hash, timeout_seconds, delete_files=delete_files)
    if client_type == "transmission":
        return _remove_transmission(url, username, password, torrent_hash, timeout_seconds, delete_files=delete_files)
    if client_type == "deluge":
        return _remove_deluge(url, password, torrent_hash, timeout_seconds, delete_files=delete_files)
    if client_type == "rtorrent":
        return _remove_rtorrent(url, torrent_hash, username, password, timeout_seconds, delete_files=delete_files)
    if client_type == "utorrent":
        return _remove_utorrent(url, username, password, torrent_hash, timeout_seconds, delete_files=delete_files)
    if client_type == "aria2":
        return _remove_aria2(url, password, torrent_hash, timeout_seconds, delete_files=delete_files)
    if client_type == "rqbit":
        return _remove_rqbit(url, username, password, torrent_hash, timeout_seconds, delete_files=delete_files)
    if client_type == "flood":
        return _remove_flood(url, username, password, torrent_hash, timeout_seconds, delete_files=delete_files)
    if client_type == "vuze":
        return _remove_transmission(url, username, password, torrent_hash, timeout_seconds, delete_files=delete_files)
    if client_type == "hadouken":
        return _remove_hadouken(url, username, password, torrent_hash, timeout_seconds, delete_files=delete_files)
    if client_type == "tribler":
        return _remove_tribler(url, password, torrent_hash, timeout_seconds, delete_files=delete_files)
    if client_type == "blackhole":
        return False, "Blackhole client does not support API removals."
    if client_type == "downloadstation":
        return _remove_downloadstation_torrent(url, username, password, torrent_hash, timeout_seconds, delete_files=delete_files)
    if client_type == "freeboxdownload":
        return _remove_freebox(url, username, password, torrent_hash, timeout_seconds, delete_files=delete_files)
    return False, UNSUPPORTED_CLIENT_TYPE_MESSAGE


def _utorrent_gui_base(url):
    base = str(url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.lower().endswith("/gui"):
        return base
    return f"{base}/gui"


def _utorrent_extract_token(text):
    match = re.search(r"id=['\"]token['\"][^>]*>([^<]+)<", text or "", flags=re.IGNORECASE)
    return (match.group(1).strip() if match else "")


def _utorrent_request(session, gui_base, token, timeout_seconds, params=None, method="GET"):
    request_params = dict(params or {})
    request_params["token"] = token
    resp = session.request(
        method,
        f"{gui_base}/",
        params=request_params,
        timeout=timeout_seconds,
    )
    return resp


def _utorrent_authenticated_session(url, username=None, password=None, timeout_seconds=10):
    gui_base = _utorrent_gui_base(url)
    if not gui_base:
        return None, None, "uTorrent URL is required."
    session = _new_client_session(username, password)
    try:
        token_resp = session.get(f"{gui_base}/token.html", timeout=timeout_seconds)
    except Exception as exc:
        return None, None, f"uTorrent request failed: {exc}"
    if token_resp.status_code == 401:
        return None, None, "uTorrent authentication failed."
    if token_resp.status_code != 200:
        return None, None, f"uTorrent returned {token_resp.status_code}."
    token = _utorrent_extract_token(token_resp.text)
    if not token:
        return None, None, "uTorrent token not found in response."
    return session, token, None


def _utorrent_list_raw(session, gui_base, token, timeout_seconds):
    resp = _utorrent_request(
        session,
        gui_base,
        token,
        timeout_seconds,
        params={"list": 1},
    )
    if resp.status_code != 200:
        return False, f"uTorrent returned {resp.status_code}.", []
    try:
        payload = resp.json() or {}
    except Exception as exc:
        return False, f"uTorrent JSON parse error: {exc}", []
    return True, None, (payload.get("torrents") or [])


def _parse_utorrent_item(raw, download_path=None):
    if not isinstance(raw, list) or len(raw) < 3:
        return None
    torrent_hash = str(raw[0] or "").strip().lower()
    status_code = _to_int(raw[1], 0)
    name = str(raw[2] or "").strip() or torrent_hash
    size = _to_int(raw[3], 0)
    progress_permille = _to_float(raw[4], 0.0)
    progress = max(0.0, min(100.0, progress_permille / 10.0))
    downloaded = _to_int(raw[5], int(size * (progress / 100.0)))
    up_speed = _to_int(raw[8], 0)
    down_speed = _to_int(raw[9], 0)
    eta = _to_int(raw[10], -1)
    label = str(raw[11] or "").strip() if len(raw) > 11 else ""
    peers = _to_int(raw[12], 0) if len(raw) > 12 else 0
    seeders = _to_int(raw[14], 0) if len(raw) > 14 else 0
    leechers = _to_int(raw[13], 0) if len(raw) > 13 else 0
    if status_code & 16:
        status = "error"
    elif status_code & 32:
        status = "paused"
    elif status_code & 1:
        status = "downloading"
    elif status_code & 64:
        status = "queued"
    elif progress >= 100.0:
        status = "completed"
    else:
        status = "stalled"
    content_path = os.path.join(download_path, name) if download_path else name
    return {
        "hash": torrent_hash,
        "name": name,
        "path": content_path,
        "status": status,
        "progress": progress,
        "size": size,
        "downloaded": downloaded,
        "down_speed": down_speed,
        "up_speed": up_speed,
        "eta": eta,
        "peers": peers,
        "seeders": seeders,
        "leechers": leechers,
        "category": label,
        "protocol": "torrent",
        "client_type": "utorrent",
        "_status_code": status_code,
    }


def _test_utorrent(url, username=None, password=None, timeout_seconds=10):
    session, token, error = _utorrent_authenticated_session(url, username, password, timeout_seconds)
    if error:
        return False, error
    gui_base = _utorrent_gui_base(url)
    resp = _utorrent_request(
        session,
        gui_base,
        token,
        timeout_seconds,
        params={"action": "getsettings"},
    )
    if resp.status_code != 200:
        return False, f"uTorrent returned {resp.status_code}."
    try:
        build = (resp.json() or {}).get("build")
    except Exception:
        build = None
    if build:
        return True, f"uTorrent OK (build {build})."
    return True, "uTorrent OK."


def _add_utorrent(url, username, password, download_url, category, download_path, timeout_seconds):
    gui_base = _utorrent_gui_base(url)
    session, token, error = _utorrent_authenticated_session(url, username, password, timeout_seconds)
    if error:
        return False, error, None
    ok, error, before_items = _utorrent_list_raw(session, gui_base, token, timeout_seconds)
    before_hashes = {str(item[0] or "").strip().lower() for item in before_items if isinstance(item, list) and item}
    if not ok:
        before_hashes = set()
    resp = _utorrent_request(
        session,
        gui_base,
        token,
        timeout_seconds,
        params={"action": "add-url", "s": download_url},
    )
    if resp.status_code != 200:
        return False, f"uTorrent returned {resp.status_code}.", None
    torrent_hash = _extract_magnet_hash(download_url) if download_url else None
    ok, _err, after_items = _utorrent_list_raw(session, gui_base, token, timeout_seconds)
    if ok:
        after_hashes = [str(item[0] or "").strip().lower() for item in after_items if isinstance(item, list) and item]
        new_hashes = [h for h in after_hashes if h and h not in before_hashes]
        if new_hashes:
            torrent_hash = new_hashes[0]
    managed_label = _build_rtorrent_managed_label(category)
    if torrent_hash:
        _utorrent_request(
            session,
            gui_base,
            token,
            timeout_seconds,
            params={"action": "setprops", "hash": torrent_hash, "s": "label", "v": managed_label},
        )
    _ = download_path
    return True, "uTorrent accepted torrent.", torrent_hash


def _list_utorrent_with_state(url, username, password, category, download_path, timeout_seconds, include_completed):
    gui_base = _utorrent_gui_base(url)
    session, token, error = _utorrent_authenticated_session(url, username, password, timeout_seconds)
    if error:
        logger.warning("Failed to load uTorrent downloads: %s", error)
        return []
    ok, error, rows = _utorrent_list_raw(session, gui_base, token, timeout_seconds)
    if not ok:
        logger.warning("Failed to load uTorrent downloads: %s", error)
        return []
    expected_label = _build_rtorrent_managed_label(category) if category else None
    out = []
    for raw in rows:
        item = _parse_utorrent_item(raw, download_path=download_path)
        if not item:
            continue
        label = str(item.get("category") or "").strip()
        if not _is_deluge_managed_label(label):
            continue
        if expected_label and label != expected_label:
            continue
        is_completed = item.get("progress", 0) >= 100.0
        if include_completed and not is_completed:
            continue
        if not include_completed and is_completed:
            continue
        out.append(item)
    return out


def _list_active_utorrent(url, username, password, category, download_path, timeout_seconds):
    return _list_utorrent_with_state(
        url,
        username,
        password,
        category,
        download_path,
        timeout_seconds,
        include_completed=False,
    )


def _list_completed_utorrent(url, username, password, category, download_path, timeout_seconds):
    return _list_utorrent_with_state(
        url,
        username,
        password,
        category,
        download_path,
        timeout_seconds,
        include_completed=True,
    )


def _remove_utorrent(url, username, password, torrent_hash, timeout_seconds, delete_files=False):
    gui_base = _utorrent_gui_base(url)
    session, token, error = _utorrent_authenticated_session(url, username, password, timeout_seconds)
    if error:
        return False, error
    action = "removedata" if delete_files else "remove"
    resp = _utorrent_request(
        session,
        gui_base,
        token,
        timeout_seconds,
        params={"action": action, "hash": str(torrent_hash or "").strip().lower()},
    )
    if resp.status_code != 200:
        return False, f"uTorrent returned {resp.status_code}."
    return True, "uTorrent remove request sent."


def _aria2_rpc_url(url):
    base = str(url or "").strip().rstrip("/")
    if not base:
        return ""
    parsed = urlparse(base)
    if not parsed.path or parsed.path == "/":
        return f"{base}/jsonrpc"
    return base


def _aria2_request(url, secret, method, params=None, timeout_seconds=15):
    rpc_url = _aria2_rpc_url(url)
    if not rpc_url:
        return False, "Aria2 URL is required.", None
    full_params = []
    if secret:
        full_params.append(f"token:{secret}")
    full_params.extend(list(params or []))
    payload = {
        "jsonrpc": "2.0",
        "id": f"aerofoil-{int(time.time() * 1000)}",
        "method": method,
        "params": full_params,
    }
    try:
        resp = requests.post(rpc_url, json=payload, timeout=timeout_seconds, headers={"User-Agent": DOWNLOADS_USER_AGENT})
    except Exception as exc:
        return False, f"Aria2 request failed: {exc}", None
    if resp.status_code != 200:
        return False, f"Aria2 returned {resp.status_code}.", None
    try:
        body = resp.json() or {}
    except Exception as exc:
        return False, f"Aria2 JSON parse error: {exc}", None
    if body.get("error"):
        msg = body.get("error", {}).get("message") or str(body.get("error"))
        return False, f"Aria2 error: {msg}", None
    return True, None, body.get("result")


def _test_aria2(url, secret=None, timeout_seconds=10):
    ok, error, version = _aria2_request(url, secret, "aria2.getVersion", [], timeout_seconds=timeout_seconds)
    if not ok:
        return False, error
    version_text = ""
    if isinstance(version, dict):
        version_text = str(version.get("version") or "").strip()
    if version_text:
        return True, f"Aria2 OK (v{version_text})."
    return True, "Aria2 OK."


def _add_aria2(url, secret, download_url, download_path, timeout_seconds):
    options = {}
    if download_path:
        options["dir"] = download_path
    ok, error, gid = _aria2_request(
        url,
        secret,
        "aria2.addUri",
        [[download_url], options],
        timeout_seconds=timeout_seconds,
    )
    if not ok:
        return False, error, None
    return True, "Aria2 accepted torrent.", str(gid or "")


def _parse_aria2_item(raw):
    gid = str((raw or {}).get("gid") or "").strip()
    if not gid:
        return None
    total = _to_int((raw or {}).get("totalLength"), 0)
    completed = _to_int((raw or {}).get("completedLength"), 0)
    progress = (completed / total * 100.0) if total > 0 else 0.0
    status = str((raw or {}).get("status") or "").strip().lower()
    mapped = {
        "active": "downloading",
        "waiting": "queued",
        "paused": "paused",
        "complete": "completed",
        "error": "error",
    }.get(status, status or "unknown")
    name = str((((raw or {}).get("bittorrent") or {}).get("info") or {}).get("name") or "").strip()
    if not name:
        files = (raw or {}).get("files") or []
        if files and isinstance(files[0], dict):
            name = os.path.basename(str(files[0].get("path") or "").strip()) or gid
    if not name:
        name = gid
    base_dir = str((raw or {}).get("dir") or "").strip()
    content_path = os.path.join(base_dir, name) if base_dir else name
    return {
        "hash": gid,
        "name": name,
        "path": content_path,
        "status": mapped,
        "progress": max(0.0, min(100.0, progress)),
        "size": total,
        "downloaded": completed,
        "down_speed": _to_int((raw or {}).get("downloadSpeed"), 0),
        "up_speed": _to_int((raw or {}).get("uploadSpeed"), 0),
        "eta": -1,
        "peers": 0,
        "seeders": 0,
        "leechers": 0,
        "category": None,
        "protocol": "torrent",
        "client_type": "aria2",
    }


def _list_aria2(url, secret, download_path, timeout_seconds, include_completed):
    if not str(download_path or "").strip():
        return []
    out = []
    ok, error, active = _aria2_request(url, secret, "aria2.tellActive", [], timeout_seconds=timeout_seconds)
    if not ok:
        logger.warning("Failed to load Aria2 active downloads: %s", error)
        active = []
    ok, error, waiting = _aria2_request(url, secret, "aria2.tellWaiting", [0, 1000], timeout_seconds=timeout_seconds)
    if not ok:
        waiting = []
    ok, error, stopped = _aria2_request(url, secret, "aria2.tellStopped", [0, 1000], timeout_seconds=timeout_seconds)
    if not ok:
        stopped = []
    for raw in list(active or []) + list(waiting or []) + list(stopped or []):
        item = _parse_aria2_item(raw)
        if not item:
            continue
        item_path = str(item.get("path") or "")
        if not item_path.lower().startswith(str(download_path).lower()):
            continue
        is_completed = item.get("status") == "completed" or item.get("progress", 0) >= 100.0
        if include_completed and not is_completed:
            continue
        if not include_completed and is_completed:
            continue
        out.append(item)
    return out


def _list_active_aria2(url, secret, download_path, timeout_seconds):
    return _list_aria2(url, secret, download_path, timeout_seconds, include_completed=False)


def _list_completed_aria2(url, secret, download_path, timeout_seconds):
    return _list_aria2(url, secret, download_path, timeout_seconds, include_completed=True)


def _remove_aria2(url, secret, torrent_hash, timeout_seconds, delete_files=False):
    ok, error, _ = _aria2_request(url, secret, "aria2.remove", [str(torrent_hash or "").strip()], timeout_seconds=timeout_seconds)
    if not ok:
        return False, error
    if delete_files:
        _aria2_request(url, secret, "aria2.removeDownloadResult", [str(torrent_hash or "").strip()], timeout_seconds=timeout_seconds)
    return True, "Aria2 remove request sent."


def _rqbit_request(url, username, password, path="", method="GET", timeout_seconds=15, json_payload=None, data_payload=None):
    base = str(url or "").strip().rstrip("/")
    endpoint = f"{base}{path}" if path.startswith("/") else f"{base}/{path}"
    session = _new_client_session(username, password)
    try:
        resp = session.request(method, endpoint, timeout=timeout_seconds, json=json_payload, data=data_payload)
    except Exception as exc:
        return False, f"RQBit request failed: {exc}", None
    if resp.status_code != 200:
        return False, f"RQBit returned {resp.status_code}.", None
    if not resp.text:
        return True, None, {}
    try:
        return True, None, (resp.json() or {})
    except Exception:
        return True, None, {}


def _test_rqbit(url, username=None, password=None, timeout_seconds=10):
    ok, error, payload = _rqbit_request(url, username, password, path="", timeout_seconds=timeout_seconds)
    if not ok:
        return False, error
    version = str((payload or {}).get("version") or "").strip()
    if version:
        return True, f"RQBit OK (v{version})."
    return True, "RQBit OK."


def _add_rqbit(url, username, password, download_url, timeout_seconds):
    ok, error, payload = _rqbit_request(
        url,
        username,
        password,
        path="/torrents?overwrite=true",
        method="POST",
        timeout_seconds=timeout_seconds,
        data_payload=download_url,
    )
    if not ok:
        return False, error, None
    details = (payload or {}).get("details") or {}
    info_hash = str(details.get("info_hash") or details.get("infoHash") or "").strip().lower()
    return True, "RQBit accepted torrent.", info_hash or _extract_magnet_hash(download_url)


def _parse_rqbit_item(raw):
    info_hash = str((raw or {}).get("info_hash") or "").strip().lower()
    if not info_hash:
        return None
    stats = (raw or {}).get("stats") or {}
    total = _to_int(stats.get("total_bytes"), 0)
    done = _to_int(stats.get("progress_bytes"), 0)
    progress = (done / total * 100.0) if total > 0 else 0.0
    state = str(stats.get("state") or "").strip().lower()
    mapped = "downloading"
    if state in ("paused", "queued", "waiting"):
        mapped = "paused"
    elif state in ("error", "invalid"):
        mapped = "error"
    elif bool(stats.get("finished")):
        mapped = "completed"
    elif state in ("live", "initializing"):
        mapped = "downloading"
    out_dir = str((raw or {}).get("output_folder") or "").strip()
    name = str((raw or {}).get("name") or "").strip() or info_hash
    live = stats.get("live") or {}
    down = _to_float(((live.get("download_speed") or {}).get("mbps")), 0.0)
    up = _to_float(((live.get("upload_speed") or {}).get("mbps")), 0.0)
    peers = _to_int((((live.get("snapshot") or {}).get("peer_stats") or {}).get("live")), 0)
    eta = _to_int(((((live.get("time_remaining") or {}).get("duration") or {}).get("secs"))), -1)
    return {
        "hash": info_hash,
        "name": name,
        "path": os.path.join(out_dir, name) if out_dir else name,
        "status": mapped,
        "progress": max(0.0, min(100.0, progress)),
        "size": total,
        "downloaded": done,
        "down_speed": int(down * 1048576),
        "up_speed": int(up * 1048576),
        "eta": eta,
        "peers": peers,
        "seeders": 0,
        "leechers": 0,
        "category": None,
        "protocol": "torrent",
        "client_type": "rqbit",
    }


def _list_rqbit(url, username, password, download_path, timeout_seconds, include_completed):
    if not str(download_path or "").strip():
        return []
    ok, error, payload = _rqbit_request(
        url,
        username,
        password,
        path="/torrents?with_stats=true",
        timeout_seconds=timeout_seconds,
    )
    if not ok:
        logger.warning("Failed to load RQBit downloads: %s", error)
        return []
    out = []
    for raw in (payload or {}).get("torrents") or []:
        item = _parse_rqbit_item(raw)
        if not item:
            continue
        if not str(item.get("path") or "").lower().startswith(str(download_path).lower()):
            continue
        is_completed = item.get("status") == "completed" or item.get("progress", 0) >= 100.0
        if include_completed and not is_completed:
            continue
        if not include_completed and is_completed:
            continue
        out.append(item)
    return out


def _list_active_rqbit(url, username, password, download_path, timeout_seconds):
    return _list_rqbit(url, username, password, download_path, timeout_seconds, include_completed=False)


def _list_completed_rqbit(url, username, password, download_path, timeout_seconds):
    return _list_rqbit(url, username, password, download_path, timeout_seconds, include_completed=True)


def _remove_rqbit(url, username, password, torrent_hash, timeout_seconds, delete_files=False):
    endpoint = "/delete" if delete_files else "/forget"
    ok, error, _ = _rqbit_request(
        url,
        username,
        password,
        path=f"/torrents/{str(torrent_hash or '').strip().lower()}{endpoint}",
        method="POST",
        timeout_seconds=timeout_seconds,
    )
    if not ok:
        return False, error
    return True, "RQBit remove request sent."


def _flood_api_base(url):
    base = str(url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.lower().endswith("/api"):
        return base
    return f"{base}/api"


def _flood_auth(session, api_base, username, password, timeout_seconds):
    resp = session.post(
        f"{api_base}/auth/authenticate",
        json={"username": username or "", "password": password or ""},
        timeout=timeout_seconds,
        headers={"User-Agent": DOWNLOADS_USER_AGENT},
    )
    if resp.status_code in (401, 403):
        return False, "Flood authentication failed."
    if resp.status_code != 200:
        return False, f"Flood returned {resp.status_code}."
    return True, None


def _flood_request(session, api_base, method, path, timeout_seconds=15, json_payload=None):
    resp = session.request(
        method,
        f"{api_base}{path}",
        timeout=timeout_seconds,
        headers={"User-Agent": DOWNLOADS_USER_AGENT, "Content-Type": "application/json"},
        json=json_payload,
    )
    if resp.status_code != 200:
        return False, f"Flood returned {resp.status_code}.", None
    if not resp.text:
        return True, None, {}
    try:
        return True, None, (resp.json() or {})
    except Exception:
        return True, None, {}


def _test_flood(url, username=None, password=None, timeout_seconds=10):
    api_base = _flood_api_base(url)
    if not api_base:
        return False, "Flood URL is required."
    session = requests.Session()
    ok, error = _flood_auth(session, api_base, username, password, timeout_seconds)
    if not ok:
        return False, error
    ok, error, _ = _flood_request(session, api_base, "GET", "/auth/verify", timeout_seconds=timeout_seconds)
    if not ok:
        return False, error
    return True, "Flood OK."


def _add_flood(url, username, password, download_url, category, timeout_seconds):
    api_base = _flood_api_base(url)
    session = requests.Session()
    ok, error = _flood_auth(session, api_base, username, password, timeout_seconds)
    if not ok:
        return False, error, None
    tags = [_build_rtorrent_managed_label(category)]
    payload = {"urls": [download_url], "tags": tags, "start": True}
    ok, error, _ = _flood_request(session, api_base, "POST", "/torrents/add-urls", timeout_seconds=timeout_seconds, json_payload=payload)
    if not ok:
        return False, error, None
    torrent_hash = _extract_magnet_hash(download_url) if download_url else None
    return True, "Flood accepted torrent.", torrent_hash


def _parse_flood_item(torrent_hash, raw, download_path=None):
    status_flags = [str(v).lower() for v in ((raw or {}).get("status") or [])]
    mapped = "queued"
    if any("error" in s for s in status_flags):
        mapped = "error"
    elif any(("seeding" in s or "complete" in s) for s in status_flags):
        mapped = "completed"
    elif any("stopped" in s for s in status_flags):
        mapped = "paused"
    elif any("downloading" in s for s in status_flags):
        mapped = "downloading"
    size = _to_int((raw or {}).get("sizeBytes"), 0)
    done = _to_int((raw or {}).get("bytesDone"), 0)
    progress = (done / size * 100.0) if size > 0 else 0.0
    directory = str((raw or {}).get("directory") or "").strip()
    name = str((raw or {}).get("name") or "").strip() or torrent_hash
    root = download_path or directory
    path = os.path.join(root, name) if root else name
    tags = (raw or {}).get("tags") or []
    category = tags[0] if tags else None
    return {
        "hash": str(torrent_hash or "").strip().lower(),
        "name": name,
        "path": path,
        "status": mapped,
        "progress": max(0.0, min(100.0, progress)),
        "size": size,
        "downloaded": done,
        "down_speed": 0,
        "up_speed": 0,
        "eta": _to_int((raw or {}).get("eta"), -1),
        "peers": 0,
        "seeders": 0,
        "leechers": 0,
        "category": category,
        "protocol": "torrent",
        "client_type": "flood",
    }


def _list_flood(url, username, password, category, download_path, timeout_seconds, include_completed):
    api_base = _flood_api_base(url)
    session = requests.Session()
    ok, error = _flood_auth(session, api_base, username, password, timeout_seconds)
    if not ok:
        logger.warning("Failed to load Flood downloads: %s", error)
        return []
    ok, error, payload = _flood_request(session, api_base, "GET", "/torrents", timeout_seconds=timeout_seconds)
    if not ok:
        logger.warning("Failed to load Flood downloads: %s", error)
        return []
    torrents = (payload or {}).get("torrents") or {}
    expected_tag = _build_rtorrent_managed_label(category) if category else None
    out = []
    for torrent_hash, raw in torrents.items():
        tags = [str(v) for v in ((raw or {}).get("tags") or [])]
        if expected_tag and expected_tag not in tags:
            continue
        if not expected_tag and not any(_is_deluge_managed_label(t) for t in tags):
            continue
        item = _parse_flood_item(torrent_hash, raw, download_path=download_path)
        is_completed = item.get("status") == "completed" or item.get("progress", 0) >= 100.0
        if include_completed and not is_completed:
            continue
        if not include_completed and is_completed:
            continue
        out.append(item)
    return out


def _list_active_flood(url, username, password, category, download_path, timeout_seconds):
    return _list_flood(url, username, password, category, download_path, timeout_seconds, include_completed=False)


def _list_completed_flood(url, username, password, category, download_path, timeout_seconds):
    return _list_flood(url, username, password, category, download_path, timeout_seconds, include_completed=True)


def _remove_flood(url, username, password, torrent_hash, timeout_seconds, delete_files=False):
    api_base = _flood_api_base(url)
    session = requests.Session()
    ok, error = _flood_auth(session, api_base, username, password, timeout_seconds)
    if not ok:
        return False, error
    payload = {"hashes": [str(torrent_hash or "").strip().lower()], "deleteData": bool(delete_files)}
    ok, error, _ = _flood_request(
        session,
        api_base,
        "POST",
        "/torrents/delete",
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if not ok:
        return False, error
    return True, "Flood remove request sent."


def _hadouken_jsonrpc(url, username, password, method, params=None, timeout_seconds=15):
    base = str(url or "").strip().rstrip("/")
    if not base:
        return False, "Hadouken URL is required.", None
    endpoint = f"{base}/api"
    payload = {"jsonrpc": "2.0", "id": f"aerofoil-{int(time.time()*1000)}", "method": method, "params": list(params or [])}
    session = _new_client_session(username, password)
    try:
        resp = session.post(endpoint, json=payload, timeout=timeout_seconds)
    except Exception as exc:
        return False, f"Hadouken request failed: {exc}", None
    if resp.status_code != 200:
        return False, f"Hadouken returned {resp.status_code}.", None
    try:
        data = resp.json() or {}
    except Exception as exc:
        return False, f"Hadouken JSON parse error: {exc}", None
    if data.get("error"):
        return False, f"Hadouken error: {data.get('error')}", None
    return True, None, data.get("result")


def _test_hadouken(url, username=None, password=None, timeout_seconds=10):
    ok, error, info = _hadouken_jsonrpc(url, username, password, "core.getSystemInfo", timeout_seconds=timeout_seconds)
    if not ok:
        return False, error
    version = ""
    if isinstance(info, dict):
        version = str(info.get("version") or "").strip()
    if version:
        return True, f"Hadouken OK (v{version})."
    return True, "Hadouken OK."


def _add_hadouken(url, username, password, download_url, timeout_seconds):
    ok, error, _ = _hadouken_jsonrpc(
        url,
        username,
        password,
        "webui.addTorrent",
        ["url", download_url, {"label": AEROFOIL_MANAGED_TAG}],
        timeout_seconds=timeout_seconds,
    )
    if not ok:
        return False, error, None
    return True, "Hadouken accepted torrent.", (_extract_magnet_hash(download_url) if download_url else None)


def _parse_hadouken_item(raw, download_path=None):
    if not isinstance(raw, list) or len(raw) < 12:
        return None
    info_hash = str(raw[0] or "").strip().lower()
    if not info_hash:
        return None
    state = _to_int(raw[1], 0)
    name = str(raw[2] or "").strip() or info_hash
    size = _to_int(raw[3], 0)
    progress_permille = _to_float(raw[4], 0.0)
    progress = max(0.0, min(100.0, progress_permille / 10.0))
    downloaded = _to_int(raw[5], int(size * (progress / 100.0)))
    down = _to_int(raw[9], 0)
    label = str(raw[11] or "").strip()
    save_path = str(raw[26] or "").strip() if len(raw) > 26 else ""
    path_root = download_path or save_path
    mapped = "queued"
    if state & 1:
        mapped = "downloading"
    elif state & 32:
        mapped = "paused"
    elif progress >= 100.0:
        mapped = "completed"
    return {
        "hash": info_hash,
        "name": name,
        "path": os.path.join(path_root, name) if path_root else name,
        "status": mapped,
        "progress": progress,
        "size": size,
        "downloaded": downloaded,
        "down_speed": down,
        "up_speed": 0,
        "eta": -1,
        "peers": 0,
        "seeders": 0,
        "leechers": 0,
        "category": label,
        "protocol": "torrent",
        "client_type": "hadouken",
    }


def _list_hadouken(url, username, password, category, download_path, timeout_seconds, include_completed):
    ok, error, payload = _hadouken_jsonrpc(url, username, password, "webui.list", timeout_seconds=timeout_seconds)
    if not ok:
        logger.warning("Failed to load Hadouken downloads: %s", error)
        return []
    rows = (payload or {}).get("torrents") if isinstance(payload, dict) else payload
    rows = rows or []
    out = []
    for raw in rows:
        item = _parse_hadouken_item(raw, download_path=download_path)
        if not item:
            continue
        label = str(item.get("category") or "").strip()
        if category and label != _build_rtorrent_managed_label(category):
            continue
        if not category and label and not _is_deluge_managed_label(label):
            continue
        is_completed = item.get("progress", 0) >= 100.0 or item.get("status") == "completed"
        if include_completed and not is_completed:
            continue
        if not include_completed and is_completed:
            continue
        out.append(item)
    return out


def _list_active_hadouken(url, username, password, category, download_path, timeout_seconds):
    return _list_hadouken(url, username, password, category, download_path, timeout_seconds, include_completed=False)


def _list_completed_hadouken(url, username, password, category, download_path, timeout_seconds):
    return _list_hadouken(url, username, password, category, download_path, timeout_seconds, include_completed=True)


def _remove_hadouken(url, username, password, torrent_hash, timeout_seconds, delete_files=False):
    method = "webui.perform"
    action = "removedata" if delete_files else "remove"
    ok, error, _ = _hadouken_jsonrpc(url, username, password, method, [action, [str(torrent_hash or "").strip().lower()]], timeout_seconds=timeout_seconds)
    if not ok:
        return False, error
    return True, "Hadouken remove request sent."


def _tribler_base(url):
    return str(url or "").strip().rstrip("/")


def _tribler_request(url, api_key, method, path, timeout_seconds=15, json_payload=None):
    base = _tribler_base(url)
    if not base:
        return False, "Tribler URL is required.", None
    headers = {"User-Agent": DOWNLOADS_USER_AGENT, "X-Api-Key": api_key or "", "Content-Type": "application/json"}
    try:
        resp = requests.request(method, f"{base}{path}", headers=headers, timeout=timeout_seconds, json=json_payload)
    except Exception as exc:
        return False, f"Tribler request failed: {exc}", None
    if resp.status_code == 401:
        return False, "Tribler authentication failed.", None
    if resp.status_code not in (200, 201):
        return False, f"Tribler returned {resp.status_code}.", None
    if not resp.text:
        return True, None, {}
    try:
        return True, None, (resp.json() or {})
    except Exception:
        return True, None, {}


def _test_tribler(url, api_key=None, timeout_seconds=10):
    ok, error, payload = _tribler_request(url, api_key, "GET", "/api/settings", timeout_seconds=timeout_seconds)
    if not ok:
        return False, error
    _ = payload
    return True, "Tribler OK."


def _add_tribler(url, api_key, download_url, download_path, timeout_seconds):
    payload = {"uri": download_url}
    if download_path:
        payload["destination"] = download_path
    ok, error, data = _tribler_request(url, api_key, "PUT", "/api/downloads", timeout_seconds=timeout_seconds, json_payload=payload)
    if not ok:
        return False, error, None
    info_hash = str((data or {}).get("infohash") or "").strip().lower()
    return True, "Tribler accepted torrent.", info_hash or _extract_magnet_hash(download_url)


def _parse_tribler_item(raw, download_path=None):
    info_hash = str((raw or {}).get("infohash") or "").strip().lower()
    if not info_hash:
        return None
    name = str((raw or {}).get("name") or "").strip() or info_hash
    progress = max(0.0, min(100.0, _to_float((raw or {}).get("progress"), 0.0) * 100.0))
    status_raw = str((raw or {}).get("status") or "").strip().lower()
    mapped = "downloading"
    if any(k in status_raw for k in ("error", "stopped")):
        mapped = "paused"
    elif "seeding" in status_raw or progress >= 100.0:
        mapped = "completed"
    size = _to_int((raw or {}).get("size"), 0)
    downloaded = int(size * progress / 100.0) if size > 0 else _to_int((raw or {}).get("downloaded"), 0)
    save_path = str((raw or {}).get("destination") or "").strip()
    root = download_path or save_path
    return {
        "hash": info_hash,
        "name": name,
        "path": os.path.join(root, name) if root else name,
        "status": mapped,
        "progress": progress,
        "size": size,
        "downloaded": downloaded,
        "down_speed": _to_int((raw or {}).get("speed_down"), 0),
        "up_speed": _to_int((raw or {}).get("speed_up"), 0),
        "eta": -1,
        "peers": _to_int((raw or {}).get("num_peers"), 0),
        "seeders": _to_int((raw or {}).get("num_seeds"), 0),
        "leechers": 0,
        "category": AEROFOIL_MANAGED_TAG,
        "protocol": "torrent",
        "client_type": "tribler",
    }


def _list_tribler(url, api_key, _category, download_path, timeout_seconds, include_completed):
    ok, error, payload = _tribler_request(url, api_key, "GET", "/api/downloads", timeout_seconds=timeout_seconds)
    if not ok:
        logger.warning("Failed to load Tribler downloads: %s", error)
        return []
    out = []
    for raw in (payload or {}).get("downloads") or []:
        item = _parse_tribler_item(raw, download_path=download_path)
        if not item:
            continue
        if download_path and not str(item.get("path") or "").lower().startswith(str(download_path).lower()):
            continue
        is_completed = item.get("status") == "completed" or item.get("progress", 0) >= 100.0
        if include_completed and not is_completed:
            continue
        if not include_completed and is_completed:
            continue
        out.append(item)
    return out


def _list_active_tribler(url, api_key, category, download_path, timeout_seconds):
    return _list_tribler(url, api_key, category, download_path, timeout_seconds, include_completed=False)


def _list_completed_tribler(url, api_key, category, download_path, timeout_seconds):
    return _list_tribler(url, api_key, category, download_path, timeout_seconds, include_completed=True)


def _remove_tribler(url, api_key, torrent_hash, timeout_seconds, delete_files=False):
    payload = {"remove_data": bool(delete_files)}
    ok, error, _ = _tribler_request(
        url,
        api_key,
        "DELETE",
        f"/api/downloads/{str(torrent_hash or '').strip().lower()}",
        timeout_seconds=timeout_seconds,
        json_payload=payload,
    )
    if not ok:
        return False, error
    return True, "Tribler remove request sent."


def _test_blackhole(path):
    folder = str(path or "").strip()
    if not folder:
        return False, "Blackhole watch folder is required in URL field."
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception as exc:
        return False, f"Blackhole folder error: {exc}"
    if not os.path.isdir(folder):
        return False, "Blackhole folder is not a directory."
    return True, f"Blackhole OK ({folder})."


def _add_blackhole(download_url, torrent_content, download_path):
    folder = str(download_path or "").strip()
    if not folder:
        return False, "Blackhole download path is required.", None
    os.makedirs(folder, exist_ok=True)
    stamp = int(time.time() * 1000)
    if torrent_content:
        target = os.path.join(folder, f"aerofoil_{stamp}.torrent")
        with open(target, "wb") as handle:
            handle.write(torrent_content)
        return True, "Blackhole wrote torrent file.", os.path.basename(target)
    if str(download_url or "").startswith("magnet:"):
        target = os.path.join(folder, f"aerofoil_{stamp}.magnet")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(download_url)
        return True, "Blackhole wrote magnet file.", os.path.basename(target)
    target = os.path.join(folder, f"aerofoil_{stamp}.url")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(str(download_url or ""))
    return True, "Blackhole wrote URL file.", os.path.basename(target)


def _test_downloadstation_torrent(url, username=None, password=None, timeout_seconds=10):
    from app.downloads.usenet_client import test_downloadstation
    return test_downloadstation(url, username=username, password=password, timeout_seconds=timeout_seconds)


def _add_downloadstation_torrent(url, username, password, download_url, timeout_seconds):
    from app.downloads.usenet_client import _downloadstation_auth, _downloadstation_task
    ok, error, sid = _downloadstation_auth(url, username, password, timeout_seconds=timeout_seconds)
    if not ok:
        return False, error, None
    ok, error, _ = _downloadstation_task(url, sid, "create", timeout_seconds=timeout_seconds, extra={"uri": download_url})
    if not ok:
        return False, error, None
    return True, "Download Station accepted torrent.", f"ds-{int(time.time()*1000)}"


def _list_downloadstation_torrent(url, username, password, category, download_path, timeout_seconds, include_completed):
    from app.downloads.usenet_client import _downloadstation_auth, _downloadstation_task
    _ = category
    ok, error, sid = _downloadstation_auth(url, username, password, timeout_seconds=timeout_seconds)
    if not ok:
        logger.warning("Failed to load Download Station torrents: %s", error)
        return []
    ok, error, data = _downloadstation_task(url, sid, "list", timeout_seconds=timeout_seconds, extra={"additional": "detail,transfer"})
    if not ok:
        logger.warning("Failed to load Download Station torrents: %s", error)
        return []
    out = []
    for item in (data.get("tasks") or []):
        if str(item.get("type") or "").lower() != "bt":
            continue
        status_raw = str(item.get("status") or "").lower()
        size = _to_int(item.get("size"), 0)
        transfer = (item.get("additional") or {}).get("transfer") or {}
        downloaded = _to_int(transfer.get("size_downloaded"), 0)
        progress = (downloaded / size * 100.0) if size > 0 else 0.0
        mapped = "downloading"
        if status_raw in ("finished", "seeding"):
            mapped = "completed"
        elif status_raw in ("paused", "waiting"):
            mapped = "paused"
        elif status_raw in ("error",):
            mapped = "error"
        path = str(((item.get("additional") or {}).get("detail") or {}).get("destination") or "")
        name = str(item.get("title") or item.get("id") or "")
        full_path = os.path.join(path, name) if path else name
        if download_path and not full_path.lower().startswith(str(download_path).lower()):
            continue
        is_completed = mapped == "completed" or progress >= 100.0
        if include_completed and not is_completed:
            continue
        if not include_completed and is_completed:
            continue
        out.append({
            "hash": str(item.get("id") or ""),
            "name": name,
            "path": full_path,
            "status": mapped,
            "progress": progress,
            "size": size,
            "downloaded": downloaded,
            "down_speed": _to_int(transfer.get("speed_download"), 0),
            "up_speed": _to_int(transfer.get("speed_upload"), 0),
            "eta": -1,
            "peers": 0,
            "seeders": 0,
            "leechers": 0,
            "category": None,
            "protocol": "torrent",
            "client_type": "downloadstation",
        })
    return out


def _list_active_downloadstation_torrent(url, username, password, category, download_path, timeout_seconds):
    return _list_downloadstation_torrent(url, username, password, category, download_path, timeout_seconds, include_completed=False)


def _list_completed_downloadstation_torrent(url, username, password, category, download_path, timeout_seconds):
    return _list_downloadstation_torrent(url, username, password, category, download_path, timeout_seconds, include_completed=True)


def _remove_downloadstation_torrent(url, username, password, torrent_hash, timeout_seconds, delete_files=False):
    from app.downloads.usenet_client import _downloadstation_auth, _downloadstation_task
    _ = delete_files
    ok, error, sid = _downloadstation_auth(url, username, password, timeout_seconds=timeout_seconds)
    if not ok:
        return False, error
    ok, error, _ = _downloadstation_task(url, sid, "delete", timeout_seconds=timeout_seconds, extra={"id": torrent_hash, "force_complete": "false"})
    if not ok:
        return False, error
    return True, "Download Station task removed."


def _freebox_base(url):
    return str(url or "").strip().rstrip("/")


def _freebox_request(url, app_id, app_token, path, method="GET", timeout_seconds=15, json_payload=None, data_payload=None, session_token=None):
    base = _freebox_base(url)
    headers = {"User-Agent": DOWNLOADS_USER_AGENT, "Content-Type": "application/json"}
    if session_token:
        headers["X-Fbx-App-Auth"] = session_token
    resp = requests.request(method, f"{base}{path}", headers=headers, timeout=timeout_seconds, json=json_payload, data=data_payload)
    if resp.status_code != 200:
        return False, f"Freebox returned {resp.status_code}.", None
    body = resp.json() if resp.content else {}
    if not body.get("success"):
        return False, f"Freebox API error: {body.get('msg') or body.get('error_code') or 'request failed'}", None
    return True, None, body.get("result")


def _freebox_session(url, app_id, app_token, timeout_seconds=10):
    ok, error, login = _freebox_request(url, app_id, app_token, "/login", timeout_seconds=timeout_seconds)
    if not ok:
        return False, error, None
    challenge = str((login or {}).get("challenge") or "")
    if not challenge:
        return False, "Freebox challenge not found.", None
    secret = str(app_token or "").encode("ascii", errors="ignore")
    passwd = hmac.new(secret, challenge.encode("ascii", errors="ignore"), hashlib.sha1).hexdigest()
    payload = {"app_id": app_id or "", "password": passwd}
    ok, error, session = _freebox_request(url, app_id, app_token, "/login/session", method="POST", timeout_seconds=timeout_seconds, json_payload=payload)
    if not ok:
        return False, error, None
    token = str((session or {}).get("session_token") or "")
    if not token:
        return False, "Freebox session token not found.", None
    return True, None, token


def _test_freebox(url, app_id=None, app_token=None, timeout_seconds=10):
    ok, error, token = _freebox_session(url, app_id, app_token, timeout_seconds=timeout_seconds)
    if not ok:
        return False, error
    ok, error, _ = _freebox_request(url, app_id, app_token, "/downloads/", timeout_seconds=timeout_seconds, session_token=token)
    if not ok:
        return False, error
    return True, "Freebox Download OK."


def _add_freebox(url, app_id, app_token, download_url, timeout_seconds):
    ok, error, token = _freebox_session(url, app_id, app_token, timeout_seconds=timeout_seconds)
    if not ok:
        return False, error, None
    form = {"download_url": download_url}
    ok, error, result = _freebox_request(url, app_id, app_token, "/downloads/add", method="POST", timeout_seconds=timeout_seconds, data_payload=form, session_token=token)
    if not ok:
        return False, error, None
    return True, "Freebox accepted torrent.", str((result or {}).get("id") or "")


def _list_freebox(url, app_id, app_token, category, download_path, timeout_seconds, include_completed):
    _ = category
    ok, error, token = _freebox_session(url, app_id, app_token, timeout_seconds=timeout_seconds)
    if not ok:
        logger.warning("Failed to load Freebox downloads: %s", error)
        return []
    ok, error, tasks = _freebox_request(url, app_id, app_token, "/downloads/", timeout_seconds=timeout_seconds, session_token=token)
    if not ok:
        logger.warning("Failed to load Freebox downloads: %s", error)
        return []
    out = []
    for item in tasks or []:
        if str(item.get("type") or "").lower() != "bt":
            continue
        status_raw = str(item.get("status") or "").lower()
        mapped = "downloading"
        if status_raw in ("done", "seeding"):
            mapped = "completed"
        elif status_raw in ("stopped", "stopping", "queued"):
            mapped = "paused"
        elif status_raw in ("error",):
            mapped = "error"
        size = _to_int(item.get("size"), 0)
        progress = _to_float(item.get("rx_pct"), 0.0) / 100.0
        name = str(item.get("name") or item.get("id") or "")
        directory = str(item.get("download_dir") or "")
        full_path = os.path.join(directory, name) if directory else name
        if download_path and not full_path.lower().startswith(str(download_path).lower()):
            continue
        is_completed = mapped == "completed" or progress >= 100.0
        if include_completed and not is_completed:
            continue
        if not include_completed and is_completed:
            continue
        out.append({
            "hash": str(item.get("id") or ""),
            "name": name,
            "path": full_path,
            "status": mapped,
            "progress": max(0.0, min(100.0, progress)),
            "size": size,
            "downloaded": int(size * (progress / 100.0)) if size > 0 else 0,
            "down_speed": _to_int(item.get("rx_bytes"), 0),
            "up_speed": _to_int(item.get("tx_bytes"), 0),
            "eta": _to_int(item.get("eta"), -1),
            "peers": 0,
            "seeders": 0,
            "leechers": 0,
            "category": None,
            "protocol": "torrent",
            "client_type": "freeboxdownload",
        })
    return out


def _list_active_freebox(url, app_id, app_token, category, download_path, timeout_seconds):
    return _list_freebox(url, app_id, app_token, category, download_path, timeout_seconds, include_completed=False)


def _list_completed_freebox(url, app_id, app_token, category, download_path, timeout_seconds):
    return _list_freebox(url, app_id, app_token, category, download_path, timeout_seconds, include_completed=True)


def _remove_freebox(url, app_id, app_token, torrent_hash, timeout_seconds, delete_files=False):
    ok, error, token = _freebox_session(url, app_id, app_token, timeout_seconds=timeout_seconds)
    if not ok:
        return False, error
    suffix = "/erase" if delete_files else ""
    ok, error, _ = _freebox_request(url, app_id, app_token, f"/downloads/{torrent_hash}{suffix}", method="DELETE", timeout_seconds=timeout_seconds, session_token=token)
    if not ok:
        return False, error
    return True, "Freebox task removed."


def _test_qbittorrent(url, username=None, password=None, timeout_seconds=10):
    base = url.rstrip("/")
    session = _new_client_session()
    if not _login_qbittorrent(session, base, username, password, timeout_seconds):
        return False, "qBittorrent login failed."
    version_resp = session.get(f"{base}/api/v2/app/version", timeout=timeout_seconds)
    if version_resp.status_code != 200:
        return False, f"qBittorrent returned {version_resp.status_code}."
    version_text = str(version_resp.text or "").strip()
    if version_text.lower().startswith("v"):
        return True, f"qBittorrent OK ({version_text})."
    return True, f"qBittorrent OK (v{version_text})."


def _test_transmission(url, username=None, password=None, timeout_seconds=10):
    base = url.rstrip("/")
    session = _new_client_session(username, password)

    payload = {"method": "session-get"}
    resp = _transmission_request(session, base, payload, timeout_seconds)
    if resp.status_code != 200:
        return False, f"Transmission returned {resp.status_code}."
    return True, "Transmission OK."


def _deluge_json_rpc(url, password, method, params=None, timeout_seconds=10):
    base = url.rstrip("/")
    session = _new_client_session()
    if password is None:
        password = ""
    payload = {
        "method": method,
        "params": params or [],
        "id": 1
    }
    resp = session.post(f"{base}/json", json=payload, timeout=timeout_seconds)
    if resp.status_code != 200:
        return False, resp
    data = resp.json()
    if data.get("error"):
        return False, data
    return True, data.get("result")


def _deluge_login(url, password, timeout_seconds=10):
    ok, result = _deluge_json_rpc(url, password, "auth.login", [password], timeout_seconds=timeout_seconds)
    if not ok:
        return False, result
    return bool(result), result


def _test_deluge(url, password=None, timeout_seconds=10):
    ok, result = _deluge_login(url, password, timeout_seconds=timeout_seconds)
    if not ok or not result:
        return False, "Deluge login failed."
    ok, result = _deluge_json_rpc(url, password, "daemon.info", [], timeout_seconds=timeout_seconds)
    if not ok:
        return False, "Deluge returned an error."
    version = None
    if isinstance(result, dict):
        version = result.get("daemon_version") or result.get("version")
    return True, f"Deluge OK{f' (v{version})' if version else ''}."


def _test_rtorrent(url, username=None, password=None, timeout_seconds=10):
    ok, error, version = _rtorrent_xmlrpc(
        url,
        "system.client_version",
        [],
        timeout_seconds=timeout_seconds,
        username=username,
        password=password,
    )
    if not ok:
        return False, error or "rTorrent returned an error."
    version_text = str(version or "").strip()
    return True, f"rTorrent OK{f' (v{version_text})' if version_text else ''}."


def _add_deluge(url, password, download_url, torrent_content, category, download_path, timeout_seconds, update_only, dlc_only=False, exclude_russian=False, expected_update_number=None, expected_version=None):
    ok, logged_in = _deluge_login(url, password, timeout_seconds=timeout_seconds)
    if not ok or not logged_in:
        return False, "Deluge login failed.", None

    options = {}
    if update_only or dlc_only:
        options["add_paused"] = True
    if download_path:
        options["download_location"] = download_path

    managed_label = _build_deluge_managed_label(category)
    ok, result = _deluge_json_rpc(url, password, "label.add", [managed_label], timeout_seconds=timeout_seconds)
    if not ok:
        err = result.get("error") if isinstance(result, dict) else None
        if not err or "already" not in str(err).lower():
            return False, "Deluge label error.", None
    options["label"] = managed_label

    if torrent_content:
        metainfo = get_metainfo_base64(torrent_content)
        filename = f"aerofoil_{int(time.time())}.torrent"
        ok, result = _deluge_json_rpc(
            url,
            password,
            "core.add_torrent_file",
            [filename, metainfo, options],
            timeout_seconds=timeout_seconds
        )
    else:
        ok, result = _deluge_json_rpc(
            url,
            password,
            "core.add_torrent_url",
            [download_url, options],
            timeout_seconds=timeout_seconds
        )
    if not ok:
        return False, "Deluge returned an error.", None
    torrent_hash = None
    if isinstance(result, str):
        torrent_hash = result
    elif isinstance(result, dict):
        torrent_hash = result.get("id")
    if not torrent_hash:
        torrent_hash = _extract_magnet_hash(download_url) if download_url else None

    if (update_only or dlc_only) and torrent_hash:
        file_names = poll_update_file_names(
            lambda: _fetch_deluge_file_names(url, password, torrent_hash, timeout_seconds),
            sleep_fn=time.sleep,
        )
        if update_only:
            keep_indices = get_matching_update_indices(
                file_names,
                expected_update_number=expected_update_number,
                expected_version=expected_version,
                exclude_russian=exclude_russian,
            )
            no_match_error = TORRENT_UPDATE_SELECTION_ERROR
        else:
            keep_indices = get_matching_dlc_indices(
                file_names,
                exclude_russian=exclude_russian,
            )
            no_match_error = TORRENT_DLC_SELECTION_ERROR
        if not keep_indices:
            _remove_deluge(url, password, torrent_hash, timeout_seconds)
            return False, no_match_error, None
        priorities = [0] * len(file_names or [])
        for idx in keep_indices:
            if idx < len(priorities):
                priorities[idx] = 1
        _deluge_json_rpc(
            url,
            password,
            "core.set_torrent_file_priorities",
            [torrent_hash, priorities],
            timeout_seconds=timeout_seconds
        )
        _deluge_json_rpc(
            url,
            password,
            "core.resume_torrent",
            [[torrent_hash]],
            timeout_seconds=timeout_seconds
        )
    return True, "Deluge accepted torrent.", torrent_hash


def _add_rtorrent(
    url,
    download_url,
    username,
    password,
    category,
    download_path,
    timeout_seconds,
    update_only,
    dlc_only=False,
    exclude_russian=False,
    expected_update_number=None,
    expected_version=None,
    expected_name=None,
):
    preflight_files = None
    if update_only:
        preflight_files = _get_torrent_file_list(download_url, timeout_seconds)
        if not preflight_has_matching_update(
            preflight_files,
            expected_update_number=expected_update_number,
            expected_version=expected_version,
            exclude_russian=exclude_russian,
        ):
            return False, TORRENT_UPDATE_SELECTION_ERROR, None
    elif dlc_only:
        preflight_files = _get_torrent_file_list(download_url, timeout_seconds)
        if not preflight_has_matching_dlc(
            preflight_files,
            exclude_russian=exclude_russian,
        ):
            return False, TORRENT_DLC_SELECTION_ERROR, None

    managed_label = _build_rtorrent_managed_label(category)
    add_args = ["", download_url]
    add_args.extend(_build_rtorrent_add_commands(category, download_path))
    ok, error, add_result = _rtorrent_xmlrpc(
        url,
        "load.start_verbose",
        add_args,
        timeout_seconds=timeout_seconds,
        username=username,
        password=password,
    )
    if not ok:
        return False, error or "rTorrent add failed.", None

    torrent_hash = _compute_torrent_infohash(download_url, timeout_seconds) or _extract_magnet_hash(download_url)
    if not torrent_hash:
        torrent_hash = _extract_rtorrent_hash(add_result)
    if not torrent_hash:
        torrent_hash = _find_recent_rtorrent_hash_by_name(
            url,
            expected_name,
            username,
            password,
            timeout_seconds,
        )
    if (update_only or dlc_only) and torrent_hash:
        _rtorrent_xmlrpc(url, "d.stop", [torrent_hash], timeout_seconds=timeout_seconds, username=username, password=password)
    if torrent_hash:
        _rtorrent_xmlrpc(url, "d.custom1.set", [torrent_hash, managed_label], timeout_seconds=timeout_seconds, username=username, password=password)
        if download_path:
            _rtorrent_xmlrpc(url, "d.directory.set", [torrent_hash, download_path], timeout_seconds=timeout_seconds, username=username, password=password)
    if update_only or dlc_only:
        if not torrent_hash:
            return False, "Unable to resolve torrent hash for file selection.", None
        if update_only:
            selected = _select_rtorrent_highest_version(
                url,
                torrent_hash,
                username,
                password,
                timeout_seconds,
                exclude_russian=exclude_russian,
                expected_update_number=expected_update_number,
                expected_version=expected_version,
            )
            no_match_error = TORRENT_UPDATE_SELECTION_ERROR
        else:
            selected = _select_rtorrent_dlc_files(
                url,
                torrent_hash,
                username,
                password,
                timeout_seconds,
                exclude_russian=exclude_russian,
            )
            no_match_error = TORRENT_DLC_SELECTION_ERROR
        if not selected:
            _remove_rtorrent(url, torrent_hash, username, password, timeout_seconds, delete_files=True)
            return False, no_match_error, None
        _rtorrent_xmlrpc(url, "d.start", [torrent_hash], timeout_seconds=timeout_seconds, username=username, password=password)
    return True, "rTorrent accepted torrent.", torrent_hash


def _add_qbittorrent(url, username, password, download_url, torrent_content, category, download_path, timeout_seconds, expected_name, update_only, dlc_only=False, exclude_russian=False, expected_update_number=None, expected_version=None):
    base = url.rstrip("/")
    session = _new_client_session()
    if not _login_qbittorrent(session, base, username, password, timeout_seconds):
        return False, "qBittorrent login failed.", None

    data = {}
    files = None
    if torrent_content:
        files = {"torrents": (f"aerofoil_{int(time.time())}.torrent", torrent_content)}
    else:
        data["urls"] = download_url
    if update_only or dlc_only:
        if torrent_content:
            preflight_files = _get_torrent_file_list_from_content(torrent_content)
        else:
            preflight_files = _get_torrent_file_list(download_url, timeout_seconds)
        if update_only:
            if not preflight_has_matching_update(
                preflight_files,
                expected_update_number=expected_update_number,
                expected_version=expected_version,
                exclude_russian=exclude_russian,
            ):
                return False, TORRENT_UPDATE_SELECTION_ERROR, None
        else:
            if not preflight_has_matching_dlc(
                preflight_files,
                exclude_russian=exclude_russian,
            ):
                return False, TORRENT_DLC_SELECTION_ERROR, None
    if category:
        data["category"] = category
    tags = _build_qbittorrent_tags(category)
    if tags:
        data["tags"] = tags
    if update_only or dlc_only:
        data["paused"] = "true"
    if download_path:
        data["savepath"] = download_path
    added_at = int(time.time())
    infohash_v1 = _compute_torrent_infohash_from_content(torrent_content) if torrent_content else _compute_torrent_infohash(download_url, timeout_seconds)
    if (update_only or dlc_only) and infohash_v1:
        logger.info("Computed torrent infohash_v1: %s", infohash_v1)
    resp = session.post(f"{base}/api/v2/torrents/add", data=data, files=files, timeout=timeout_seconds)
    if resp.status_code not in (200, 202):
        return False, f"qBittorrent returned {resp.status_code}.", None
    add_response_text = str(resp.text or "").strip()
    if add_response_text and add_response_text.lower() not in ("ok", "ok."):
        accepted = False
        if add_response_text.startswith("{") and add_response_text.endswith("}"):
            try:
                add_payload = json.loads(add_response_text)
            except Exception:
                add_payload = None
            if isinstance(add_payload, dict):
                added_ids = add_payload.get("added_torrent_ids") or []
                success_count = add_payload.get("success_count")
                failure_count = add_payload.get("failure_count")
                if isinstance(added_ids, list) and len(added_ids) > 0:
                    accepted = True
                if not accepted and isinstance(success_count, int) and success_count > 0:
                    accepted = True
                if accepted and isinstance(failure_count, int) and failure_count < 0:
                    accepted = False
        if not accepted:
            return False, f"qBittorrent rejected torrent add: {add_response_text}", None
    torrent_hash = _extract_magnet_hash(download_url) if download_url else None
    if torrent_hash:
        resolved_hash = None
        for _ in range(10):
            normalized = _normalize_hash(session, base, torrent_hash, timeout_seconds)
            if normalized:
                resolved_hash = normalized
                break
            time.sleep(1)
        torrent_hash = resolved_hash
    if infohash_v1 and ((update_only or dlc_only) or not torrent_hash):
        torrent_hash = _find_qbittorrent_hash_by_infohash(session, base, infohash_v1, category, added_at, timeout_seconds)
        if torrent_hash:
            logger.info("Matched torrent hash %s for infohash_v1 %s", torrent_hash, infohash_v1)
    if not torrent_hash:
        for _ in range(10):
            torrent_hash = _find_recent_qbittorrent_hash(
                session,
                base,
                expected_name,
                category,
                timeout_seconds,
                added_after=added_at,
                require_match=True,
            )
            if torrent_hash:
                break
            time.sleep(1)
    if (update_only or dlc_only) and torrent_hash:
        normalized = _normalize_hash(session, base, torrent_hash, timeout_seconds)
        if normalized:
            torrent_hash = normalized
    if update_only or dlc_only:
        if not torrent_hash:
            return False, "Unable to resolve torrent hash for file selection.", None
        if update_only:
            logger.info("Selecting highest version for torrent %s", torrent_hash)
            selected = _select_qbittorrent_highest_version(
                session,
                base,
                torrent_hash,
                timeout_seconds,
                exclude_russian,
                expected_update_number=expected_update_number,
                expected_version=expected_version
            )
            no_match_error = TORRENT_UPDATE_SELECTION_ERROR
        else:
            logger.info("Selecting DLC files for torrent %s", torrent_hash)
            selected = _select_qbittorrent_dlc_files(
                session,
                base,
                torrent_hash,
                timeout_seconds,
                exclude_russian,
            )
            no_match_error = TORRENT_DLC_SELECTION_ERROR
        if not selected:
            _remove_qbittorrent_with_session(session, base, torrent_hash, timeout_seconds)
            return False, no_match_error, None
        _resume_qbittorrent(session, base, torrent_hash, timeout_seconds)
    elif not torrent_hash:
        return False, "qBittorrent did not report the added torrent.", None
    elif exclude_russian and torrent_hash:
        _exclude_qbittorrent_russian(session, base, torrent_hash, timeout_seconds)
    return True, "qBittorrent accepted torrent.", torrent_hash


def _add_transmission(url, username, password, download_url, torrent_content, category, download_path, timeout_seconds, expected_name, update_only, dlc_only=False, exclude_russian=False, expected_update_number=None, expected_version=None):
    base = url.rstrip("/")
    session = _new_client_session(username, password)

    preflight_files = None
    if update_only or dlc_only:
        if torrent_content:
            preflight_files = _get_torrent_file_list_from_content(torrent_content)
        else:
            preflight_files = _get_torrent_file_list(download_url, timeout_seconds)
        if update_only:
            if not preflight_has_matching_update(
                preflight_files,
                expected_update_number=expected_update_number,
                expected_version=expected_version,
                exclude_russian=exclude_russian,
            ):
                return False, TORRENT_UPDATE_SELECTION_ERROR, None
        else:
            if not preflight_has_matching_dlc(
                preflight_files,
                exclude_russian=exclude_russian,
            ):
                return False, TORRENT_DLC_SELECTION_ERROR, None

    if torrent_content:
        payload = {"method": "torrent-add", "arguments": {"metainfo": get_metainfo_base64(torrent_content)}}
    else:
        payload = {"method": "torrent-add", "arguments": {"filename": download_url}}
    if update_only or dlc_only:
        payload["arguments"]["paused"] = True
    labels = [AEROFOIL_MANAGED_TAG]
    if category:
        labels.append(category)
    payload["arguments"]["labels"] = list(dict.fromkeys(labels))
    if download_path:
        payload["arguments"]["download-dir"] = download_path

    def _request(payload_body):
        return _transmission_request(session, base, payload_body, timeout_seconds)

    resp = _request(payload)
    if resp.status_code != 200:
        return False, f"Transmission returned {resp.status_code}.", None
    body = resp.json() or {}
    # Transmission RPC returns HTTP 200 even on logical failures; the real status
    # is in the "result" field ("success" on success). Without this check a
    # rejected torrent-add surfaced as the misleading "Unable to resolve torrent
    # id for file selection." (and non-update adds falsely reported success).
    rpc_result = str(body.get("result") or "").strip()
    if rpc_result.lower() != "success":
        return False, f"Transmission rejected torrent: {rpc_result or 'missing result'}", None
    data = body.get("arguments", {})
    torrent = data.get("torrent-added") or data.get("torrent-duplicate") or {}
    torrent_hash = torrent.get("hashString") or (_extract_magnet_hash(download_url) if download_url else None)
    torrent_id = torrent.get("id") or torrent.get("hashString")

    if (update_only or dlc_only) and not torrent_id:
        return False, "Unable to resolve torrent id for file selection.", None
    if (update_only or dlc_only) and torrent_id:
        file_names = poll_update_file_names(
            lambda: _fetch_transmission_file_names(_request, torrent_id),
            sleep_fn=time.sleep,
        )
        if update_only:
            file_indices = get_matching_update_indices(
                file_names,
                expected_update_number=expected_update_number,
                expected_version=expected_version,
                exclude_russian=exclude_russian,
            )
            no_match_error = TORRENT_UPDATE_SELECTION_ERROR
        else:
            file_indices = get_matching_dlc_indices(
                file_names,
                exclude_russian=exclude_russian,
            )
            no_match_error = TORRENT_DLC_SELECTION_ERROR
        if not file_indices:
            if torrent_hash:
                _remove_transmission(url, username, password, torrent_hash, timeout_seconds)
            return False, no_match_error, None
        all_indices = list(range(len(file_names))) if file_names else []
        unwanted = [i for i in all_indices if i not in file_indices]
        set_payload = {
            "method": "torrent-set",
            "arguments": {
                "ids": [torrent_id],
                "files-wanted": file_indices,
                "files-unwanted": unwanted
            }
        }
        _request(set_payload)
        _request({"method": "torrent-start", "arguments": {"ids": [torrent_id]}})
    return True, "Transmission accepted torrent.", torrent_hash




def _is_path_within(path, base):
    if not path or not base:
        return False
    try:
        path_abs = os.path.abspath(path)
        base_abs = os.path.abspath(base)
        return os.path.commonpath([path_abs, base_abs]) == base_abs
    except Exception:
        return False


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _qb_is_active(item):
    progress = _to_float(item.get("progress"), 0.0)
    if progress < 1:
        return True
    if _to_int(item.get("dlspeed"), 0) > 0:
        return True
    return False


def _list_active_qbittorrent(url, username, password, category, download_path, timeout_seconds):
    base = url.rstrip("/")
    session = _new_client_session()
    if not _login_qbittorrent(session, base, username, password, timeout_seconds):
        return []

    items = _fetch_qbittorrent_managed_items(
        session,
        base,
        timeout_seconds,
        extra_params={"sort": "dlspeed", "reverse": "true"},
    )
    if not items:
        return []
    active = []
    for item in items:
        tags = _parse_qbittorrent_tags(item.get("tags"))
        if not _has_managed_qbittorrent_tag(item.get("tags")):
            continue
        if category and item.get("category") != category and category not in tags:
            continue
        content_path = item.get("content_path")
        save_path = item.get("save_path")
        if download_path and not _is_path_within(content_path or save_path, download_path):
            continue
        if not _qb_is_active(item):
            continue
        progress = _to_float(item.get("progress"), 0.0) * 100.0
        seeders = _to_int(item.get("num_seeds"), 0)
        leechers = _to_int(item.get("num_leechs"), 0)
        active.append({
            "hash": item.get("hash"),
            "name": item.get("name") or "",
            "status": item.get("state") or "",
            "progress": max(0.0, min(progress, 100.0)),
            "down_speed": _to_int(item.get("dlspeed"), 0),
            "up_speed": _to_int(item.get("upspeed"), 0),
            "peers": max(_to_int(item.get("num_complete"), 0), 0) + max(_to_int(item.get("num_incomplete"), 0), 0),
            "seeders": seeders,
            "leechers": leechers,
            "eta": _to_int(item.get("eta"), -1),
            "size": _to_int(item.get("size") or item.get("total_size"), 0),
            "downloaded": _to_int(item.get("completed"), 0),
        })
    return active


_TRANSMISSION_STATUS = {
    0: "stopped",
    1: "check_wait",
    2: "checking",
    3: "download_wait",
    4: "downloading",
    5: "seed_wait",
    6: "seeding",
}


def _list_active_transmission(url, username, password, category, download_path, timeout_seconds):
    base = url.rstrip("/")
    session = _new_client_session(username, password)

    payload = {
        "method": "torrent-get",
        "arguments": {
            "fields": [
                "id", "hashString", "name", "percentDone", "rateDownload", "rateUpload",
                "peersConnected", "peersGettingFromUs", "peersSendingToUs", "eta", "status",
                "labels", "downloadDir", "totalSize", "leftUntilDone"
            ]
        },
    }
    resp = _transmission_request(session, base, payload, timeout_seconds)
    if resp.status_code != 200:
        return []
    torrents = resp.json().get("arguments", {}).get("torrents", []) or []
    active = []
    for torrent in torrents:
        labels = _normalize_labels(torrent.get("labels"))
        if not _has_managed_transmission_labels(labels):
            continue
        if category and category not in labels:
            continue
        download_dir = torrent.get("downloadDir")
        name = torrent.get("name")
        content_path = os.path.join(download_dir.rstrip("/\\"), name) if download_dir and name else None
        if download_path and not _is_path_within(content_path or download_dir, download_path):
            continue
        progress_raw = _to_float(torrent.get("percentDone"), 0.0)
        down_speed = _to_int(torrent.get("rateDownload"), 0)
        if progress_raw >= 1.0 and down_speed <= 0:
            continue
        active.append({
            "hash": torrent.get("hashString"),
            "name": name or "",
            "status": _TRANSMISSION_STATUS.get(_to_int(torrent.get("status"), -1), str(torrent.get("status") or "")),
            "progress": max(0.0, min(progress_raw * 100.0, 100.0)),
            "down_speed": down_speed,
            "up_speed": _to_int(torrent.get("rateUpload"), 0),
            "peers": _to_int(torrent.get("peersConnected"), 0),
            "seeders": _to_int(torrent.get("peersGettingFromUs"), 0),
            "leechers": _to_int(torrent.get("peersSendingToUs"), 0),
            "eta": _to_int(torrent.get("eta"), -1),
            "size": _to_int(torrent.get("totalSize"), 0),
            "downloaded": max(_to_int(torrent.get("totalSize"), 0) - _to_int(torrent.get("leftUntilDone"), 0), 0),
        })
    return active


def _list_active_deluge(url, password, category, download_path, timeout_seconds):
    ok, logged_in = _deluge_login(url, password, timeout_seconds=timeout_seconds)
    if not ok or not logged_in:
        return []

    fields = [
        "hash", "name", "save_path", "download_location", "state", "label", "progress",
        "download_payload_rate", "upload_payload_rate", "num_peers", "num_seeds", "eta",
        "total_size", "total_done"
    ]
    ok, result = _deluge_json_rpc(
        url,
        password,
        "core.get_torrents_status",
        [{}, fields],
        timeout_seconds=timeout_seconds
    )
    if not ok or not isinstance(result, dict):
        return []
    active = []
    for torrent_hash, data in result.items():
        if not isinstance(data, dict):
            continue
        label = data.get("label")
        if not _is_deluge_managed_label(label):
            continue
        if category and _build_deluge_managed_label(category) != label:
            continue
        path = data.get("download_location") or data.get("save_path")
        name = data.get("name")
        content_path = os.path.join(path.rstrip("/\\"), name) if path and name else None
        if download_path and not _is_path_within(content_path or path, download_path):
            continue
        progress = _to_float(data.get("progress"), 0.0)
        down_speed = _to_int(data.get("download_payload_rate"), 0)
        if progress >= 100.0 and down_speed <= 0:
            continue
        active.append({
            "hash": torrent_hash,
            "name": name or "",
            "status": data.get("state") or "",
            "progress": max(0.0, min(progress, 100.0)),
            "down_speed": down_speed,
            "up_speed": _to_int(data.get("upload_payload_rate"), 0),
            "peers": _to_int(data.get("num_peers"), 0),
            "seeders": _to_int(data.get("num_seeds"), 0),
            "leechers": max(_to_int(data.get("num_peers"), 0) - _to_int(data.get("num_seeds"), 0), 0),
            "eta": _to_int(data.get("eta"), -1),
            "size": _to_int(data.get("total_size"), 0),
            "downloaded": _to_int(data.get("total_done"), 0),
        })
    return active


def _list_rtorrent_with_state(url, username, password, category, download_path, timeout_seconds, include_completed):
    session = _new_client_session(username, password)
    ok, error, hashes = _rtorrent_xmlrpc(
        url,
        "download_list",
        [],
        timeout_seconds=timeout_seconds,
        username=username,
        password=password,
        session=session,
    )
    if not ok or not isinstance(hashes, list):
        return []

    expected_label = _build_rtorrent_managed_label(category) if category else None
    filtered = []
    for torrent_hash in hashes:
        torrent_hash = str(torrent_hash or "").strip()
        if not torrent_hash:
            continue
        ok_label, _, label = _rtorrent_xmlrpc(
            url,
            "d.custom1",
            [torrent_hash],
            timeout_seconds=timeout_seconds,
            username=username,
            password=password,
            session=session,
        )
        if not ok_label or not _is_deluge_managed_label(label):
            continue
        if expected_label and str(label or "").strip().lower() != expected_label.lower():
            # Keep managed torrents even if the current category config changed after add.
            raw_label = str(label or "").strip().lower()
            if raw_label not in MANAGED_TAGS:
                continue

        ok_name, _, name = _rtorrent_xmlrpc(
            url,
            "d.name",
            [torrent_hash],
            timeout_seconds=timeout_seconds,
            username=username,
            password=password,
            session=session,
        )
        ok_dir, _, directory = _rtorrent_xmlrpc(
            url,
            "d.directory",
            [torrent_hash],
            timeout_seconds=timeout_seconds,
            username=username,
            password=password,
            session=session,
        )
        ok_base_path, _, base_path = _rtorrent_xmlrpc(
            url,
            "d.base_path",
            [torrent_hash],
            timeout_seconds=timeout_seconds,
            username=username,
            password=password,
            session=session,
        )
        if not ok_name:
            name = torrent_hash
        if not ok_dir:
            directory = ""
        name = str(name or "").strip()
        directory = str(directory or "").strip()
        base_path_text = str(base_path or "").strip() if ok_base_path else ""
        if base_path_text:
            content_path = base_path_text
        else:
            content_path = os.path.join(directory.rstrip("/\\"), name) if directory and name else None
        if download_path and not _is_path_within(content_path or directory, download_path):
            continue

        ok_complete, _, complete = _rtorrent_xmlrpc(
            url,
            "d.complete",
            [torrent_hash],
            timeout_seconds=timeout_seconds,
            username=username,
            password=password,
            session=session,
        )
        is_completed = bool(_to_int(complete, 0)) if ok_complete else False
        if include_completed != is_completed:
            continue

        if include_completed:
            filtered.append({"hash": torrent_hash, "path": content_path, "name": name})
            continue

        ok_bytes_done, _, bytes_done = _rtorrent_xmlrpc(
            url,
            "d.bytes_done",
            [torrent_hash],
            timeout_seconds=timeout_seconds,
            username=username,
            password=password,
            session=session,
        )
        ok_size, _, size_bytes = _rtorrent_xmlrpc(
            url,
            "d.size_bytes",
            [torrent_hash],
            timeout_seconds=timeout_seconds,
            username=username,
            password=password,
            session=session,
        )
        ok_down, _, down_speed = _rtorrent_xmlrpc(
            url,
            "d.down.rate",
            [torrent_hash],
            timeout_seconds=timeout_seconds,
            username=username,
            password=password,
            session=session,
        )
        ok_up, _, up_speed = _rtorrent_xmlrpc(
            url,
            "d.up.rate",
            [torrent_hash],
            timeout_seconds=timeout_seconds,
            username=username,
            password=password,
            session=session,
        )
        ok_peers, _, peers = _rtorrent_xmlrpc(
            url,
            "d.peers_connected",
            [torrent_hash],
            timeout_seconds=timeout_seconds,
            username=username,
            password=password,
            session=session,
        )

        total_size = _to_int(size_bytes, 0) if ok_size else 0
        downloaded = _to_int(bytes_done, 0) if ok_bytes_done else 0
        progress = 0.0 if total_size <= 0 else max(0.0, min((float(downloaded) / float(total_size)) * 100.0, 100.0))
        filtered.append({
            "hash": torrent_hash,
            "name": name,
            "status": "downloading",
            "progress": progress,
            "down_speed": _to_int(down_speed, 0) if ok_down else 0,
            "up_speed": _to_int(up_speed, 0) if ok_up else 0,
            "peers": _to_int(peers, 0) if ok_peers else 0,
            "seeders": 0,
            "leechers": 0,
            "eta": -1,
            "size": total_size,
            "downloaded": downloaded,
        })
    return filtered


def _list_active_rtorrent(url, username, password, category, download_path, timeout_seconds):
    return _list_rtorrent_with_state(url, username, password, category, download_path, timeout_seconds, include_completed=False)


def _list_completed_qbittorrent(url, username, password, category, download_path, timeout_seconds):
    base = url.rstrip("/")
    session = _new_client_session()
    if not _login_qbittorrent(session, base, username, password, timeout_seconds):
        return []

    def fetch_with_params(extra_params=None):
        params = extra_params or {}
        params["status"] = "completed"
        resp = session.get(f"{base}/api/v2/torrents/info", params=params, timeout=timeout_seconds)
        if resp.status_code != 200:
            return []
        return resp.json() or []

    items = []
    seen_hashes = set()
    for managed_tag in MANAGED_TAGS:
        chunk = fetch_with_params({"tag": managed_tag})
        for item in chunk:
            torrent_hash = str(item.get("hash") or "").strip().lower()
            if torrent_hash and torrent_hash in seen_hashes:
                continue
            if torrent_hash:
                seen_hashes.add(torrent_hash)
            items.append(item)
    if not items:
        return []
    completed = []
    for item in items:
        if item.get("progress") == 1:
            tags = _parse_qbittorrent_tags(item.get("tags"))
            if not _has_managed_qbittorrent_tag(item.get("tags")):
                continue
            if category and item.get("category") != category and category not in tags:
                continue
            torrent_hash = item.get("hash")
            content_path = item.get("content_path")
            save_path = item.get("save_path")
            name = item.get("name")
            if not content_path and save_path and name:
                content_path = os.path.join(save_path.rstrip("/\\"), name)
            if download_path and not _is_path_within(content_path or save_path, download_path):
                continue
            if torrent_hash:
                completed.append({
                    "hash": torrent_hash,
                    "path": content_path,
                    "name": name
                })
    return completed


def _list_completed_transmission(url, username, password, category, download_path, timeout_seconds):
    base = url.rstrip("/")
    session = _new_client_session(username, password)

    payload = {
        "method": "torrent-get",
        "arguments": {"fields": ["id", "hashString", "percentDone", "labels", "downloadDir", "name"]},
    }
    resp = _transmission_request(session, base, payload, timeout_seconds)
    if resp.status_code != 200:
        return []
    torrents = resp.json().get("arguments", {}).get("torrents", []) or []
    completed = []
    for torrent in torrents:
        if torrent.get("percentDone") != 1:
            continue
        labels = _normalize_labels(torrent.get("labels"))
        if not _has_managed_transmission_labels(labels):
            continue
        if category and category not in labels:
            continue
        torrent_hash = torrent.get("hashString")
        download_dir = torrent.get("downloadDir")
        name = torrent.get("name")
        content_path = None
        if download_dir and name:
            content_path = os.path.join(download_dir.rstrip("/\\"), name)
        if download_path and not _is_path_within(content_path or download_dir, download_path):
            continue
        if torrent_hash:
            completed.append({
                "hash": torrent_hash,
                "path": content_path,
                "name": name
            })
    return completed


def _list_completed_deluge(url, password, category, download_path, timeout_seconds):
    ok, logged_in = _deluge_login(url, password, timeout_seconds=timeout_seconds)
    if not ok or not logged_in:
        return []

    state_filter = {"state": "Seeding"}
    fields = ["hash", "name", "save_path", "download_location", "state", "label"]
    ok, result = _deluge_json_rpc(
        url,
        password,
        "core.get_torrents_status",
        [state_filter, fields],
        timeout_seconds=timeout_seconds
    )
    if not ok or not isinstance(result, dict):
        return []
    completed = []
    for torrent_hash, data in result.items():
        if not isinstance(data, dict):
            continue
        label = data.get("label")
        if not _is_deluge_managed_label(label):
            continue
        if category and _build_deluge_managed_label(category) != label:
            continue
        path = data.get("download_location") or data.get("save_path")
        name = data.get("name")
        content_path = None
        if path and name:
            content_path = os.path.join(path.rstrip("/\\"), name)
        if download_path and not _is_path_within(content_path or path, download_path):
            continue
        completed.append({
            "hash": torrent_hash,
            "path": content_path,
            "name": name
        })
    return completed


def _list_completed_rtorrent(url, username, password, category, download_path, timeout_seconds):
    return _list_rtorrent_with_state(url, username, password, category, download_path, timeout_seconds, include_completed=True)


def _build_deluge_managed_label(category):
    if category:
        return f"{AEROFOIL_MANAGED_TAG}:{category}"
    return AEROFOIL_MANAGED_TAG


def _is_deluge_managed_label(label):
    if not label:
        return False
    label = str(label).strip()
    if not label:
        return False
    for managed_tag in MANAGED_TAGS:
        if label == managed_tag or label.startswith(f"{managed_tag}:"):
            return True
    return False


def _remove_qbittorrent(url, username, password, torrent_hash, timeout_seconds, delete_files=False):
    base = url.rstrip("/")
    session = _new_client_session()
    if not _login_qbittorrent(session, base, username, password, timeout_seconds):
        return False, "qBittorrent login failed."
    resp = session.post(
        f"{base}/api/v2/torrents/delete",
        data={"hashes": torrent_hash, "deleteFiles": "true" if delete_files else "false"},
        timeout=timeout_seconds,
    )
    if resp.status_code != 200:
        return False, f"qBittorrent returned {resp.status_code}."
    return True, "qBittorrent removed torrent."


def _remove_qbittorrent_with_session(session, base, torrent_hash, timeout_seconds, delete_files=False):
    if not torrent_hash:
        return False
    resp = session.post(
        f"{base}/api/v2/torrents/delete",
        data={"hashes": torrent_hash, "deleteFiles": "true" if delete_files else "false"},
        timeout=timeout_seconds,
    )
    return resp.status_code == 200


def _remove_transmission(url, username, password, torrent_hash, timeout_seconds, delete_files=False):
    base = url.rstrip("/")
    session = _new_client_session(username, password)

    payload = {
        "method": "torrent-remove",
        "arguments": {"ids": [torrent_hash], "delete-local-data": bool(delete_files)},
    }
    resp = _transmission_request(session, base, payload, timeout_seconds)
    if resp.status_code != 200:
        return False, f"Transmission returned {resp.status_code}."
    return True, "Transmission removed torrent."


def _remove_deluge(url, password, torrent_hash, timeout_seconds, delete_files=False):
    ok, logged_in = _deluge_login(url, password, timeout_seconds=timeout_seconds)
    if not ok or not logged_in:
        return False, "Deluge login failed."

    ok, result = _deluge_json_rpc(
        url,
        password,
        "core.remove_torrent",
        [torrent_hash, bool(delete_files)],
        timeout_seconds=timeout_seconds
    )
    if not ok:
        return False, "Deluge returned an error."
    return True, "Deluge removed torrent."


def _remove_rtorrent(url, torrent_hash, username, password, timeout_seconds, delete_files=False):
    normalized_hash = str(torrent_hash or "").strip().lower()
    if not normalized_hash:
        return False, "Torrent hash is required."
    _rtorrent_xmlrpc(url, "d.stop", [normalized_hash], timeout_seconds=timeout_seconds, username=username, password=password)
    ok, error, _result = _rtorrent_xmlrpc(url, "d.erase", [normalized_hash], timeout_seconds=timeout_seconds, username=username, password=password)
    if not ok:
        return False, error or "rTorrent returned an error."
    return True, "rTorrent removed torrent."


def _extract_magnet_hash(magnet_url):
    if not magnet_url:
        return None
    match = re.search(r"xt=urn:btih:([A-Fa-f0-9]+)", magnet_url)
    if match:
        return match.group(1).lower()
    match = re.search(r"xt=urn:btih:([A-Z2-7]+)", magnet_url)
    if match:
        return match.group(1).lower()
    return None


def _extract_rtorrent_hash(value):
    text = str(value or "").strip()
    if re.fullmatch(r"[A-Fa-f0-9]{40}", text):
        return text.lower()
    return None


def _find_recent_rtorrent_hash_by_name(url, expected_name, username, password, timeout_seconds):
    expected = str(expected_name or "").strip().lower()
    if not expected:
        return None
    expected_terms = [term for term in re.split(r"\s+", expected) if len(term) > 2]
    if not expected_terms:
        expected_terms = [expected]

    ok, _error, hashes = _rtorrent_xmlrpc(
        url,
        "download_list",
        [],
        timeout_seconds=timeout_seconds,
        username=username,
        password=password,
    )
    if not ok or not isinstance(hashes, list):
        return None

    best_hash = None
    best_score = -1
    for torrent_hash in hashes:
        normalized_hash = _extract_rtorrent_hash(torrent_hash)
        if not normalized_hash:
            continue
        ok_name, _name_error, name = _rtorrent_xmlrpc(
            url,
            "d.name",
            [normalized_hash],
            timeout_seconds=timeout_seconds,
            username=username,
            password=password,
        )
        if not ok_name:
            continue
        name_text = str(name or "").strip().lower()
        if not name_text:
            continue
        if expected in name_text:
            return normalized_hash
        score = sum(1 for term in expected_terms if term in name_text)
        if score > best_score:
            best_score = score
            best_hash = normalized_hash
    if best_score > 0:
        return best_hash
    return None


def _resume_qbittorrent(session, base, torrent_hash, timeout_seconds):
    data = {"hashes": torrent_hash} if torrent_hash else {"hashes": "all"}
    session.post(f"{base}/api/v2/torrents/resume", data=data, timeout=timeout_seconds)


def _compute_torrent_infohash(download_url, timeout_seconds):
    if not download_url or download_url.lower().startswith("magnet:"):
        return None
    try:
        resp = requests.get(download_url, timeout=timeout_seconds)
        if resp.status_code != 200:
            return None
        return _compute_torrent_infohash_from_content(resp.content)
    except Exception:
        return None


def _compute_torrent_infohash_from_content(content):
    if not content:
        return None
    try:
        info_slice = _extract_info_bencode_slice(content)
        if not info_slice:
            return None
        return hashlib.sha1(info_slice).hexdigest()
    except Exception:
        return None


def _bdecode_value(data, idx):
    if idx >= len(data):
        return None, idx
    token = data[idx:idx + 1]
    if token == b"i":
        idx += 1
        end = data.find(b"e", idx)
        if end == -1:
            return None, idx
        try:
            value = int(data[idx:end])
        except Exception:
            value = None
        return value, end + 1
    if token == b"l":
        idx += 1
        items = []
        while idx < len(data) and data[idx:idx + 1] != b"e":
            item, idx = _bdecode_value(data, idx)
            items.append(item)
        return items, idx + 1
    if token == b"d":
        idx += 1
        items = {}
        while idx < len(data) and data[idx:idx + 1] != b"e":
            key, idx = _bdecode_bytes(data, idx)
            if key is None:
                return None, idx
            value, idx = _bdecode_value(data, idx)
            items[key] = value
        return items, idx + 1
    if token.isdigit():
        value, next_idx = _bdecode_bytes(data, idx)
        return value, next_idx
    return None, idx


def _decode_torrent_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except Exception:
            return value.decode("latin-1", errors="ignore")
    return str(value)


def _get_torrent_file_list(download_url, timeout_seconds):
    if not download_url or download_url.lower().startswith("magnet:"):
        return None
    try:
        resp = requests.get(download_url, timeout=timeout_seconds)
        if resp.status_code != 200:
            return None
        return _get_torrent_file_list_from_content(resp.content)
    except Exception:
        return None
    return None


def _get_torrent_file_list_from_content(content):
    if not content:
        return None
    try:
        metadata, _ = _bdecode_value(content, 0)
        if not isinstance(metadata, dict):
            return None
        info = metadata.get(b"info")
        if not isinstance(info, dict):
            return None
        files = info.get(b"files")
        if isinstance(files, list):
            file_list = []
            for entry in files:
                if not isinstance(entry, dict):
                    continue
                path_parts = entry.get(b"path.utf-8") or entry.get(b"path") or []
                if not isinstance(path_parts, list):
                    continue
                decoded_parts = [_decode_torrent_text(p) for p in path_parts if p is not None]
                if decoded_parts:
                    file_list.append("/".join(decoded_parts))
            return file_list
        name = info.get(b"name.utf-8") or info.get(b"name")
        if name:
            return [_decode_torrent_text(name)]
    except Exception:
        return None
    return None


def _select_update_file_indices(file_names, expected_update_number=None, expected_version=None, exclude_russian=False):
    return shared_select_update_file_indices(
        file_names,
        expected_update_number=expected_update_number,
        expected_version=expected_version,
        exclude_russian=exclude_russian,
    )


def _select_matching_file_ids(file_entries, selector, no_match_message, selector_label):
    keep_ids = [str(file_id) for file_id in selector(file_entries or [])]
    if not keep_ids:
        logger.warning(
            "No %s files found in torrent selection (%s entries).",
            selector_label,
            len(file_entries or []),
        )
        return False, no_match_message, []
    return True, None, keep_ids


def _select_update_file_ids(file_entries, expected_update_number=None, expected_version=None, exclude_russian=False):
    return _select_matching_file_ids(
        file_entries,
        lambda entries: select_update_entry_ids(
            entries,
            expected_update_number=expected_update_number,
            expected_version=expected_version,
            exclude_russian=exclude_russian,
        ),
        TORRENT_UPDATE_SELECTION_ERROR,
        "update",
    )


def _select_dlc_file_ids(file_entries, exclude_russian=False):
    return _select_matching_file_ids(
        file_entries,
        lambda entries: select_dlc_entry_ids(
            entries,
            exclude_russian=exclude_russian,
        ),
        TORRENT_DLC_SELECTION_ERROR,
        "DLC",
    )


def _extract_info_bencode_slice(data):
    if not data:
        return None
    idx = 0
    if data[idx:idx + 1] != b"d":
        return None
    idx += 1
    while idx < len(data) and data[idx:idx + 1] != b"e":
        key, idx = _bdecode_bytes(data, idx)
        if key is None:
            return None
        if key == b"info":
            start = idx
            idx = _bdecode_skip(data, idx)
            return data[start:idx]
        idx = _bdecode_skip(data, idx)
    return None


def _bdecode_bytes(data, idx):
    if idx >= len(data) or data[idx:idx + 1].isdigit() is False:
        return None, idx
    length = 0
    while idx < len(data) and data[idx:idx + 1].isdigit():
        length = length * 10 + (data[idx] - 48)
        idx += 1
    if idx >= len(data) or data[idx:idx + 1] != b":":
        return None, idx
    idx += 1
    end = idx + length
    if end > len(data):
        return None, idx
    return data[idx:end], end


def _bdecode_skip(data, idx):
    if idx >= len(data):
        return idx
    token = data[idx:idx + 1]
    if token == b"i":
        idx += 1
        while idx < len(data) and data[idx:idx + 1] != b"e":
            idx += 1
        return idx + 1
    if token == b"l":
        idx += 1
        while idx < len(data) and data[idx:idx + 1] != b"e":
            idx = _bdecode_skip(data, idx)
        return idx + 1
    if token == b"d":
        idx += 1
        while idx < len(data) and data[idx:idx + 1] != b"e":
            _, idx = _bdecode_bytes(data, idx)
            idx = _bdecode_skip(data, idx)
        return idx + 1
    if token.isdigit():
        _, idx = _bdecode_bytes(data, idx)
        return idx
    return idx


def _find_qbittorrent_hash_by_infohash(session, base, infohash_v1, category, added_after, timeout_seconds):
    items = _fetch_qbittorrent_managed_items(
        session,
        base,
        timeout_seconds,
        extra_params={"sort": "added_on", "reverse": "true"},
    )
    if not items:
        return None
    matches = []
    candidates = []
    for item in items:
        tags = _parse_qbittorrent_tags(item.get("tags"))
        if category:
            if item.get("category") != category and category not in tags:
                continue
        added_on = int(item.get("added_on") or 0)
        if added_after and added_on < added_after:
            continue
        candidates.append({
            "hash": item.get("hash"),
            "infohash_v1": item.get("infohash_v1"),
            "name": item.get("name"),
            "added_on": added_on
        })
        if (item.get("infohash_v1") or "").lower() == infohash_v1.lower():
            matches.append(item)
        elif (item.get("hash") or "").lower() == infohash_v1.lower():
            matches.append(item)
    if matches:
        matches.sort(key=lambda item: item.get("added_on", 0), reverse=True)
        return matches[0].get("hash")
    if candidates:
        logger.info("No hash match for infohash_v1. Candidates: %s", candidates)
    return None


def _normalize_hash(session, base, torrent_hash, timeout_seconds):
    if not torrent_hash:
        return None
    resp = session.get(
        f"{base}/api/v2/torrents/info",
        params={"hashes": torrent_hash},
        timeout=timeout_seconds,
    )
    if resp.status_code != 200:
        return None
    items = resp.json() or []
    if not items:
        return None
    return items[0].get("hash") or torrent_hash


def _build_qbittorrent_tags(category):
    tags = [AEROFOIL_MANAGED_TAG]
    if category:
        tags.append(category)
    return ",".join(dict.fromkeys(tags))


def _find_qbittorrent_hash_by_tag(session, base, tag, timeout_seconds):
    if not tag:
        return None
    resp = session.get(
        f"{base}/api/v2/torrents/info",
        params={"tag": tag},
        timeout=timeout_seconds,
    )
    if resp.status_code != 200:
        return None
    items = resp.json() or []
    if not items:
        return None
    items.sort(key=lambda item: item.get("added_on", 0), reverse=True)
    return items[0].get("hash")


def _find_recent_qbittorrent_hash(session, base, expected_name, category, timeout_seconds, added_after=None, require_match=False):
    expected = (expected_name or "").lower()
    expected_terms = [term for term in re.split(r"\s+", expected) if len(term) > 2]
    candidates = []

    def fetch(params):
        resp = session.get(f"{base}/api/v2/torrents/info", params=params, timeout=timeout_seconds)
        if resp.status_code != 200:
            return []
        return resp.json() or []

    candidates = _fetch_qbittorrent_managed_items(
        session,
        base,
        timeout_seconds,
        extra_params={"sort": "added_on", "reverse": "true", "limit": 20},
    )

    matches = []
    for item in candidates:
        tags = _parse_qbittorrent_tags(item.get("tags"))
        if not _has_managed_qbittorrent_tag(item.get("tags")):
            continue
        if category and item.get("category") != category and category not in tags:
            continue
        name = (item.get("name") or "").lower()
        added_on = int(item.get("added_on") or 0)
        if added_after and added_on < added_after:
            continue
        if expected and expected in name:
            matches.append(item)
        elif expected_terms and all(term in name for term in expected_terms):
            matches.append(item)
    if matches:
        matches.sort(key=lambda item: item.get("added_on", 0), reverse=True)
        return matches[0].get("hash")
    if require_match:
        if not expected and len(candidates) == 1:
            return candidates[0].get("hash")
        return None
    if candidates:
        candidates.sort(key=lambda item: item.get("added_on", 0), reverse=True)
        return candidates[0].get("hash")
    return None


def _select_qbittorrent_highest_version(session, base, torrent_hash, timeout_seconds, exclude_russian, expected_update_number=None, expected_version=None):
    resp = session.get(
        f"{base}/api/v2/torrents/files",
        params={"hash": torrent_hash},
        timeout=timeout_seconds,
    )
    if resp.status_code != 200:
        logger.warning("Failed to fetch torrent files for %s: %s", torrent_hash, resp.status_code)
        return
    files = resp.json() or []
    logger.info("Torrent %s file list entries: %s", torrent_hash, len(files))
    all_ids = []
    file_entries = []
    for file in files:
        name = file.get("name") or ""
        file_id = file.get("index")
        if file_id is None:
            file_id = file.get("id")
        if file_id is None:
            continue
        all_ids.append(str(file_id))
        file_entries.append((file_id, name))
    keep_ids = [
        str(file_id) for file_id in select_update_entry_ids(
            file_entries,
            expected_update_number=expected_update_number,
            expected_version=expected_version,
            exclude_russian=exclude_russian,
        )
    ]
    if not keep_ids:
        logger.warning(
            "No update files found for torrent %s (expected_version=%s, expected_update_number=%s).",
            torrent_hash,
            expected_version,
            expected_update_number,
        )
        return False
    keep_set = set(keep_ids)
    if all_ids:
        disable_resp = _set_qbittorrent_file_priority(session, base, torrent_hash, all_ids, 0, timeout_seconds)
        logger.info("Disable all files response: %s", disable_resp)
    if keep_ids:
        enable_resp = _set_qbittorrent_file_priority(session, base, torrent_hash, keep_ids, 1, timeout_seconds)
        logger.info("Enable file ids %s response: %s", "|".join(keep_ids), enable_resp)

    verify = session.get(
        f"{base}/api/v2/torrents/files",
        params={"hash": torrent_hash},
        timeout=timeout_seconds,
    )
    if verify.status_code != 200:
        logger.warning("Failed to verify file priorities for %s: %s", torrent_hash, verify.status_code)
        return
    files_after = verify.json() or []
    retry_disable = []
    retry_enable = []
    for file in files_after:
        file_id = file.get("index")
        if file_id is None:
            file_id = file.get("id")
        if file_id is None:
            continue
        file_id_str = str(file_id)
        priority = file.get("priority")
        if file_id_str in keep_set:
            if priority != 1:
                retry_enable.append(file_id_str)
        else:
            if priority != 0:
                retry_disable.append(file_id_str)
    if retry_disable:
        _set_qbittorrent_file_priority(session, base, torrent_hash, retry_disable, 0, timeout_seconds, per_file=True)
    if retry_enable:
        _set_qbittorrent_file_priority(session, base, torrent_hash, retry_enable, 1, timeout_seconds, per_file=True)
    return True


def _select_qbittorrent_dlc_files(session, base, torrent_hash, timeout_seconds, exclude_russian):
    resp = session.get(
        f"{base}/api/v2/torrents/files",
        params={"hash": torrent_hash},
        timeout=timeout_seconds,
    )
    if resp.status_code != 200:
        logger.warning("Failed to fetch torrent files for %s: %s", torrent_hash, resp.status_code)
        return
    files = resp.json() or []
    logger.info("Torrent %s file list entries: %s", torrent_hash, len(files))
    all_ids = []
    file_entries = []
    for file in files:
        name = file.get("name") or ""
        file_id = file.get("index")
        if file_id is None:
            file_id = file.get("id")
        if file_id is None:
            continue
        all_ids.append(str(file_id))
        file_entries.append((file_id, name))
    keep_ids = [
        str(file_id) for file_id in select_dlc_entry_ids(
            file_entries,
            exclude_russian=exclude_russian,
        )
    ]
    if not keep_ids:
        logger.warning("No DLC files found for torrent %s.", torrent_hash)
        return False
    keep_set = set(keep_ids)
    if all_ids:
        disable_resp = _set_qbittorrent_file_priority(session, base, torrent_hash, all_ids, 0, timeout_seconds)
        logger.info("Disable all files response: %s", disable_resp)
    if keep_ids:
        enable_resp = _set_qbittorrent_file_priority(session, base, torrent_hash, keep_ids, 1, timeout_seconds)
        logger.info("Enable file ids %s response: %s", "|".join(keep_ids), enable_resp)

    verify = session.get(
        f"{base}/api/v2/torrents/files",
        params={"hash": torrent_hash},
        timeout=timeout_seconds,
    )
    if verify.status_code != 200:
        logger.warning("Failed to verify file priorities for %s: %s", torrent_hash, verify.status_code)
        return
    files_after = verify.json() or []
    retry_disable = []
    retry_enable = []
    for file in files_after:
        file_id = file.get("index")
        if file_id is None:
            file_id = file.get("id")
        if file_id is None:
            continue
        file_id_str = str(file_id)
        priority = file.get("priority")
        if file_id_str in keep_set:
            if priority != 1:
                retry_enable.append(file_id_str)
        else:
            if priority != 0:
                retry_disable.append(file_id_str)
    if retry_disable:
        _set_qbittorrent_file_priority(session, base, torrent_hash, retry_disable, 0, timeout_seconds, per_file=True)
    if retry_enable:
        _set_qbittorrent_file_priority(session, base, torrent_hash, retry_enable, 1, timeout_seconds, per_file=True)
    return True


def _fetch_deluge_file_names(url, password, torrent_hash, timeout_seconds):
    ok, status = _deluge_json_rpc(
        url,
        password,
        "core.get_torrent_status",
        [torrent_hash, ["files"]],
        timeout_seconds=timeout_seconds,
    )
    if not ok or not isinstance(status, dict) or not status.get("files"):
        return []
    files = status.get("files") or []
    return [f.get("path") for f in files]


def _fetch_rtorrent_file_entries(url, torrent_hash, username, password, timeout_seconds):
    ok, _error, rows = _rtorrent_xmlrpc(
        url,
        "f.multicall",
        [torrent_hash, "", "f.path=", "f.priority="],
        timeout_seconds=timeout_seconds,
        username=username,
        password=password,
    )
    if not ok or not isinstance(rows, list):
        return []
    entries = []
    for idx, row in enumerate(rows):
        if isinstance(row, (list, tuple)):
            path = row[0] if len(row) > 0 else ""
        else:
            path = row
        entries.append((idx, str(path or "")))
    return entries


def _fetch_rtorrent_file_names(url, torrent_hash, username, password, timeout_seconds):
    return [name for _entry_id, name in _fetch_rtorrent_file_entries(url, torrent_hash, username, password, timeout_seconds)]


def _set_rtorrent_file_priority(url, torrent_hash, file_id, priority, username, password, timeout_seconds):
    for method in ("f.priority.set", "f.set_priority"):
        ok, _error, _result = _rtorrent_xmlrpc(
            url,
            method,
            [torrent_hash, int(file_id), int(priority)],
            timeout_seconds=timeout_seconds,
            username=username,
            password=password,
        )
        if ok:
            return True
    return False


def _select_rtorrent_highest_version(url, torrent_hash, username, password, timeout_seconds, exclude_russian, expected_update_number=None, expected_version=None):
    file_names = poll_update_file_names(
        lambda: _fetch_rtorrent_file_names(url, torrent_hash, username, password, timeout_seconds),
        sleep_fn=time.sleep,
    )
    if not file_names:
        return False
    file_entries = list(enumerate(file_names))
    keep_ids = set(
        select_update_entry_ids(
            file_entries,
            expected_update_number=expected_update_number,
            expected_version=expected_version,
            exclude_russian=exclude_russian,
        )
    )
    if not keep_ids:
        return False
    for file_id, _name in file_entries:
        _set_rtorrent_file_priority(url, torrent_hash, file_id, 0, username, password, timeout_seconds)
    for file_id in keep_ids:
        _set_rtorrent_file_priority(url, torrent_hash, file_id, 1, username, password, timeout_seconds)
    return True


def _select_rtorrent_dlc_files(url, torrent_hash, username, password, timeout_seconds, exclude_russian):
    file_names = poll_update_file_names(
        lambda: _fetch_rtorrent_file_names(url, torrent_hash, username, password, timeout_seconds),
        sleep_fn=time.sleep,
    )
    if not file_names:
        return False
    file_entries = list(enumerate(file_names))
    keep_ids = set(
        select_dlc_entry_ids(
            file_entries,
            exclude_russian=exclude_russian,
        )
    )
    if not keep_ids:
        return False
    for file_id, _name in file_entries:
        _set_rtorrent_file_priority(url, torrent_hash, file_id, 0, username, password, timeout_seconds)
    for file_id in keep_ids:
        _set_rtorrent_file_priority(url, torrent_hash, file_id, 1, username, password, timeout_seconds)
    return True


def _fetch_transmission_file_names(request_fn, torrent_id):
    info_payload = {
        "method": "torrent-get",
        "arguments": {"fields": ["id", "files", "name"], "ids": [torrent_id]},
    }
    info_resp = request_fn(info_payload)
    if info_resp.status_code != 200:
        return []
    torrents = info_resp.json().get("arguments", {}).get("torrents", []) or []
    if not torrents or not torrents[0].get("files"):
        return []
    files = torrents[0].get("files") or []
    return [f.get("name") for f in files]


def _set_qbittorrent_file_priority(session, base, torrent_hash, ids, priority, timeout_seconds, per_file=False):
    if not ids:
        return None
    if per_file:
        statuses = []
        for file_id in ids:
            resp = session.post(
                f"{base}/api/v2/torrents/filePrio",
                data={"hash": torrent_hash, "id": str(file_id), "priority": priority},
                timeout=timeout_seconds,
            )
            statuses.append(resp.status_code)
        return statuses
    resp = session.post(
        f"{base}/api/v2/torrents/filePrio",
        data={"hash": torrent_hash, "id": "|".join(ids), "priority": priority},
        timeout=timeout_seconds,
    )
    return resp.status_code


def _exclude_qbittorrent_russian(session, base, torrent_hash, timeout_seconds):
    resp = session.get(
        f"{base}/api/v2/torrents/files",
        params={"hash": torrent_hash},
        timeout=timeout_seconds,
    )
    if resp.status_code != 200:
        return
    files = resp.json() or []
    drop_ids = []
    for file in files:
        name = (file.get("name") or "").lower()
        if "russian" in name or "rus" in name:
            file_id = file.get("index")
            if file_id is None:
                file_id = file.get("id")
            if file_id is None:
                continue
            drop_ids.append(str(file_id))
    if drop_ids:
        _set_qbittorrent_file_priority(session, base, torrent_hash, drop_ids, 0, timeout_seconds, per_file=True)
