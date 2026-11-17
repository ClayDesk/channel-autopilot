import pytest

from autopilot import http
from autopilot.errors import AutopilotError, ConfigError, QuotaReached
from autopilot.platforms import facebook, instagram, linkedin
from autopilot.queue import Item
from autopilot.schedule import Ctx
from pathlib import Path


def item(**kw):
    base = dict(slug="s", dir=Path("."), kind="short", title="T", description="D", caption="Cap", files={"video": Path("v.mp4"), "image": Path("i.png")})
    base.update(kw)
    return Item(**base)


class Fake:
    """Records calls and answers like the Graph API (or LinkedIn's REST API)."""
    def __init__(self, answers): self.calls, self.answers = [], answers
    def __call__(self, method, url, data=None, headers=None, **kw):
        self.calls.append((method, url, data if isinstance(data, bytes) else dict(data or {})))
        for needle, ans in self.answers:
            if needle in url:
                return ans(data) if callable(ans) else ans
        raise AssertionError(f"unexpected call {method} {url}")


def test_instagram_reel_flow(cfg, monkeypatch):
    f = Fake([("IG1/media_publish", {"id": "M1"}), ("IG1/media", {"id": "C1"}), ("/C1", {"status_code": "FINISHED"}),
              ("/M1", {"permalink": "https://www.instagram.com/reel/abc/"})])
    monkeypatch.setattr(http, "request", f)
    r = instagram.reel(Ctx(cfg), item())
    assert r.url == "https://www.instagram.com/reel/abc/" and r.remote_id == "M1"
    first = f.calls[0][2]
    assert first["media_type"] == "REELS" and first["video_url"].startswith("https://cdn.example.com/s-v.mp4") and first["caption"] == "Cap"


def test_instagram_story_uses_stories_media_type(cfg, monkeypatch):
    f = Fake([("IG1/media_publish", {"id": "M1"}), ("IG1/media", {"id": "C1"}), ("/C1", {"status_code": "FINISHED"}), ("/M1", {"permalink": "u"})])
    monkeypatch.setattr(http, "request", f)
    instagram.story(Ctx(cfg), item())
    assert f.calls[0][2]["media_type"] == "STORIES" and "caption" not in f.calls[0][2]


def test_instagram_quota_is_not_a_failure(cfg, monkeypatch):
    def lim(*a, **k): raise AutopilotError("HTTP 400 content_publishing_limit reached")
    monkeypatch.setattr(http, "request", lim)
    with pytest.raises(QuotaReached):
        instagram.reel(Ctx(cfg), item())


def test_instagram_container_error_raises(cfg, monkeypatch):
    f = Fake([("IG1/media", {"id": "C1"}), ("/C1", {"status_code": "ERROR", "status": "bad video"})])
    monkeypatch.setattr(http, "request", f)
    with pytest.raises(AutopilotError, match="ERROR"):
        instagram.reel(Ctx(cfg), item())


def test_facebook_reel_flow(cfg, monkeypatch):
    facebook._page_token._c = {}
    f = Fake([("PG1/videos", {"id": "V1"}), ("PG1", {"access_token": "PAGE"}),
              ("/V1", {"status": {"video_status": "ready"}, "permalink_url": "/reel/V1/"})])
    monkeypatch.setattr(http, "request", f)
    r = facebook.reel(Ctx(cfg), item())
    assert r.url == "https://www.facebook.com/reel/V1/"
    post_call = [c for c in f.calls if c[0] == "POST"][0]
    assert post_call[2]["access_token"] == "PAGE" and post_call[2]["description"] == "Cap"


def test_facebook_post_with_link(cfg, monkeypatch):
    facebook._page_token._c = {}
    f = Fake([("PG1/feed", {"id": "PG1_9"}), ("PG1_9", {"permalink_url": "https://www.facebook.com/PG1/posts/9"}), ("PG1", {"access_token": "PAGE"})])
    monkeypatch.setattr(http, "request", f)
    r = facebook.post(Ctx(cfg), item(link="https://example.com/blog"))
    assert r.url.endswith("/posts/9")
    assert [c for c in f.calls if c[0] == "POST"][0][2]["link"] == "https://example.com/blog"


def test_missing_credentials_are_a_config_error(cfg):
    cfg.data["instagram"] = {}
    with pytest.raises(ConfigError):
        instagram.reel(Ctx(cfg), item())


def test_linkedin_post_with_link(cfg, monkeypatch):
    f = Fake([("rest/posts", ({}, {"x-restli-id": "urn:li:share:7"}))])
    monkeypatch.setattr(http, "call", f)
    r = linkedin.post(Ctx(cfg), item(caption="Read (part 1) #money", link="https://example.com/blog"))
    assert r.url == "https://www.linkedin.com/feed/update/urn:li:share:7/" and r.remote_id == "urn:li:share:7"
    body = f.calls[0][2]
    assert body["author"] == "urn:li:organization:42" and body["commentary"] == r"Read \(part 1\) {hashtag|\#|money}"
    assert body["content"]["article"] == {"source": "https://example.com/blog", "title": "T", "description": "D"}
    linkedin.post(Ctx(cfg), item(description="", link="https://example.com/blog"))
    assert "description" not in f.calls[1][2]["content"]["article"]


def test_linkedin_video_flow(cfg, monkeypatch, tmp_path):
    (tmp_path / "v.mp4").write_bytes(b"0123456789")
    parts = [{"uploadUrl": "https://up.example/1", "firstByte": 0, "lastByte": 5},
             {"uploadUrl": "https://up.example/2", "firstByte": 6, "lastByte": 9}]
    f = Fake([("initializeUpload", ({"value": {"video": "urn:li:video:V1", "uploadToken": "UT", "uploadInstructions": parts}}, {})),
              ("up.example/1", ({}, {"etag": '"E1"'})), ("up.example/2", ({}, {"etag": "E2"})), ("finalizeUpload", ({}, {})),
              ("video%3AV1", ({"status": "AVAILABLE"}, {})), ("rest/posts", ({}, {"x-restli-id": "urn:li:ugcPost:9"}))])
    monkeypatch.setattr(http, "call", f)
    r = linkedin.video(Ctx(cfg), item(files={"video": tmp_path / "v.mp4"}))
    assert r.remote_id == "urn:li:ugcPost:9"
    assert [c[2] for c in f.calls if c[0] == "PUT"] == [b"012345", b"6789"]
    assert f.calls[3][2]["finalizeUploadRequest"]["uploadedPartIds"] == ["E1", "E2"]
    assert f.calls[-1][2]["content"] == {"media": {"title": "T", "id": "urn:li:video:V1"}}


def test_linkedin_video_error_raises(cfg, monkeypatch, tmp_path):
    (tmp_path / "v.mp4").write_bytes(b"x")
    f = Fake([("initializeUpload", ({"value": {"video": "urn:li:video:V1", "uploadInstructions": []}}, {})),
              ("finalizeUpload", ({}, {})), ("video%3AV1", ({"status": "PROCESSING_FAILED"}, {}))])
    monkeypatch.setattr(http, "call", f)
    with pytest.raises(AutopilotError, match="PROCESSING_FAILED"):
        linkedin.video(Ctx(cfg), item(files={"video": tmp_path / "v.mp4"}))


def test_linkedin_rate_limit_is_not_a_failure(cfg, monkeypatch):
    def lim(*a, **k): raise AutopilotError("POST https://api.linkedin.com/rest/posts: HTTP 429 Too Many Requests")
    monkeypatch.setattr(http, "call", lim)
    with pytest.raises(QuotaReached):
        linkedin.post(Ctx(cfg), item())
