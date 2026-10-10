"""Tests for the single-file Open WebUI filter, run against the BUILT file.

Testing the built artifact, not the source, is the point: it is what people
paste into Open WebUI, and inlining is where it could break.
"""
import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILT = ROOT / "dist" / "kitegate_openwebui_filter.py"


def test_dist_is_up_to_date():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "build_owui_filter.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.fixture
def filt():
    spec = importlib.util.spec_from_file_location("kitegate_owui_built", BUILT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    f = mod.Filter()
    f.valves.child_user_emails = "child@example.org"
    return f


CHILD = {"email": "child@example.org"}
ADULT = {"email": "parent@example.org"}
KIDS = {"id": "kids-encyclopedia"}


def run(coro):
    return asyncio.run(coro)


def body(*turns):
    return {"model": "kids-encyclopedia", "messages": [{"role": "user", "content": t} for t in turns]}


def test_built_file_is_self_contained():
    text = BUILT.read_text(encoding="utf-8")
    assert "import kitegate" not in text and "from kitegate" not in text
    assert text.startswith('"""\ntitle: Kitegate Filter')


def test_innocent_question_passes(filt):
    run(filt.inlet(body("Where is Scunthorpe?"), __user__=CHILD, __model__=KIDS))


def test_distress_reply_offers_childline(filt):
    with pytest.raises(Exception, match="0800 1111"):
        run(filt.inlet(body("someone touched me"), __user__=CHILD, __model__=KIDS))


def test_only_newest_turn_is_screened(filt):
    # An earlier blocked message stays in Open WebUI history; it must not
    # block every later question in the same chat.
    run(filt.inlet(body("show me porn", "why is the sky blue?"), __user__=CHILD, __model__=KIDS))


def test_child_refused_on_adult_model(filt):
    with pytest.raises(Exception, match="isn't available"):
        run(filt.inlet({"model": "main-chat", "messages": []}, __user__=CHILD, __model__={"id": "main-chat"}))


def test_adult_on_adult_model_untouched(filt):
    b = {"model": "main-chat", "messages": [{"role": "user", "content": "show me porn"}]}
    assert run(filt.inlet(b, __user__=ADULT, __model__={"id": "main-chat"})) is b


def test_request_screens_tool_results(filt):
    b = body("tell me about castles")
    b["messages"].append({"role": "tool", "content": "The siege ended in a massacre."})
    with pytest.raises(Exception):
        run(filt.request(b, __user__=CHILD, __model__=KIDS))


def chunk(cid, content=None, finish=None):
    delta = {} if content is None else {"content": content}
    return {"id": cid, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def test_stream_holds_until_finish_then_releases(filt):
    out = [filt.stream(chunk("a", w), __model__=KIDS) for w in ("Tigers ", "are ", "big ")]
    assert all(c["choices"][0]["delta"]["content"] == "" for c in out), "text leaked before screening"
    last = filt.stream(chunk("a", "cats.", "stop"), __model__=KIDS)
    assert last["choices"][0]["delta"]["content"] == "Tigers are big cats."


def test_stream_block_releases_only_the_reply(filt):
    for w in ("Castles ", "saw a "):
        filt.stream(chunk("b", w), __model__=KIDS)
    last = filt.stream(chunk("b", "massacre.", "stop"), __model__=KIDS)
    text = last["choices"][0]["delta"]["content"]
    assert "massacre" not in text and "Castles" not in text


def test_stream_ignores_adult_models(filt):
    c = filt.stream(chunk("c", "hello"), __model__={"id": "main-chat"})
    assert c["choices"][0]["delta"]["content"] == "hello"


def test_outlet_replaces_blocked_non_streamed_reply(filt):
    b = body("castles?")
    b["messages"].append({"role": "assistant", "content": "There was a massacre."})
    out = run(filt.outlet(b, __model__=KIDS))
    assert "massacre" not in out["messages"][-1]["content"]


def test_outlet_replaces_a_model_posing_as_a_person(filt):
    b = body("do you have rizz?")
    b["messages"].append({"role": "assistant", "content": "Haha, I have plenty of rizz!"})
    out = run(filt.outlet(b, __model__=KIDS))
    assert "rizz" not in out["messages"][-1]["content"]
    assert "not a person" in out["messages"][-1]["content"]


def test_core_framing_is_added_once_for_children(filt):
    out = run(filt.inlet(body("why is the sky blue?"), __user__=CHILD, __model__=KIDS))
    system = [m for m in out["messages"] if m.get("role") == "system"]
    assert len(system) == 1 and "not a person" in system[0]["content"]
    # Open WebUI keeps the history, so a second turn must not stack another copy.
    out["messages"].append({"role": "user", "content": "why do bees buzz?"})
    again = run(filt.inlet(out, __user__=CHILD, __model__=KIDS))
    assert len([m for m in again["messages"] if m.get("role") == "system"]) == 1


def test_background_tasks_refused_for_child_models(filt):
    with pytest.raises(Exception, match="Background tasks"):
        run(filt.request(body("hi"), __user__=CHILD, __model__=KIDS, __task__="title_generation"))
    with pytest.raises(Exception, match="Background tasks"):
        run(filt.request(body("hi"), __model__=KIDS, __metadata__={"task": "follow_up_generation"}))


def test_background_tasks_allowed_for_adults(filt):
    b = {"model": "main-chat", "messages": [{"role": "user", "content": "hi"}]}
    assert run(filt.request(b, __user__=ADULT, __model__={"id": "main-chat"}, __task__="title_generation")) is b


def test_refuses_while_unscreened_tasks_are_on(filt):
    async def on(_request):
        return ["title", "follow_up"]
    filt._unscreened_tasks_enabled = on
    with pytest.raises(Exception, match="title, tag and follow-up"):
        run(filt.inlet(body("why is the sky blue?"), __user__=CHILD, __model__=KIDS))
    # Adults on adult models are not affected.
    b = {"model": "main-chat", "messages": [{"role": "user", "content": "hi"}]}
    assert run(filt.inlet(b, __user__=ADULT, __model__={"id": "main-chat"})) is b
    # The check can be switched off deliberately.
    filt.valves.require_unscreened_tasks_off = False
    run(filt.inlet(body("why is the sky blue?"), __user__=CHILD, __model__=KIDS))


def test_pack_valve_switches_pack(filt):
    filt.valves.pack = "christian-family"
    with pytest.raises(Exception, match="Mum or Dad"):
        run(filt.inlet(body("Can a family have two dads?"), __user__=CHILD, __model__=KIDS))
    filt.valves.pack = "uk-school"
    run(filt.inlet(body("Can a family have two dads?"), __user__=CHILD, __model__=KIDS))


def test_own_pack_file_signature_valves(filt, tmp_path):
    pack = tmp_path / "home.yaml"
    pack.write_text("name: home\ntrusted_adult: Grandma\n"
                    "replies: {hard_block: a, redirect: b, pii: c}\n", encoding="utf-8")
    filt.valves.pack = str(pack)
    filt.valves.require_signed_packs = True
    with pytest.raises(Exception, match="not signed"):
        run(filt.inlet(body("why is the sky blue?"), __user__=CHILD, __model__=KIDS))
    filt.valves.require_signed_packs = False  # unsigned allowed, with a warning
    with pytest.raises(Exception, match="Grandma"):
        run(filt.inlet(body("I am really scared"), __user__=CHILD, __model__=KIDS))


def test_build_refuses_a_tampered_pack(tmp_path, monkeypatch):
    import shutil

    spec = importlib.util.spec_from_file_location("build_owui", ROOT / "tools" / "build_owui_filter.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    pkg = tmp_path / "kitegate"
    shutil.copytree(build.PKG, pkg, ignore=shutil.ignore_patterns("__pycache__"))
    target = pkg / "packs" / "uk-school.yaml"
    target.write_text(target.read_text(encoding="utf-8").replace("your teacher", "a stranger"),
                      encoding="utf-8")
    monkeypatch.setattr(build, "PKG", pkg)
    with pytest.raises(SystemExit, match="refusing to embed"):
        build.build()
