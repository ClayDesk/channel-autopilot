"""TikTok (Content Posting API, Direct Post).

config:
  tiktok:
    client_key: ${TIKTOK_CLIENT_KEY}               # your app on developers.tiktok.com (Login Kit + Content Posting API)
    client_secret: ${TIKTOK_CLIENT_SECRET}
    token: secrets/tiktok-token.json                # written by `autopilot tiktok-auth`
    redirect_uri: http://localhost:8765/callback/   # optional; by default any free port, registered as http://localhost:*/callback/
    privacy: PUBLIC_TO_EVERYONE                     # one the account allows: PUBLIC_TO_EVERYONE | MUTUAL_FOLLOW_FRIENDS | FOLLOWER_OF_CREATOR | SELF_ONLY
Until TikTok audits your app, posts can only be private (SELF_ONLY). TikTok caps API posts per creator per day.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from .. import http
from ..errors import AutopilotError, ConfigError, QuotaReached
from . import Result

API = "https://open.tiktokapis.com/v2/"
SCOPES = "user.info.basic,video.publish"
CHUNK = 10 * 1024 * 1024   # TikTok wants 5-64 MB chunks; smaller files go in one piece
_STOP = ("spam_risk_too_many_posts", "reached_active_user_cap")   # TikTok's daily caps


def _cfg(cfg):
    c = cfg.get("tiktok", {}) or {}
    if not c.get("client_key") or not c.get("client_secret"):
        raise ConfigError("tiktok.client_key and tiktok.client_secret are required")
    return c


def _save(token, t):
    t["expires_at"] = int(time.time()) + int(t.get("expires_in", 0))
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(json.dumps(t))
    token.chmod(0o600)


def auth(cfg):
    """One-time browser sign-in as the TikTok account owner; saves the token."""
    c = _cfg(cfg)
    verifier, state, got = secrets.token_urlsafe(64), secrets.token_urlsafe(16), {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            got.update(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Done: you can close this tab.")

        def log_message(self, *a):
            pass

    u = urllib.parse.urlsplit(c.get("redirect_uri") or "http://localhost/callback/")
    with HTTPServer((u.hostname, u.port or 0), Callback) as srv:   # port 0: any free port
        redirect = c.get("redirect_uri") or f"http://localhost:{srv.server_port}/callback/"
        url = "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode({
            "client_key": c["client_key"], "scope": SCOPES, "response_type": "code", "redirect_uri": redirect, "state": state,
            "code_challenge": hashlib.sha256(verifier.encode()).hexdigest(), "code_challenge_method": "S256"})
        print("Sign in to TikTok in the browser (or open this URL):", url)
        webbrowser.open(url)
        while "code" not in got and "error" not in got:
            srv.handle_request()
    if got.get("state") != state or "code" not in got:
        raise AutopilotError(f"TikTok sign-in failed: {got.get('error_description') or got.get('error') or 'state mismatch'}")
    t = http.request("POST", API + "oauth/token/", {"client_key": c["client_key"], "client_secret": c["client_secret"],
                     "code": got["code"], "grant_type": "authorization_code", "redirect_uri": redirect, "code_verifier": verifier})
    if "access_token" not in t:
        raise AutopilotError(f"TikTok sign-in failed: {t.get('error_description') or t.get('error')}")
    token = cfg.path("tiktok.token", "secrets/tiktok-token.json")
    _save(token, t)
    return token


def _access_token(cfg):
    """The stored token, refreshed when it is about to expire (TikTok's last 2 hours; each refresh replaces the refresh token)."""
    c = _cfg(cfg)
    token = cfg.path("tiktok.token", "secrets/tiktok-token.json")
    if not token.exists():
        raise ConfigError(f"no TikTok token at {token}: run `autopilot tiktok-auth`")
    t = json.loads(token.read_text())
    if time.time() > t.get("expires_at", 0) - 300:
        t = http.request("POST", API + "oauth/token/", {"client_key": c["client_key"], "client_secret": c["client_secret"],
                         "grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
        if "access_token" not in t:
            raise ConfigError(f"TikTok token refresh failed ({t.get('error_description') or t.get('error')}): run `autopilot tiktok-auth`")
        _save(token, t)
    return t["access_token"]


def _api(tok, path, body=None):
    try:
        r = http.call("POST", API + path, body or {}, {"Authorization": f"Bearer {tok}", "Content-Type": "application/json; charset=UTF-8"})[0]
    except AutopilotError as e:
        if any(s in str(e) for s in _STOP):
            raise QuotaReached(str(e)) from None
        raise
    code = (r.get("error") or {}).get("code", "ok")
    if code in _STOP:   # creator_info reports the daily cap with HTTP 200
        raise QuotaReached(f"TikTok: {code}")
    if code != "ok":
        raise AutopilotError(f"TikTok {path}: {code} {r['error'].get('message', '')}")
    return r.get("data") or {}


def _upload(url, path, size, chunk, count):
    with open(path, "rb") as f:
        for i in range(count):
            first, last = i * chunk, (size if i == count - 1 else (i + 1) * chunk) - 1   # the last chunk takes the trailing bytes
            try:
                http.call("PUT", url, f.read(last - first + 1), {"Content-Type": "video/mp4", "Content-Range": f"bytes {first}-{last}/{size}"})
            except json.JSONDecodeError:
                pass   # a 2xx whose body is not JSON: the chunk was accepted


def _wait_posted(tok, publish_id, public, wait=600, poll=10):
    """The post id once TikTok has published it; "" when it is posted but has no public id (private, or still in moderation)."""
    deadline = time.time() + wait
    while True:
        s = _api(tok, "post/publish/status/fetch/", {"publish_id": publish_id})
        if s.get("publicaly_available_post_id"):   # TikTok's spelling
            return str(s["publicaly_available_post_id"][0])
        if s.get("status") == "FAILED":
            reason = s.get("fail_reason", "")
            raise (QuotaReached if reason in _STOP else AutopilotError)(f"TikTok post FAILED: {reason}")
        if s.get("status") == "PUBLISH_COMPLETE" and (not public or time.time() > deadline):
            return ""
        if time.time() > deadline:
            raise AutopilotError("TikTok post still processing after %ss" % wait)
        time.sleep(poll)


def _title(text):
    """TikTok counts the 2200-character caption limit in UTF-16 units (an emoji is two)."""
    return text.encode("utf-16-le")[:4400].decode("utf-16-le", "ignore")


def video(ctx, item):
    c = _cfg(ctx.cfg)
    tok = _access_token(ctx.cfg)
    info = _api(tok, "post/publish/creator_info/query/")
    privacy = c.get("privacy", "PUBLIC_TO_EVERYONE")
    if privacy not in info.get("privacy_level_options", []):
        raise ConfigError(f"tiktok.privacy {privacy} is not allowed for this account; use one of {info.get('privacy_level_options')}")
    path = item.files["video"]
    size = path.stat().st_size
    chunk = min(size, CHUNK)
    init = _api(tok, "post/publish/video/init/", {
        "post_info": {"title": _title(item.text()), "privacy_level": privacy},
        "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": size // chunk}})
    _upload(init["upload_url"], path, size, chunk, size // chunk)
    post_id = _wait_posted(tok, init["publish_id"], privacy == "PUBLIC_TO_EVERYONE")
    profile = f"https://www.tiktok.com/@{info.get('creator_username', '')}"
    return Result(url=f"{profile}/video/{post_id}" if post_id else profile, remote_id=post_id or init["publish_id"])
