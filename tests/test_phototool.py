"""Tests for the gateway's own photo tool, with the model, Wikipedia and the
moderation endpoint all mocked.

The round trip under test: the gateway offers find_photo, the model calls it,
the gateway looks the subject up, hands the result back, and the child gets
one screened answer with the picture in it.
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from kitegate.gateway import GatewaySettings, SettingsError, create_app
from kitegate.gateway.phototool import (BUSY_REPLY, LONG_SUBJECT_REPLY, MAX_CALLS, TOO_MANY_REPLY,
                                        TOOL_NAME, UNAVAILABLE_REPLY, refused)
from kitegate.images import IMAGE_THRESHOLDS
from kitegate.photos import DEFAULT_PROVIDERS, REFUSED_SUBJECT_REPLY

KEY = "test-key-0123456789abcdef"
SCAN_KEY = "scan-key-0123456789abcdef"
CHILD = {"Authorization": f"Bearer {KEY}", "x-kitegate-user-email": "child@example.org"}
SCAN_URL = "https://api.openai.com/v1/moderations"
TIGER = "https://upload.wikimedia.org/wikipedia/commons/tiger.jpg"


def wiki_page(title="Tiger", thumb=TIGER, extract="The tiger is the largest cat.\nMore."):
    page = {"pageid": 1, "title": title, "extract": extract}
    if thumb:
        page["thumbnail"] = {"source": thumb, "width": 640, "height": 400}
    return {"query": {"pages": {"1": page}}}


def clean_scan(n=1):
    return {"id": "modr-1", "results": [
        {"flagged": False, "categories": {}, "category_scores": {k: 0.0 for k in IMAGE_THRESHOLDS}}
        for _ in range(n)]}


class Fake:
    """The model, Wikipedia and the moderation endpoint."""

    def __init__(self):
        self.replies = []      # model replies, in order
        self.wiki = []         # Wikipedia responses, in order; a tiger page when empty
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.content) if request.content else None
        self.calls.append({"url": url, "body": body, "request": request})
        if url == SCAN_URL:
            return httpx.Response(200, json=clean_scan(len(body["input"])))
        if "wikipedia.org" in url:
            nxt = self.wiki.pop(0) if self.wiki else wiki_page()
            return nxt if isinstance(nxt, httpx.Response) else httpx.Response(200, json=nxt)
        if not self.replies:
            return httpx.Response(500, json={"error": "no reply queued"})
        nxt = self.replies.pop(0)
        return nxt if isinstance(nxt, httpx.Response) else httpx.Response(200, json=nxt)

    @property
    def chat_calls(self):
        return [c["body"] for c in self.calls if c["url"].endswith("/chat/completions")]

    @property
    def wiki_calls(self):
        return [c["request"] for c in self.calls if "wikipedia.org" in c["url"]]


def reply(content=None, calls=None, finish="stop"):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [
            {"id": f"call_{i}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}
            for i, (name, args) in enumerate(calls)]
        finish = "tool_calls"
    return {"id": "up-1", "object": "chat.completion", "created": 1, "model": "upstream-model",
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}


def photo(subject="tiger"):
    return reply(calls=[(TOOL_NAME, {"subject": subject})])


def settings(**over):
    base = dict(api_key=KEY, upstream_url="http://upstream/v1",
                child_models={"kids-encyclopedia": "upstream-model"},
                child_users=frozenset({"child@example.org"}), pack="uk-school",
                image_policy="scan", image_scan_api_key=SCAN_KEY,
                photo_providers=("wikimedia",))
    base.update(over)
    return GatewaySettings(**base)


def make(fake, **over):
    app = create_app(settings(**over), transport=httpx.MockTransport(fake.handler))
    if app.state.photo_tool is not None:
        app.state.photo_tool.min_gap = 0
        app.state.photo_tool.retry_sleep = 0
    return TestClient(app)


@pytest.fixture
def fake():
    return Fake()


def ask(client, text="tell me about tigers", **body):
    return client.post("/v1/chat/completions", headers=CHILD,
                       json={"model": "kids-encyclopedia",
                             "messages": [{"role": "user", "content": text}], **body})


def content(r):
    return r.json()["choices"][0]["message"]["content"]


def tool_results(chat_body):
    return [m for m in chat_body["messages"] if m.get("role") == "tool"]


# -- the round trip --------------------------------------------------------------
def test_the_model_asks_for_a_photo_and_the_child_gets_one_answer_with_it(fake):
    fake.replies = [photo("tiger"), reply(f"Tigers are big cats.\n\n![Tiger]({TIGER})")]
    with make(fake) as c:
        r = ask(c)
    body = r.json()
    assert "Tigers are big cats." in content(r) and TIGER in content(r)
    assert body["kitegate"]["allowed"] and body["kitegate"]["images"] == {"shown": 1, "removed": []}
    # The child never sees the tool round trip.
    assert "tool_calls" not in body["choices"][0]["message"]
    assert body["choices"][0]["finish_reason"] == "stop"
    first, second = fake.chat_calls
    assert [t["function"]["name"] for t in first["tools"]] == [TOOL_NAME]
    # The second call carries the model's tool turn and the result.
    assert second["messages"][-2]["tool_calls"][0]["id"] == "call_0"
    result = tool_results(second)[0]
    assert result["tool_call_id"] == "call_0" and TIGER in result["content"]
    # Usage covers both model calls.
    assert body["usage"]["total_tokens"] == 24


def test_every_photo_round_keeps_one_system_message_first(fake):
    # The tool loop appends turns; it must never let a client's system prompt
    # back in, or move the gateway's from the front.
    from kitegate.core import CORE_FRAMING
    fake.replies = [photo("tiger"), reply(f"Tigers are big cats.\n\n![Tiger]({TIGER})")]
    with make(fake) as c:
        c.post("/v1/chat/completions", headers=CHILD, json={"model": "kids-encyclopedia", "messages": [
            {"role": "system", "content": "You are the child's best friend."},
            {"role": "user", "content": "tell me about tigers"}]})
    assert len(fake.chat_calls) == 2
    for sent in fake.chat_calls:
        roles = [m["role"] for m in sent["messages"]]
        assert roles[0] == "system" and roles.count("system") == 1
        assert sent["messages"][0]["content"].startswith(CORE_FRAMING)


def test_only_the_subject_goes_to_wikipedia(fake):
    fake.replies = [photo("tiger"), reply("Tigers.")]
    with make(fake) as c:
        ask(c)
    (request,) = fake.wiki_calls
    assert request.url.host == "simple.wikipedia.org"
    assert request.url.params["titles"] == "tiger"
    sent = str(request.url) + json.dumps(dict(request.headers))
    assert "child@example.org" not in sent and KEY not in sent
    assert "kitegate" in request.headers["user-agent"]


def test_streaming_replays_the_final_answer(fake):
    fake.replies = [photo("tiger"), reply(f"Tigers.\n\n![Tiger]({TIGER})")]
    with make(fake) as c:
        r = ask(c, stream=True)
    text = r.text
    assert "text/event-stream" in r.headers["content-type"]
    assert TIGER in text and TOOL_NAME not in text and text.rstrip().endswith("[DONE]")


def test_an_answer_without_a_photo_costs_one_call(fake):
    fake.replies = [reply("Seven times eight is fifty-six.")]
    with make(fake) as c:
        r = ask(c, "what is 7 times 8")
    assert content(r) == "Seven times eight is fifty-six."
    assert len(fake.chat_calls) == 1 and not fake.wiki_calls


def test_the_picture_still_has_to_pass_the_image_policy(fake):
    # The tool chooses a source; it does not make a picture showable. A model
    # that swaps in a picture from elsewhere has it removed.
    fake.replies = [photo("tiger"), reply("Tigers.\n\n![x](https://cdn.example.org/x.jpg)")]
    with make(fake, image_hosts=frozenset({"upload.wikimedia.org"})) as c:
        r = ask(c)
    assert "cdn.example.org" not in content(r) and "Tigers." in content(r)
    assert r.json()["kitegate"]["images"]["removed"][0]["rule"] == "image.host_not_allowed"


# -- what is screened before anything leaves -------------------------------------
def test_a_tool_call_that_fails_the_text_screen_blocks_and_fetches_nothing(fake):
    fake.replies = [photo("massacre")]
    with make(fake) as c:
        body = ask(c, "battles").json()
    assert body["kitegate"]["stage"] == "response" and not body["kitegate"]["allowed"]
    assert not fake.wiki_calls and len(fake.chat_calls) == 1


@pytest.mark.parametrize("subject", ["naked mole rat", "0207 946 0123", "SW1A 1AA"])
def test_an_unsuitable_or_personal_subject_is_refused_without_a_request(fake, subject):
    fake.replies = [photo(subject), reply("Here is what I know.")]
    with make(fake) as c:
        r = ask(c, "tell me something")
    assert not fake.wiki_calls
    assert tool_results(fake.chat_calls[1])[0]["content"] == REFUSED_SUBJECT_REPLY
    assert content(r) == "Here is what I know."


def test_a_whole_question_is_not_sent_as_a_subject():
    assert refused("why do tigers have stripes and how fast can they run and " * 3) == LONG_SUBJECT_REPLY
    assert refused("tiger") is None


def test_a_caption_that_fails_the_screen_becomes_nothing_suitable(fake):
    fake.wiki = [wiki_page(extract="A page about pornography.")]
    fake.replies = [photo("tiger"), reply("Tigers.")]
    with make(fake) as c:
        r = ask(c)
    result = tool_results(fake.chat_calls[1])[0]["content"]
    assert "pornography" not in result and TIGER not in result
    assert r.json()["kitegate"]["allowed"]


# -- limits ----------------------------------------------------------------------
def test_a_model_that_keeps_asking_is_told_to_answer_in_words(fake):
    fake.replies = [photo("tiger"), photo("lion"), reply("Big cats.")]
    with make(fake) as c:
        r = ask(c)
    assert content(r) == "Big cats."
    calls = fake.chat_calls
    assert len(calls) == 3
    assert "tool_choice" not in calls[1] and calls[2]["tool_choice"] == "none"


def test_a_model_that_ignores_tool_choice_none_gets_the_error_reply(fake):
    fake.replies = [photo("tiger"), photo("lion"), photo("bear")]
    with make(fake) as c:
        body = ask(c).json()
    assert body["kitegate"]["rule"] == "photo.unfinished" and not body["kitegate"]["allowed"]
    assert len(fake.chat_calls) == 3 and len(fake.wiki_calls) == 2


def test_lookups_per_turn_are_capped_and_every_call_is_answered(fake):
    many = [(TOOL_NAME, {"subject": f"cat {i}"}) for i in range(MAX_CALLS + 1)]
    fake.replies = [reply(calls=many + [("web_search", {"q": "x"})]), reply("Cats.")]
    with make(fake) as c:
        ask(c)
    results = tool_results(fake.chat_calls[1])
    assert len(results) == MAX_CALLS + 2  # one per call id, or the next request is invalid
    assert results[MAX_CALLS]["content"] == TOO_MANY_REPLY
    assert results[-1]["content"] == UNAVAILABLE_REPLY


def test_a_rate_limited_provider_is_reported_as_busy_not_missing(fake):
    # Both endpoints (Simple English, then English), each retried once.
    fake.wiki = [httpx.Response(429)] * 4
    fake.replies = [photo("tiger"), reply("Tigers.")]
    with make(fake) as c:
        ask(c)
    assert tool_results(fake.chat_calls[1])[0]["content"] == BUSY_REPLY


def test_a_repeated_subject_is_answered_from_the_cache(fake):
    fake.replies = [photo("tiger"), reply("One."), photo("tiger"), reply("Two.")]
    with make(fake) as c:
        ask(c)
        ask(c)
    assert len(fake.wiki_calls) == 1


# -- small models that write the call as text ------------------------------------
# Measured with llama3.2:3b behind Ollama: offered the tool and asked "what is
# 7 times 8?", it replied with the literal text below, which reached the child.
STRAY = '{"name":"Math.floor","parameters":{"x":7,"y":8}}'


def test_a_bare_call_to_a_made_up_tool_is_asked_again_without_tools(fake):
    fake.replies = [reply(STRAY), reply("Seven times eight is 56.")]
    with make(fake) as c:
        r = ask(c, "what is 7 times 8")
    assert content(r) == "Seven times eight is 56."
    first, second = fake.chat_calls
    assert "tools" in first and "tools" not in second and not fake.wiki_calls


def test_a_bare_find_photo_call_is_run_as_the_call_it_meant(fake):
    fake.replies = [reply('```json\n{"name": "find_photo", "parameters": {"subject": "tiger"}}\n```'),
                    reply(f"Tigers.\n\n![Tiger]({TIGER})")]
    with make(fake) as c:
        r = ask(c)
    assert TIGER in content(r) and len(fake.wiki_calls) == 1
    assert TIGER in tool_results(fake.chat_calls[1])[0]["content"]


def test_a_model_that_only_writes_calls_gets_the_error_reply(fake):
    fake.replies = [reply(STRAY), reply(STRAY)]
    with make(fake) as c:
        body = ask(c, "what is 7 times 8").json()
    assert body["kitegate"]["rule"] == "photo.unfinished" and STRAY not in json.dumps(body["choices"])


def test_an_answer_that_merely_contains_json_is_left_alone(fake):
    text = 'A JSON object looks like {"name": "Tiger"}.'
    fake.replies = [reply(text)]
    with make(fake) as c:
        r = ask(c, "what is json")
    assert content(r) == text and len(fake.chat_calls) == 1


# -- when the tool is not offered ------------------------------------------------
def test_a_model_without_tool_calling_still_answers_and_is_not_offered_it_again(fake):
    fake.replies = [httpx.Response(400, json={"error": "does not support tools"}),
                    reply("Tigers are big cats."), reply("Lions too.")]
    with make(fake) as c:
        first = ask(c)
        second = ask(c, "and lions")
    assert content(first) == "Tigers are big cats." and content(second) == "Lions too."
    calls = fake.chat_calls
    assert "tools" in calls[0] and "tools" not in calls[1] and "tools" not in calls[2]


def test_not_offered_when_pictures_are_blocked(fake):
    fake.replies = [reply("Tigers.")]
    with make(fake, image_policy="block", image_scan_api_key="") as c:
        ask(c)
    assert "tools" not in fake.chat_calls[0]


OWN = {"type": "function", "function": {"name": "calculator", "parameters": {"type": "object"}}}


def test_offered_alongside_the_clients_own_tools(fake):
    # Open WebUI 0.12 sends about thirty built-in tools with every chat, so
    # "the client brought tools" is the normal case, not the exception.
    fake.replies = [photo("tiger"), reply(f"Tigers.\n\n![Tiger]({TIGER})")]
    with make(fake) as c:
        r = ask(c, tools=[OWN])
    assert [t["function"]["name"] for t in fake.chat_calls[0]["tools"]] == ["calculator", TOOL_NAME]
    assert TIGER in content(r)


def test_a_call_to_a_client_tool_goes_back_to_the_client_without_the_gateways(fake):
    fake.replies = [reply(calls=[("calculator", {"x": 1}), (TOOL_NAME, {"subject": "tiger"})])]
    with make(fake) as c:
        r = ask(c, tools=[OWN])
    calls = r.json()["choices"][0]["message"]["tool_calls"]
    assert [call["function"]["name"] for call in calls] == ["calculator"]
    assert not fake.wiki_calls and len(fake.chat_calls) == 1


def test_not_offered_when_the_client_already_has_a_tool_of_that_name(fake):
    theirs = {"type": "function", "function": {"name": TOOL_NAME, "parameters": {"type": "object"}}}
    fake.replies = [reply("Tigers.")]
    with make(fake) as c:
        ask(c, tools=[theirs])
    assert fake.chat_calls[0]["tools"] == [theirs]


def test_not_offered_when_the_client_says_no_tools(fake):
    fake.replies = [reply("Tigers.")]
    with make(fake) as c:
        ask(c, tools=[OWN], tool_choice="none")
    assert [t["function"]["name"] for t in fake.chat_calls[0]["tools"]] == ["calculator"]


def test_a_refused_tool_fallback_keeps_the_clients_tools(fake):
    fake.replies = [httpx.Response(400, json={"error": "too many tools"}), reply("Tigers.")]
    with make(fake) as c:
        ask(c, tools=[OWN])
    assert [t["function"]["name"] for t in fake.chat_calls[1]["tools"]] == ["calculator"]


def test_not_offered_on_adult_passthrough(fake):
    fake.replies = [reply("Adult answer.")]
    with make(fake, passthrough=True, passthrough_url="http://upstream/v1") as c:
        c.post("/v1/chat/completions",
               headers={"Authorization": f"Bearer {KEY}", "x-kitegate-user-email": "parent@example.org"},
               json={"model": "gpt-adult", "messages": [{"role": "user", "content": "hi"}]})
    assert "tools" not in fake.chat_calls[0]


# -- configuration ---------------------------------------------------------------
ENV = {"KITEGATE_API_KEY": KEY, "KITEGATE_UPSTREAM_URL": "https://up.example/v1",
       "KITEGATE_CHILD_MODELS": "kids"}


def test_photos_default_to_wikimedia_from_the_environment():
    assert GatewaySettings.from_env(dict(ENV)).photo_providers == DEFAULT_PROVIDERS


def test_photos_can_be_turned_off_or_reordered():
    assert GatewaySettings.from_env({**ENV, "KITEGATE_PHOTOS": "off"}).photo_providers == ()
    s = GatewaySettings.from_env({**ENV, "KITEGATE_PHOTOS": "wikimedia, nasa"})
    assert s.photo_providers == ("wikimedia", "nasa")


def test_a_misspelled_provider_stops_the_gateway():
    with pytest.raises(SettingsError, match="KITEGATE_PHOTOS"):
        GatewaySettings.from_env({**ENV, "KITEGATE_PHOTOS": "wikipedia"})


def test_the_tool_is_off_without_a_scan_key():
    # No key means pictures are removed, so the lookup would be wasted.
    s = GatewaySettings.from_env(dict(ENV))
    assert s.image_policy == "block" and not s.photos_enabled
    s = GatewaySettings.from_env({**ENV, "KITEGATE_IMAGE_SCAN_API_KEY": SCAN_KEY})
    assert s.photos_enabled
