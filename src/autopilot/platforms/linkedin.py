"""LinkedIn (Posts API) for a Page or a member profile you own.

config:
  linkedin:
    author: urn:li:organization:12345    # your Page; or urn:li:person:<id> for your own profile
    token: ${LINKEDIN_TOKEN}             # w_organization_social (Page) or w_member_social (member)
    version: "202611"                    # optional LinkedIn-Version (YYYYMM); LinkedIn retires versions after about a year
Videos are uploaded directly; no media hosting is needed. Member tokens expire after 60 days.
"""
from __future__ import annotations

import re
import time
import urllib.parse

from .. import http
from ..errors import AutopilotError, ConfigError, QuotaReached
from . import Result

API = "https://api.linkedin.com/rest/"
_TEXT = re.compile(r"#(\w+)|([\\|{}@\[\]()<>#*_~])")   # a hashtag, or a character LinkedIn's text format reserves


def _cfg(ctx):
    c = ctx.cfg.get("linkedin", {}) or {}
    if not c.get("author") or not c.get("token"):
        raise ConfigError("linkedin.author and linkedin.token are required")
    if not c["author"].startswith(("urn:li:organization:", "urn:li:person:")):
        raise ConfigError("linkedin.author must be urn:li:organization:<id> or urn:li:person:<id>")
    return c["author"], {"Authorization": f"Bearer {c['token']}", "LinkedIn-Version": str(c.get("version", "202611")),
                         "X-Restli-Protocol-Version": "2.0.0"}


def _call(headers, method, url, body=None):
    try:
        return http.call(method, url, body, headers)
    except AutopilotError as e:
        if "HTTP 429" in str(e):
            raise QuotaReached(str(e)) from None
        raise


def _commentary(text):
    return _TEXT.sub(lambda m: "{hashtag|\\#|%s}" % m[1] if m[1] else "\\" + m[2], text)[:3000]


def _publish(author, headers, item, content=None):
    body = {"author": author, "commentary": _commentary(item.text()), "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
    if content:
        body["content"] = content
    urn = _call(headers, "POST", API + "posts", body)[1].get("x-restli-id")
    if not urn:
        raise AutopilotError("LinkedIn accepted the post but returned no id")
    return Result(url=f"https://www.linkedin.com/feed/update/{urn}/", remote_id=urn)


def _upload(author, headers, path):
    init = _call(headers, "POST", API + "videos?action=initializeUpload", {"initializeUploadRequest": {
        "owner": author, "fileSizeBytes": path.stat().st_size, "uploadCaptions": False, "uploadThumbnail": False}})[0]["value"]
    etags = []
    with open(path, "rb") as f:
        for part in init["uploadInstructions"]:
            f.seek(part["firstByte"])
            chunk = f.read(part["lastByte"] - part["firstByte"] + 1)
            etags.append(_call({"Content-Type": "application/octet-stream"}, "PUT", part["uploadUrl"], chunk)[1]["etag"].strip('"'))
    _call(headers, "POST", API + "videos?action=finalizeUpload", {"finalizeUploadRequest": {
        "video": init["video"], "uploadToken": init.get("uploadToken", ""), "uploadedPartIds": etags}})
    return init["video"]


def _wait_ready(headers, urn, wait=900, poll=10):
    deadline = time.time() + wait
    while True:
        s = _call(headers, "GET", API + "videos/" + urllib.parse.quote(urn, safe=""))[0]
        if s.get("status") == "AVAILABLE":
            return
        if s.get("status") == "PROCESSING_FAILED":
            raise AutopilotError(f"LinkedIn video PROCESSING_FAILED: {s.get('processingFailureReason', '')}")
        if time.time() > deadline:
            raise AutopilotError("LinkedIn video still processing after %ss" % wait)
        time.sleep(poll)


def post(ctx, item):
    """A text post; with `link:` it carries a link card."""
    author, headers = _cfg(ctx)
    content = None
    if item.link:
        content = {"article": {"source": item.link, "title": item.title or item.link}}
        if item.description:
            content["article"]["description"] = item.description
    return _publish(author, headers, item, content)


def video(ctx, item):
    author, headers = _cfg(ctx)
    urn = _upload(author, headers, item.files["video"])
    _wait_ready(headers, urn)
    return _publish(author, headers, item, {"media": {"title": item.title or item.slug, "id": urn}})
