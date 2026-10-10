"""Live checks against the real APIs. Opt-in: `pytest -m live`.

Everything else in the suite runs against recorded payloads, which proves the
parsing but not the contract. These prove the contract: that the moderation
endpoint still accepts the request this code builds, still returns the fields
it reads, and still looks at the picture rather than the URL.

Deselected by default (see addopts in pyproject.toml) because they need
network and a real key, and a test that fails on a train is a test people
learn to ignore.

    pytest -m live                     # needs KITEGATE_IMAGE_SCAN_API_KEY
    pytest -m live -k wikipedia        # the photo provider only, no key needed
"""
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from kitegate.gateway import GatewaySettings, create_app
from kitegate.images import (IMAGE_THRESHOLDS, moderation_body, read_scores, safe_image_url,
                             worst_category)
from kitegate.photos import Wikimedia, image_hosts_for

pytestmark = pytest.mark.live

SCAN_URL = os.getenv("KITEGATE_IMAGE_SCAN_URL", "https://api.openai.com/v1/moderations")
SCAN_MODEL = os.getenv("KITEGATE_IMAGE_SCAN_MODEL", "omni-moderation-latest")
UA = {"User-Agent": "kitegate-tests/1.0 (+https://github.com/kitegate)"}


def scan_key():
    key = (os.getenv("KITEGATE_IMAGE_SCAN_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        pytest.skip("set KITEGATE_IMAGE_SCAN_API_KEY (or OPENAI_API_KEY) to run the live scan")
    return key


def wikipedia_photo(subject):
    """The real lead image for a subject, via the real provider."""
    provider = Wikimedia()
    endpoint = provider.endpoints[0]
    request = provider.lookup(endpoint, subject, 640)
    r = httpx.get(request.url, params=request.params, headers=UA, timeout=30)
    r.raise_for_status()
    _, photo = provider.read_lookup(r.json(), endpoint)
    if photo is None:
        pytest.skip(f"Wikipedia has no lead image for {subject!r} today")
    return photo


def moderate(urls):
    r = httpx.post(SCAN_URL, json=moderation_body(urls, SCAN_MODEL),
                   headers={"authorization": f"Bearer {scan_key()}"}, timeout=60)
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}"
    return r.json()


# -- the provider ---------------------------------------------------------------
def test_wikipedia_still_returns_a_lead_image_and_a_known_host():
    photo = wikipedia_photo("tiger")
    assert photo.title == "Tiger"
    assert photo.image_url.startswith("https://")
    assert "utm_" not in photo.image_url
    # The check that silently breaks pictures if Wikimedia adds a host: the
    # provider's declared allowlist must still cover what it really returns.
    assert safe_image_url(photo.image_url, image_hosts_for("wikimedia")) is None, (
        f"{photo.image_url} is not covered by image_hosts_for('wikimedia'); "
        "add the host in src/kitegate/photos.py")


def test_a_disambiguation_subject_still_needs_the_search_fallback():
    # "Mercury" is the live case the fallback exists for. If Wikipedia ever
    # gives it a lead image this test becomes meaningless, so it asserts the
    # shape rather than the absence.
    provider = Wikimedia()
    endpoint = provider.endpoints[0]
    request = provider.lookup(endpoint, "Mercury", 640)
    r = httpx.get(request.url, params=request.params, headers=UA, timeout=30)
    title, photo = provider.read_lookup(r.json(), endpoint)
    assert title == "Mercury"
    if photo is None:
        search = provider.search(endpoint, "Mercury")
        r = httpx.get(search.url, params=search.params, headers=UA, timeout=30)
        assert len(provider.read_search(r.json())) > 1


# -- the moderation endpoint ----------------------------------------------------
def test_the_request_this_code_builds_is_still_accepted():
    photo = wikipedia_photo("tiger")
    data = moderate([photo.image_url])
    results = data.get("results")
    assert isinstance(results, list) and len(results) == 1
    scores = read_scores(results[0])
    # Every category this code thresholds on must still exist in the response,
    # or a threshold would silently never fire.
    missing = [name for name in IMAGE_THRESHOLDS if name not in scores]
    assert not missing, f"the API no longer returns {missing}"


def test_a_clean_photo_passes():
    photo = wikipedia_photo("tiger")
    results = moderate([photo.image_url])["results"]
    worst = worst_category(results[0])
    assert worst is None, f"a tiger photo was judged {worst}"


def test_the_model_looks_at_the_picture_not_the_url():
    """A battle painting must score higher on violence than a tiger.

    This is the check that the whole feature rests on. Comparing two live
    images rather than asserting a fixed number: thresholds are ours to tune
    and the API's scores are explicitly allowed to drift, but a war scene
    scoring no higher than a big cat would mean the picture is not being read.
    """
    tiger = wikipedia_photo("tiger")
    battle = wikipedia_photo("Battle of Waterloo")
    results = moderate([tiger.image_url, battle.image_url])["results"]
    assert len(results) == 2
    tiger_violence = read_scores(results[0]).get("violence", 0.0)
    battle_violence = read_scores(results[1]).get("violence", 0.0)
    assert battle_violence > tiger_violence, (
        f"battle={battle_violence:.4f} tiger={tiger_violence:.4f}: "
        "the scores do not reflect the images")


def test_several_pictures_cost_one_request():
    tiger = wikipedia_photo("tiger")
    jupiter = wikipedia_photo("Jupiter")
    results = moderate([tiger.image_url, jupiter.image_url])["results"]
    assert len(results) == 2  # one call, two verdicts, in request order


# -- the whole chain ------------------------------------------------------------
def test_the_gateway_shows_a_real_photo_after_a_real_scan():
    """Only the model is mocked. The picture and the scan are real."""
    key = scan_key()
    photo = wikipedia_photo("tiger")
    reply = (f"Tigers are the largest cats.\n\n![{photo.title}]({photo.image_url})\n\n"
             f"{photo.caption}")

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).rstrip("/") == SCAN_URL.rstrip("/"):
            # Let the real moderation call through to the network.
            body = json.loads(request.content)
            r = httpx.post(SCAN_URL, json=body,
                           headers={"authorization": f"Bearer {key}"}, timeout=60)
            return httpx.Response(r.status_code, json=r.json())
        return httpx.Response(200, json={
            "id": "up-1", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                         "finish_reason": "stop"}]})

    settings = GatewaySettings(
        api_key="live-key-0123456789abcdef", upstream_url="https://up.invalid/v1",
        child_models={"kids-encyclopedia": "m"},
        child_users=frozenset({"child@example.org"}), pack="uk-school",
        image_policy="scan", image_scan_api_key=key, image_scan_url=SCAN_URL,
        image_hosts=frozenset(image_hosts_for("wikimedia")))
    with TestClient(create_app(settings, transport=httpx.MockTransport(handler))) as c:
        r = c.post("/v1/chat/completions",
                   headers={"Authorization": "Bearer live-key-0123456789abcdef",
                            "x-kitegate-user-email": "child@example.org"},
                   json={"model": "kids-encyclopedia",
                         "messages": [{"role": "user", "content": "tell me about tigers"}]})
    body = r.json()
    content = body["choices"][0]["message"]["content"]
    assert "Tigers are the largest cats." in content
    assert photo.image_url in content, "a clean Wikipedia photo was removed"
    assert body["kitegate"]["images"] == {"shown": 1, "removed": []}
