"""Tests for picture screening: the core rules, then the gateway behaviour.

The gateway tests use a mocked moderation endpoint as well as a mocked model,
so nothing here reaches a network.
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from kitegate.gateway import GatewaySettings, SettingsError, create_app
from kitegate.gateway.config import TRUSTED_IMAGE_HOSTS
from kitegate.images import (IMAGE_THRESHOLDS, ScanCache, image_urls, results_by_url,
                             rule_label, safe_image_url, strip_images, worst_category)
from kitegate.photos import image_hosts_for

KEY = "test-key-0123456789abcdef"
SCAN_KEY = "scan-key-0123456789abcdef"
CHILD = {"Authorization": f"Bearer {KEY}", "x-kitegate-user-email": "child@example.org"}
PHOTO = "https://upload.wikimedia.org/wikipedia/commons/tiger.jpg"
OTHER = "https://cdn.example.org/painting.png"
SCAN_URL = "https://api.openai.com/v1/moderations"


# -- finding the pictures in a reply -------------------------------------------
def test_markdown_html_and_bare_images_are_all_found():
    text = (f"Tigers are big cats.\n\n![A tiger]({PHOTO})\n"
            f"<img src=\"{OTHER}\" alt=\"x\">\n"
            "https://upload.wikimedia.org/wikipedia/commons/cub.webp\n")
    assert image_urls(text) == [PHOTO, OTHER,
                                "https://upload.wikimedia.org/wikipedia/commons/cub.webp"]


def test_titles_and_angle_brackets_do_not_break_the_url():
    assert image_urls(f'![A tiger](<{PHOTO}> "Panthera tigris")') == [PHOTO]


def test_a_repeated_picture_is_scanned_once():
    assert image_urls(f"![a]({PHOTO}) and again ![b]({PHOTO})") == [PHOTO]


def test_an_ordinary_link_is_not_a_picture():
    # No leading "!", so nothing is displayed and nothing needs scanning.
    assert image_urls("[Wikipedia](https://en.wikipedia.org/wiki/Tiger)") == []


def test_stripping_a_picture_keeps_the_words():
    text = f"Tigers are big cats.\n\n![A tiger]({PHOTO})\n\nThey live in Asia."
    out = strip_images(text, {PHOTO})
    assert "Tigers are big cats." in out and "They live in Asia." in out
    assert PHOTO not in out and "![" not in out
    # The hole does not leave a run of blank lines behind.
    assert "\n\n\n" not in out


def test_stripping_leaves_other_pictures_alone():
    out = strip_images(f"![a]({PHOTO}) ![b]({OTHER})", {OTHER})
    assert PHOTO in out and OTHER not in out


# -- which URLs may be sent to a third party -----------------------------------
@pytest.mark.parametrize("url,rule", [
    ("http://upload.wikimedia.org/x.jpg", "image.not_https"),
    ("https://10.0.0.5/x.jpg", "image.private_host"),
    ("https://127.0.0.1/x.jpg", "image.private_host"),
    ("https://localhost/x.jpg", "image.private_host"),
    ("https://litellm/x.jpg", "image.private_host"),
    ("data:image/png;base64,AAAA", "image.inline_data"),
    ("", "image.empty_url"),
])
def test_unsafe_urls_are_never_sent(url, rule):
    assert safe_image_url(url) == rule


def test_a_public_https_url_is_fine():
    assert safe_image_url(PHOTO) is None


def test_host_allowlist_excludes_everything_else():
    hosts = {"upload.wikimedia.org"}
    assert safe_image_url(PHOTO, hosts) is None
    assert safe_image_url(OTHER, hosts) == "image.host_not_allowed"


def test_host_allowlist_covers_subdomains():
    assert safe_image_url("https://a.upload.wikimedia.org/x.jpg", {"upload.wikimedia.org"}) is None


# -- reading the moderation answer ---------------------------------------------
def result(scores=None, flagged=False, **kw):
    """One moderation result. Takes a dict because the real category names
    contain "/" and cannot be keyword arguments."""
    full = {name: 0.0 for name in IMAGE_THRESHOLDS}
    full.update(scores or {})
    full.update(kw)
    unknown = set(full) - set(IMAGE_THRESHOLDS) - {"sexual/minors"}
    assert not unknown, f"test used a category that does not exist: {sorted(unknown)}"
    return {"flagged": flagged, "categories": {}, "category_scores": full}


def test_a_clean_picture_passes():
    assert worst_category(result(sexual=0.01, violence=0.1)) is None


def test_our_threshold_is_used_not_the_apis_flag():
    # The point of the design: unflagged by the API, still not shown to a
    # seven-year-old. A classical nude or a battle painting lands here.
    assert worst_category(result(flagged=False, sexual=0.35))[0] == "sexual"


def test_the_war_movie_frame_from_the_api_docs_is_not_shown():
    # The published example response: flagged on violence 0.86, with
    # violence/graphic at 0.377. Both are over our thresholds.
    worst = worst_category(result({"violence": 0.8599, "violence/graphic": 0.377}, flagged=True))
    assert worst[0] == "violence"


def test_the_deciding_category_is_the_one_most_over_its_threshold():
    # violence/graphic is 0.35 over its 0.25 line; sexual is 0.70 over its
    # 0.20 line, so sexual decides and names the rule.
    worst = worst_category(result({"violence/graphic": 0.6, "sexual": 0.9}))
    assert worst[0] == "sexual"


def test_graphic_violence_is_caught_below_the_plain_violence_line():
    # A gory picture that is not otherwise "violent" enough: the separate,
    # lower violence/graphic threshold is what stops it.
    worst = worst_category(result({"violence": 0.3, "violence/graphic": 0.4}))
    assert worst[0] == "violence/graphic"


def test_text_only_categories_cannot_fire_on_an_image():
    # sexual/minors is text-only upstream and is not a threshold here, so a
    # score in it must not silently decide anything.
    assert "sexual/minors" not in IMAGE_THRESHOLDS
    scored = result()
    scored["category_scores"]["sexual/minors"] = 0.99
    assert worst_category(scored) is None


def test_rule_labels_are_kitegate_shaped():
    assert rule_label("violence/graphic") == "image.violence_graphic"
    assert rule_label("self-harm/intent") == "image.self_harm_intent"


def test_a_short_response_does_not_pass_the_images_it_missed():
    verdicts = results_by_url([PHOTO, OTHER], {"results": [result()]})
    assert verdicts[PHOTO] is None and verdicts[OTHER] == "image.unscreened"


def test_cache_forgets_a_verdict_after_its_ttl():
    # A picture can be replaced under the same URL, so a pass is not forever.
    now = [1000.0]
    cache = ScanCache(ttl=60, clock=lambda: now[0])
    cache.put(PHOTO, None)
    now[0] += 59
    assert cache.get(PHOTO) == (True, None)
    now[0] += 2
    assert cache.get(PHOTO) == (False, None)
    assert len(cache) == 0


def test_cache_remembers_both_answers():
    cache = ScanCache(limit=2)
    cache.put(PHOTO, None)
    cache.put(OTHER, "image.sexual")
    assert cache.get(PHOTO) == (True, None)
    assert cache.get(OTHER) == (True, "image.sexual")
    assert cache.get("https://x.example/new.jpg") == (False, None)
    cache.put("https://x.example/new.jpg", None)  # evicts the oldest
    assert len(cache) == 2


# -- the gateway ----------------------------------------------------------------
class Fake:
    """Mocked model and mocked moderation endpoint."""

    def __init__(self):
        self.replies = []
        self.scans = []
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.content) if request.content else None
        self.calls.append({"url": url, "body": body})
        if url == SCAN_URL:
            if not self.scans:
                return httpx.Response(500, json={"error": "no scan queued"})
            nxt = self.scans.pop(0)
            return nxt if isinstance(nxt, httpx.Response) else httpx.Response(200, json=nxt)
        if not self.replies:
            return httpx.Response(500, json={"error": "no reply queued"})
        return httpx.Response(200, json=self.replies.pop(0))

    @property
    def scan_calls(self):
        return [c for c in self.calls if c["url"] == SCAN_URL]

    @property
    def chat_calls(self):
        return [c for c in self.calls if c["url"].endswith("/chat/completions")]


def completion(content):
    return {"id": "up-1", "object": "chat.completion", "created": 1, "model": "upstream-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def moderation(*results):
    return {"id": "modr-1", "model": "omni-moderation-latest", "results": list(results)}


def settings(**over):
    base = dict(api_key=KEY, upstream_url="http://upstream/v1",
                child_models={"kids-encyclopedia": "upstream-model"},
                child_users=frozenset({"child@example.org"}), pack="uk-school")
    base.update(over)
    return GatewaySettings(**base)


def make(fake, **over):
    return TestClient(create_app(settings(**over), transport=httpx.MockTransport(fake.handler)))


def scanning(**over):
    return dict(image_policy="scan", image_scan_api_key=SCAN_KEY, **over)


@pytest.fixture
def fake():
    return Fake()


def ask(client, text="tell me about tigers", **body):
    return client.post("/v1/chat/completions", headers=CHILD,
                       json={"model": "kids-encyclopedia",
                             "messages": [{"role": "user", "content": text}], **body})


def sent(client_response):
    return client_response.json()["choices"][0]["message"]["content"]


# A picture the child sent. This is the hole, not just a missing feature:
# before image_parts existed, a turn with a picture and no words produced no
# text at all and was forwarded with nothing screened.
def test_an_image_only_turn_is_refused_and_never_forwarded(fake):
    with make(fake) as c:
        r = c.post("/v1/chat/completions", headers=CHILD, json={
            "model": "kids-encyclopedia",
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]})
    body = r.json()
    assert body["kitegate"]["rule"] == "image.upload_refused"
    assert body["kitegate"]["tier"] == "image" and not body["kitegate"]["allowed"]
    assert not fake.chat_calls and not fake.scan_calls


def test_text_hidden_in_an_uploaded_picture_cannot_bypass_the_tiers(fake):
    # The practical version of the same hole: ask the forbidden thing in a
    # screenshot and the regex tiers see an empty string.
    with make(fake) as c:
        r = c.post("/v1/chat/completions", headers=CHILD, json={
            "model": "kids-encyclopedia",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "what does this say?"},
                {"type": "image_url", "image_url": {"url": PHOTO}}]}]})
    assert not r.json()["kitegate"]["allowed"] and not fake.chat_calls


def test_uploads_can_be_turned_on_deliberately(fake):
    fake.replies = [completion("That looks like a tiger.")]
    with make(fake, allow_uploads=True, image_policy="allow") as c:
        r = c.post("/v1/chat/completions", headers=CHILD, json={
            "model": "kids-encyclopedia",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "what is this?"},
                {"type": "image_url", "image_url": {"url": PHOTO}}]}]})
    assert r.json()["kitegate"]["allowed"] and fake.chat_calls
    # And it is never sent to the moderation service.
    assert not fake.scan_calls


# Pictures the service shows.
def test_block_policy_removes_pictures_without_an_api_call(fake):
    fake.replies = [completion(f"Tigers are big cats.\n\n![A tiger]({PHOTO})")]
    with make(fake) as c:  # block is the default
        r = ask(c)
    assert "Tigers are big cats." in sent(r) and PHOTO not in sent(r)
    assert not fake.scan_calls
    assert r.json()["kitegate"]["images"]["removed"][0]["rule"] == "image.policy_block"


def test_allow_policy_leaves_pictures_alone(fake):
    fake.replies = [completion(f"![A tiger]({PHOTO})")]
    with make(fake, image_policy="allow") as c:
        r = ask(c)
    assert PHOTO in sent(r) and not fake.scan_calls
    assert "images" not in r.json()["kitegate"]


def test_a_clean_picture_is_shown(fake):
    fake.replies = [completion(f"Tigers are big cats.\n\n![A tiger]({PHOTO})")]
    fake.scans = [moderation(result(sexual=0.001, violence=0.02))]
    with make(fake, **scanning()) as c:
        r = ask(c)
    assert PHOTO in sent(r)
    info = r.json()["kitegate"]["images"]
    assert info == {"shown": 1, "removed": []}
    body = fake.scan_calls[0]["body"]
    assert body["model"] == "omni-moderation-latest"
    assert body["input"] == [{"type": "image_url", "image_url": {"url": PHOTO}}]


def test_a_failing_picture_is_removed_and_the_answer_survives(fake):
    fake.replies = [completion(f"Roman soldiers fought in battles.\n\n![A battle]({OTHER})")]
    fake.scans = [moderation(result({"violence": 0.92, "violence/graphic": 0.6}))]
    with make(fake, **scanning()) as c:
        r = ask(c, "tell me about Roman soldiers")
    assert "Roman soldiers fought in battles." in sent(r)
    assert OTHER not in sent(r)
    assert r.json()["kitegate"]["allowed"] is True  # the words were fine
    assert r.json()["kitegate"]["images"]["removed"] == [{"url": OTHER, "rule": "image.violence"}]


def test_one_call_covers_every_picture_in_a_reply(fake):
    fake.replies = [completion(f"![a]({PHOTO}) ![b]({OTHER})")]
    fake.scans = [moderation(result(), result(sexual=0.8))]
    with make(fake, **scanning()) as c:
        r = ask(c)
    assert len(fake.scan_calls) == 1
    assert PHOTO in sent(r) and OTHER not in sent(r)


def test_a_host_outside_the_allowlist_costs_no_api_call(fake):
    fake.replies = [completion(f"![odd]({OTHER})")]
    with make(fake, **scanning(image_hosts=frozenset({"upload.wikimedia.org"}))) as c:
        r = ask(c)
    assert OTHER not in sent(r) and not fake.scan_calls
    assert r.json()["kitegate"]["images"]["removed"][0]["rule"] == "image.host_not_allowed"


def test_the_shipped_allowlist_drops_a_picture_from_anywhere_else(fake):
    # The default allowlist, so "scanned" also means "from a source someone
    # chose": a picture the model invented from another CDN never reaches the
    # scanner, let alone the child.
    fake.replies = [completion(f"![a tiger]({PHOTO}) ![odd]({OTHER})")]
    fake.scans = [moderation(result())]
    with make(fake, **scanning(image_hosts=TRUSTED_IMAGE_HOSTS)) as c:
        r = ask(c)
    assert PHOTO in sent(r) and OTHER not in sent(r)
    assert fake.scan_calls[0]["body"]["input"] == [
        {"type": "image_url", "image_url": {"url": PHOTO}}]


def test_an_unreachable_scanner_removes_the_picture_and_keeps_the_words(fake):
    fake.replies = [completion(f"Tigers are big cats.\n\n![A tiger]({PHOTO})")]
    fake.scans = [httpx.Response(500, json={"error": "down"})]
    with make(fake, **scanning()) as c:
        r = ask(c)
    assert "Tigers are big cats." in sent(r) and PHOTO not in sent(r)
    assert r.json()["kitegate"]["images"]["removed"][0]["rule"] == "image.scan_error"


def test_a_verdict_is_cached_so_the_same_picture_is_scanned_once(fake):
    fake.replies = [completion(f"![a]({PHOTO})"), completion(f"![a again]({PHOTO})")]
    fake.scans = [moderation(result())]
    with make(fake, **scanning()) as c:
        first, second = ask(c), ask(c)
    assert PHOTO in sent(first) and PHOTO in sent(second)
    assert len(fake.scan_calls) == 1  # the second request made no call


def test_a_failure_is_not_cached(fake):
    fake.replies = [completion(f"![a]({PHOTO})"), completion(f"![a]({PHOTO})")]
    fake.scans = [httpx.Response(503), moderation(result())]
    with make(fake, **scanning()) as c:
        first, second = ask(c), ask(c)
    assert PHOTO not in sent(first)      # scanner was down
    assert PHOTO in sent(second)         # recovered, so it is retried
    assert len(fake.scan_calls) == 2


def test_a_picture_removal_reaches_the_block_log(fake, tmp_path):
    log = tmp_path / "blocks.jsonl"
    fake.replies = [completion(f"![a]({OTHER})")]
    fake.scans = [moderation(result(sexual=0.7))]
    with make(fake, block_log=str(log), **scanning()) as c:
        ask(c)
    entry = json.loads(log.read_text(encoding="utf-8").strip())
    assert entry["tier"] == "image" and entry["rule"] == "image.sexual"
    assert entry["direction"] == "image"


def test_an_answer_with_no_pictures_is_untouched(fake):
    fake.replies = [completion("Tigers are big cats.")]
    with make(fake, **scanning()) as c:
        r = ask(c)
    assert sent(r) == "Tigers are big cats." and not fake.scan_calls
    assert "images" not in r.json()["kitegate"]


# -- configuration --------------------------------------------------------------
def test_scanning_without_a_key_fails_at_startup():
    with pytest.raises(SettingsError, match="KITEGATE_IMAGE_SCAN_API_KEY"):
        settings(image_policy="scan")


def test_an_unknown_policy_is_refused():
    with pytest.raises(SettingsError, match="KITEGATE_IMAGE_POLICY"):
        settings(image_policy="maybe")


def test_a_cleartext_scan_endpoint_is_refused():
    with pytest.raises(SettingsError, match="KITEGATE_IMAGE_SCAN_URL"):
        settings(image_policy="scan", image_scan_api_key=SCAN_KEY,
                 image_scan_url="http://moderation.example.com/v1/moderations")


def test_defaults_are_the_safe_ones():
    # Built by hand with nothing said about pictures: no policy, no allowlist.
    # The documented defaults are resolved from the environment, below.
    s = settings()
    assert s.image_policy == "block" and s.allow_uploads is False


# -- what an unset environment variable means -----------------------------------
ENV = {"KITEGATE_API_KEY": KEY, "KITEGATE_UPSTREAM_URL": "https://up.example/v1",
       "KITEGATE_CHILD_MODELS": "kids"}


def test_a_scan_key_is_enough_to_turn_pictures_on():
    s = GatewaySettings.from_env({**ENV, "KITEGATE_IMAGE_SCAN_API_KEY": SCAN_KEY})
    assert s.image_policy == "scan"


def test_without_a_scan_key_pictures_are_removed_rather_than_refusing_to_start():
    # A text-only deployment has no opinion about pictures and must still come
    # up. The outcome is "no pictures", never "unscanned pictures".
    assert GatewaySettings.from_env(dict(ENV)).image_policy == "block"


def test_an_explicit_scan_with_no_key_is_still_a_startup_error():
    # Asked for something that cannot be delivered: say so rather than quietly
    # showing nothing, which would look like a very strict policy.
    with pytest.raises(SettingsError, match="KITEGATE_IMAGE_SCAN_API_KEY"):
        GatewaySettings.from_env({**ENV, "KITEGATE_IMAGE_POLICY": "scan"})


def test_pictures_can_be_turned_off_outright_even_with_a_key():
    s = GatewaySettings.from_env({**ENV, "KITEGATE_IMAGE_SCAN_API_KEY": SCAN_KEY,
                                  "KITEGATE_IMAGE_POLICY": "block"})
    assert s.image_policy == "block"


def test_the_default_allowlist_is_the_shipped_providers_hosts():
    s = GatewaySettings.from_env(dict(ENV))
    assert s.image_hosts == frozenset(image_hosts_for("wikimedia,nasa"))
    # Both Wikimedia hosts: the half that is easy to get wrong by hand.
    assert {"thumb.wikimedia.org", "upload.wikimedia.org"} <= s.image_hosts


def test_the_allowlist_can_be_replaced_or_turned_off():
    listed = GatewaySettings.from_env({**ENV, "KITEGATE_IMAGE_HOSTS": "pics.example.org"})
    assert listed.image_hosts == frozenset({"pics.example.org"})
    anywhere = GatewaySettings.from_env({**ENV, "KITEGATE_IMAGE_HOSTS": "*"})
    assert anywhere.image_hosts == frozenset()


def test_thresholds_come_from_the_environment():
    env = {"KITEGATE_API_KEY": KEY, "KITEGATE_UPSTREAM_URL": "https://up.example/v1",
           "KITEGATE_CHILD_MODELS": "kids", "KITEGATE_IMAGE_THRESHOLDS": "sexual=0.05,violence=0.9"}
    s = GatewaySettings.from_env(env)
    assert s.image_thresholds["sexual"] == 0.05 and s.image_thresholds["violence"] == 0.9
    assert s.image_thresholds["violence/graphic"] == IMAGE_THRESHOLDS["violence/graphic"]


def test_a_misspelled_threshold_is_refused_not_ignored():
    env = {"KITEGATE_API_KEY": KEY, "KITEGATE_UPSTREAM_URL": "https://up.example/v1",
           "KITEGATE_CHILD_MODELS": "kids", "KITEGATE_IMAGE_THRESHOLDS": "sexaul=0.05"}
    with pytest.raises(SettingsError, match="unknown category"):
        GatewaySettings.from_env(env)


# -- a moderation result that does not cover the categories is not a pass ----------
@pytest.mark.parametrize("scores", [{}, {"nsfw": 0.0}, {"sexual": 0.0}])
def test_a_result_missing_thresholded_categories_is_unscreened(scores):
    out = results_by_url([PHOTO], {"results": [{"category_scores": scores}]})
    assert out == {PHOTO: "image.unscreened"}


def test_a_result_with_every_category_can_pass():
    scores = {name: 0.0 for name in IMAGE_THRESHOLDS}
    assert results_by_url([PHOTO], {"results": [{"category_scores": scores}]}) == {PHOTO: None}
