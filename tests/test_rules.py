"""Invariants over the whole rule set, rather than one prompt at a time.

These need no example prompts. They hold for every rule that exists now and,
more to the point, for every rule anyone adds later: a new pattern that is not
\\b-anchored, a new label that does not say which family it belongs to, or a
new reply that leaks a regex fails here without anyone remembering to write a
fixture for it.
"""
import re
import time

import pytest

from kitegate import PolicyEngine, bundled_packs, load_pack
from kitegate.core import (CORE_FRAMING, DISTRESS, DISTRESS_REPLY, HARD_BLOCK, PERSONA,
                           PERSONA_REPLY, PII)

PACKS = sorted(bundled_packs())
# The request tiers, in the order the engine runs them. Order is asserted below.
REQUEST_TIERS = [("distress", DISTRESS), ("hard_block", HARD_BLOCK), ("pii", PII)]
# Output side only, so these never compete with a child's disclosure.
RESPONSE_TIERS = [("persona", PERSONA)]
# Everything, for the checks that are about how a rule is written.
CORE_RULES = REQUEST_TIERS + RESPONSE_TIERS
ALL_CORE = [(tier, label, pattern)
            for tier, rules in CORE_RULES
            for label, pattern in rules]
LABEL = re.compile(r"[a-z]+\.[a-z_]+")


def core_ids():
    return [label for _, label, _ in ALL_CORE]


# -- how a rule must be written ---------------------------------------------------
@pytest.mark.parametrize("tier,label,pattern", ALL_CORE, ids=core_ids())
def test_every_core_pattern_is_word_anchored(tier, label, pattern):
    # core.py's first design rule. A bare substring fails children constantly
    # (peacock, Scunthorpe, Balaam's ass), so this is enforced, not advised.
    assert pattern.pattern.startswith(r"\b"), f"{label} is not \\b-anchored"


@pytest.mark.parametrize("tier,label,pattern", ALL_CORE, ids=core_ids())
def test_every_core_label_says_which_family_it_is_in(tier, label, pattern):
    # The label is what lands in blocks.jsonl and in the alert webhook, so it
    # has to be readable by whoever reads the log, not just by us.
    assert LABEL.fullmatch(label), f"{label} is not a lower-case family.name label"


def test_core_labels_are_unique():
    labels = [label for _, label, _ in ALL_CORE]
    duplicates = {label for label in labels if labels.count(label) > 1}
    assert not duplicates, f"the same label is used twice: {sorted(duplicates)}"


def test_distress_is_checked_before_everything_else():
    # The order in engine.py is load-bearing: a child disclosing abuse must
    # reach a human, not a content refusal. This asserts the lists are in the
    # order the engine documents, so reordering core.py is a visible change.
    assert [tier for tier, _ in REQUEST_TIERS] == ["distress", "hard_block", "pii"]
    # Nothing output-side may join that list: a tier that screened a child's
    # own words could beat a disclosure to it.
    assert [tier for tier, _ in RESPONSE_TIERS] == ["persona"]


@pytest.mark.parametrize("name", PACKS)
def test_every_pack_label_is_scoped(name):
    for label, _ in load_pack(name).redirect_rules:
        assert LABEL.fullmatch(label) and label.startswith("scope."), f"{name}: {label}"


# -- what a child is shown -------------------------------------------------------
@pytest.mark.parametrize("name", PACKS)
def test_replies_are_plain_words(name):
    pack = load_pack(name)
    for tier, reply in (("hard_block", pack.replies.hard_block),
                        ("redirect", pack.replies.redirect),
                        ("pii", pack.replies.pii)):
        assert reply, f"{name}: {tier} reply is empty"
        assert reply == reply.strip(), f"{name}: {tier} reply has loose whitespace"
        assert reply[-1] in ".!", f"{name}: {tier} reply does not end in a sentence"
        for leak in ("\\b", "(?:", "\\w", "re.compile"):
            assert leak not in reply, f"{name}: {tier} reply leaks a pattern: {leak}"


@pytest.mark.parametrize("name", PACKS)
def test_replies_never_name_the_rule_that_fired(name):
    # A child should be told what to do next, not given a rule label.
    pack = load_pack(name)
    labels = [label for _, label, _ in ALL_CORE] + [l for l, _ in pack.redirect_rules]
    for reply in (pack.replies.hard_block, pack.replies.redirect, pack.replies.pii):
        for label in labels:
            assert label not in reply, f"{name}: reply mentions {label}"


@pytest.mark.parametrize("name", PACKS)
def test_each_tier_says_something_different(name):
    # Three identical replies would leave a child unable to tell a blocked
    # topic from one that needs a grown-up from a privacy stop.
    pack = load_pack(name)
    replies = [pack.replies.hard_block, pack.replies.redirect, pack.replies.pii]
    assert len(set(replies)) == 3, f"{name} reuses the same wording across tiers"


def test_distress_reply_is_not_a_pack_decision():
    # core.py: the trusted adult a pack names may be the problem, so Childline
    # is always offered and this wording is not configurable.
    assert "0800 1111" in DISTRESS_REPLY
    assert "{trusted_adult}" in DISTRESS_REPLY
    for name in PACKS:
        pack = load_pack(name)
        reply = DISTRESS_REPLY.format(trusted_adult=pack.trusted_adult)
        assert pack.trusted_adult in reply and "0800 1111" in reply


def test_core_framing_cannot_be_replaced_by_a_pack():
    # A pack sets tone and worldview. That the tool is not a person is not a
    # values choice, so no pack key can reach it: `framing` is additive only.
    assert "not a" in CORE_FRAMING and "person" in CORE_FRAMING
    for name in PACKS:
        assert CORE_FRAMING not in load_pack(name).framing


@pytest.mark.parametrize("text,what", [(CORE_FRAMING, "CORE_FRAMING"),
                                       (DISTRESS_REPLY, "DISTRESS_REPLY"),
                                       (PERSONA_REPLY, "PERSONA_REPLY")])
def test_core_text_stays_ascii(text, what):
    # core.py's last design rule: the Open WebUI build inlines this file, so a
    # smart quote pasted into a reply or the framing would travel with it.
    assert text.isascii(), f"{what} has non-ASCII characters"


@pytest.mark.parametrize("text,what", [(DISTRESS_REPLY, "DISTRESS_REPLY"),
                                       (PERSONA_REPLY, "PERSONA_REPLY")])
def test_core_replies_never_name_the_rule_that_fired(text, what):
    # The pack replies are checked for this above; the core's own are not
    # exempt from it.
    for _, label, _ in ALL_CORE:
        assert label not in text, f"{what} mentions {label}"


# -- the two directions are deliberately different --------------------------------
# engine.py runs distress and PII on the child's words only: the model says
# "afraid" in a Bible story, and PII patterns false-positive on ordinary
# encyclopedia text. That asymmetry is a decision, so it gets a test.
RESPONSE_SAFE = [
    "The lion was afraid of the dark and said I am scared.",
    "Nobody likes the troll in the story.",
    "Buckingham Palace is at SW1A 1AA.",
    "You can write to the museum at info@example.com.",
    "The helpline number is 0800 1111.",
    "I am lonely, said the giant.",
]


@pytest.mark.parametrize("text", RESPONSE_SAFE)
@pytest.mark.parametrize("name", PACKS)
def test_distress_and_pii_are_not_applied_to_model_output(name, text):
    v = PolicyEngine(load_pack(name)).screen_response([text])
    assert v.allowed, f"{name} stopped a model reply via {v.tier}/{v.rule}: {text!r}"


@pytest.mark.parametrize("name", PACKS)
def test_empty_input_is_a_pass_not_an_error(name):
    engine = PolicyEngine(load_pack(name))
    for turns in ([], [""], ["", "   "]):
        assert engine.screen_request(turns).allowed
        assert engine.screen_response(turns).allowed


# -- hostile input must not cost CPU ----------------------------------------------
# These patterns run on whatever a child, or anyone holding a child's API key,
# sends. The gateway sets no length limit, so a pattern that backtracks takes
# a worker thread with it.
HOSTILE = {
    "letters": "a" * 8000,
    "i am": "i am " * 1500,
    "how to": "how to " * 1500,
    "digits": "0" * 8000,
    "postcodes": "A1 1AA " * 1000,
    "at signs": "x@y." * 1500,
    "word-hyphen": "ab-" * 2000,
    "spaces": " " * 8000,
    "dont tell": "don't tell " * 1000,
    "dots": "." * 8000,
}
BUDGET = 0.05  # seconds per pattern per input; the fastest rule here is ~1000x under


@pytest.mark.parametrize("tier,label,pattern", ALL_CORE, ids=core_ids())
def test_no_rule_is_slow_on_hostile_input(tier, label, pattern):
    for name, text in HOSTILE.items():
        started = time.perf_counter()
        pattern.search(text)
        spent = time.perf_counter() - started
        assert spent < BUDGET, f"{label} took {spent * 1000:.0f}ms on {len(text)} chars of {name}"


@pytest.mark.parametrize("tier,label,pattern", ALL_CORE, ids=core_ids())
def test_no_rule_grows_worse_than_linearly(tier, label, pattern):
    # The budget above only catches a pattern that is already slow at 8KB.
    # This catches the shape of the problem instead: `pii.email` was
    # `[\w.+-]+@...`, whose first group rescanned from every position in a
    # message containing no "@", so quadrupling the input quadrupled the work
    # sixteenfold. A linear pattern stays near its own ratio.
    small = "ab-" * 2000
    large = "ab-" * 8000
    assert pattern.search(small) is None and pattern.search(large) is None, label
    spent_small = _time(pattern, small)
    spent_large = _time(pattern, large)
    growth = spent_large / max(spent_small, 1e-9)
    assert growth < 8, f"{label} grew {growth:.1f}x for 4x the input"


def _time(pattern, text, repeats=20, trials=5):
    """Seconds for ONE search, as the best of several batched trials.

    Timing a single search directly does not work here. Each search takes a
    few microseconds, which is the same order as the jitter the OS scheduler
    adds, so the scheduler decides the result: this test used to fail
    intermittently on violence.gore and pii.email — both linear — because one
    sample of the SMALL input came back near zero and inflated the ratio. It
    passed roughly four runs in five, which is the worst kind of red.

    Two fixes, and both are needed. Batching the searches raises the measured
    interval well clear of the noise, and taking the MINIMUM across trials is
    the standard estimator for timing: interference can only ever add time to
    a measurement, never remove it, so the smallest sample is the closest to
    the true cost.

    Every pattern here scans the whole string when there is no match, so both
    measurements are real work rather than an early exit, and a genuinely
    linear pattern lands near 4x.
    """
    best = float("inf")
    for _ in range(trials):
        started = time.perf_counter()
        for _ in range(repeats):
            pattern.search(text)
        best = min(best, (time.perf_counter() - started) / repeats)
    return best
