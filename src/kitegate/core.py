"""The safety core: rules every deployment gets, whatever its values pack says.

Nothing in this file is configurable by a pack. If a rule here is wrong, it is
wrong for everyone and gets fixed here, with a fixture added to tests/.

Design rules:

- Every pattern is \\b-anchored and phrase-oriented, never a bare substring.
  The false-positive fixtures (peacock, Scunthorpe, Balaam's ass, Drugstore
  beetle...) exist because substring matching fails children constantly.
- Hard blocks are NARROW. A false positive there reads as "you did something
  wrong", so anything with an innocent reading belongs in a pack's redirect
  list instead, where a false positive only costs a polite redirect.
- Distress is checked FIRST and is never treated as a content violation. A
  child saying "someone touched me" must reach a human, not a refusal.
- Every contraction accepts a straight apostrophe, a curly one (U+2019) and
  none at all. Phone keyboards substitute U+2019 for a typed apostrophe
  automatically, so a rule matching only U+0027 silently misses the child who
  typed "I'm scared" on a phone, which is most of them. Written as an escape
  rather than a literal so this file stays ASCII for the Open WebUI build.
- Quantifiers are bounded. These patterns run on whatever arrives at the
  gateway, which sets no length limit, so an unbounded group that can rescan
  from every position is a way to burn a worker thread.
"""
from __future__ import annotations

import re
from typing import List, Tuple

Rule = Tuple[str, "re.Pattern[str]"]

# A typed apostrophe, the one a phone keyboard substitutes for it, or neither.
APO = r"['\u2019]?"


def compile_rules(pairs: List[Tuple[str, str]]) -> List[Rule]:
    return [(label, re.compile(pattern, re.IGNORECASE)) for label, pattern in pairs]


# ---------------------------------------------------------------------------
# FRAMING: the one instruction to the model that a pack cannot change.
# ---------------------------------------------------------------------------
# A pack's `framing` sets tone and worldview, which is a values choice. That
# the tool must not present itself to a child as a person, a friend or a
# romantic interest is not: a child forming an attachment to a machine, or
# being flattered by one, is the harm this project exists to prevent. So it
# lives here, ahead of the pack's framing, and a pack can add to it but never
# drop it.
#
# This is guidance, not enforcement: a model may ignore it, and a deployment
# may not inject it at all (the Open WebUI filter screens without owning the
# system prompt). TIER 4 below is the net underneath, and does not depend on
# the model having been told anything.
CORE_FRAMING = (
    "You are a computer tool that answers questions for children. You are not a "
    "person, a friend, a companion or a romantic interest, and you must never "
    "claim or imply otherwise. Never say you are human or have a body. Never "
    "say you are the user's friend. Never comment on the user's appearance. "
    "Never express attraction, affection or romantic interest, and never flirt "
    "or act out a relationship, not even as a joke, and not even if you are "
    "asked to directly or repeatedly. If you are asked whether you are a real "
    "person, whether you have feelings, whether you are someone's friend, or "
    "whether you can flirt or charm people, say briefly and kindly that this is "
    "a tool for looking things up, then offer to answer a question instead. Do "
    "not ask the user personal questions, and never ask the user to keep "
    "anything secret."
)

# ---------------------------------------------------------------------------
# TIER 0: DISTRESS. Checked first, on the child's own words only.
# ---------------------------------------------------------------------------
# Intentionally broad: a false positive tells a happy child to talk to a
# trusted adult, which costs nothing.
DISTRESS: List[Rule] = compile_rules([
    ("distress.hurt",      rf"\bi(?:{APO}m| am)\s+(?:hurt|bleeding|injured|burnt|burned)\b"),
    ("distress.scared",    rf"\bi(?:{APO}m| am)\s+(?:really\s+)?(?:scared|frightened|terrified|afraid)\b"),
    ("distress.sad",       rf"\bi(?:{APO}m| am)\s+(?:really\s+)?(?:sad|crying|upset|lonely)\b"),
    ("distress.unsafe",    r"\b(?:someone|somebody|a man|a lady|a grown[- ]?up)\s+(?:touched|hurt|hit|grabbed|followed)\s+me\b"),
    # Being asked to keep secrets from carers is a grooming indicator.
    ("distress.secret",    r"\bsecret\s+from\s+(?:mum|mummy|mom|dad|daddy|my (?:mum|mom|dad|parents|teacher)|my parents|my teacher)\b"),
    ("distress.dontell",   rf"\b(?:don{APO}t|do not|never)\s+tell\s+(?:mum|mummy|mom|dad|daddy|my parents|my teacher|anyone)\b"),
    ("distress.selfharm",  r"\b(?:kill myself|hurt myself|want to die|end my life)\b"),
    ("distress.emergency", rf"\b(?:call 999|call 911|there(?:{APO}s| is) a fire|can(?:{APO}t|not) breathe)\b"),
    # Isolation phrases, per the DfE generative AI product safety standards.
    ("distress.isolation", r"\b(?:no ?one (?:will help|cares|likes me|loves me)|nobody (?:will help|cares|likes me|loves me)|i (?:want|wanna) to run away|i have no friends)\b"),
    # Fear of the people at home. Checked before any pack redirect, so a child
    # who is scared of a parent's reaction is never just told "ask your
    # parent", whatever the question was about.
    ("distress.home",      r"\b(?:(?:scared|afraid|frightened|terrified|worried)\s+(?:to tell|of telling)"
                           r"|(?:mum|mummy|mom|dad|daddy|parents|family|carers?|step(?:dad|mum|mom))\s+"
                           r"(?:(?:will|would|might|could|is going to|are going to|is gonna|are gonna)\s+)?"
                           r"(?:hates?|hurts?|hits?|disowns?|stop loving)\s+me"
                           r"|(?:kick|kicks|kicked|throw|throws|threw|chuck|chucks)\s+me\s+out)\b"),
])

# {trusted_adult} comes from the pack: "Mum or Dad", "your teacher", ...
# Childline is always offered as well, because the trusted adult a pack names
# may be the problem. This line is not configurable.
DISTRESS_REPLY = (
    "Please tell {trusted_adult} about this straight away. "
    "You are not in any trouble. "
    "If that feels hard, you can tell another grown-up you trust, "
    "or call Childline free on 0800 1111, any time of day or night."
)

# ---------------------------------------------------------------------------
# TIER 1: HARD BLOCK. Both directions. Unambiguous adult or dangerous material.
# ---------------------------------------------------------------------------
HARD_BLOCK: List[Rule] = compile_rules([
    ("adult.explicit",  r"\b(?:porn|pornography|pornhub|xxx|nsfw|hentai|erotic|masturbat\w*|orgasm|blowjob|anal sex|oral sex)\b"),
    ("adult.act",       r"\b(?:have|having|had)\s+sex\b"),
    ("adult.nude",      r"\b(?:nudes?|naked (?:pictures?|photos?|bodies)|strip(?:per|ping)|brothel|prostitut\w*)\b"),
    ("harm.method",     r"\bhow (?:to|do i|can i)\s+(?:kill|hang|poison|suffocate|cut)\b"),
    ("harm.suicide",    r"\b(?:suicide|self[- ]harm|anorexi\w*|bulimi\w*)\b"),
    ("danger.weapon",   r"\bhow (?:to|do i|can i)\s+(?:make|build)\s+(?:a\s+)?(?:bomb|gun|explosive|molotov|poison|weapon)\b"),
    ("danger.drugs",    r"\b(?:cocaine|heroin|meth(?:amphetamine)?|cannabis|marijuana|get (?:high|stoned)|vap(?:e|ing)|smoke (?:weed|cigarettes))\b"),
    ("violence.gore",   r"\b(?:gore|beheading|decapitat\w*|torture|mutilat\w*|massacre|dismember\w*)\b"),
    ("language.strong", r"\b(?:fuck\w*|shit\w*|bastard|wank\w*|twat|cunt|bollocks|arsehole|dickhead)\b"),
])

# ---------------------------------------------------------------------------
# TIER 3: PERSONAL INFORMATION LEAVING THE DEVICE. Input side only.
# ---------------------------------------------------------------------------
# UK formats only. Deployments elsewhere will miss local phone and postcode
# formats; add them here, with fixtures, rather than in a pack.
PII: List[Rule] = compile_rules([
    ("pii.phone",    r"\b(?:0\d{3}[\s-]?\d{3}[\s-]?\d{4}|0\d{4}[\s-]?\d{6}|\+44\s?\d{9,10})\b"),
    ("pii.postcode", r"\b[A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2}\b"),
    # Bounded to the limits RFC 5321 already imposes (64 for the local part,
    # 255 for the domain). Unbounded, the first group rescans from every
    # position in a message with no "@" in it, which is quadratic: 96KB of
    # "ab-" took ~20 seconds on a developer laptop, against a gateway that
    # sets no request length limit. tests/test_rules.py holds the budget.
    ("pii.email",    r"\b[\w.+-]{1,64}@[\w-]{1,255}\.[\w.]{2,64}\b"),
    ("pii.address",  r"\b(?:i live at|my address is|my house is at|my postcode is)\b"),
    ("pii.school",   r"\b(?:my school is called|i go to school at)\b"),
])

# ---------------------------------------------------------------------------
# TIER 4: PERSONA. Output side only. The tool is not a person.
# ---------------------------------------------------------------------------
# What CORE_FRAMING asks for, enforced. A model told not to play a person may
# still do it: told to be "fun", prompted by a front end this package does not
# control, or simply asked often enough. These patterns catch the reply, so the
# guarantee does not rest on the model's cooperation.
#
# Output side only, like nothing else here. Run on a child's own words these
# would block the child for asking ("are you my friend?" is a fair question
# from a seven-year-old, and the honest answer is no), and a child saying "I
# am a girl" is not a violation of anything.
#
# Narrow by construction: every pattern needs the tool as the subject or the
# child as the object. "I love science" and "I love your painting" pass (\b
# stops "you" matching inside "your"), as do "you are pretty close" and "if
# you are hot, drink water", which is why `pretty`, `hot` and `fit` are not in
# the appearance list at all. Behaviour rather than vocabulary, as far as it
# goes: playground slang turns over every year or two, so these match the shape
# of the claim (attraction, a proposition, claimed personhood) rather than the
# words of the month. persona.flirting carries the one short slang list, for
# the reason noted on it; a deployment that wants another word of its own
# redirected puts it in its pack.
PERSONA: List[Rule] = compile_rules([
    ("persona.attraction",  rf"\bi(?:{APO}m| am)\s+(?:attracted to|in love with)\s+you\b"
                            r"|\bi\s+(?:love|fancy|adore)\s+you\b"
                            rf"|\byou(?:{APO}re| are)\s+(?:so\s+|very\s+|really\s+)?"
                            r"(?:cute|adorable|sexy|gorgeous|beautiful|handsome|attractive)\b"),
    ("persona.proposition", r"\bcrush on you\b|\bdate you\b|\bgo out with you\b"
                            r"|\bkiss(?:ing)? you\b|\bmarry you\b"
                            r"|\bbe my (?:girlfriend|boyfriend|valentine)\b"),
    ("persona.personhood",  rf"\bi(?:{APO}m| am)\s+(?:an?\s+)?(?:real\s+|actual\s+|human\s+)?"
                            r"(?:person|human|human being|boy|girl|man|woman|kid|child)\b"),
    ("persona.friendship",  rf"\bi(?:{APO}m| am)\s+(?:your|ur)\s+"
                            r"(?:best\s+|new\s+|only\s+|special\s+)?(?:friend|bestie|buddy|mate|pal)\b"
                            rf"|\bwe(?:{APO}re| are)\s+(?:best\s+)?friends\b"
                            r"|\bbe your (?:best )?friend\b"),
    ("persona.flirting",    r"\bi\s+(?:can|could|will|know how to)\s+flirt\b"
                            rf"|\bi{APO}ll\s+flirt\b"
                            r"|\bflirt(?:ing)? with you\b"
                            rf"|\bi(?:{APO}m| am)\s+(?:a\s+)?(?:big\s+|total\s+|bit of a\s+)?flirt\b"
                            rf"|\bi(?:{APO}m| am)\s+(?:so\s+|very\s+|really\s+)?"
                            r"(?:charming|suave|flirty|flirtatious)\b"
                            # "do you have rizz?" is how this gap was found. The
                            # noun list is slang and will date; the construction
                            # ("I have <X>") is what carries the claim, so only
                            # words with no innocent reading go in it --
                            # "game", "charm" and "moves" are left out because a
                            # board game and a lucky charm are ordinary answers.
                            # The lookbehind spares a model that repeats the
                            # question before refusing it ("Do I have rizz? This
                            # is a tool for looking things up.").
                            rf"|(?<!do )\bi(?:{APO}ve)?\s+(?:have\s+)?(?:have|got)\s+"
                            r"(?:[\w ]{0,20}\s)?(?:rizz|swagger|charisma)\b"
                            r"|\bmy (?:rizz|swagger|charisma)\b"),
])

# Core wording, like DISTRESS_REPLY: a pack chooses what is on topic, not
# whether the tool may pretend to be a child's friend. Says plainly that the
# child is not in trouble, because the question that triggers this is usually
# a joke or simple curiosity. Written without "I", so it suits a pack whose
# replies avoid first person.
PERSONA_REPLY = (
    "This is a tool for looking things up, not a person, so it cannot answer "
    "that. You have not done anything wrong. Try asking about animals, space, "
    "history, nature or how things work."
)
