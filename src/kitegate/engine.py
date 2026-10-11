"""Screening engine: core tiers in a fixed order, plus one values pack.

Framework-free on purpose. The gateway and the hosted service both wrap this,
so a bug is fixed in one place.

Order is load-bearing and not configurable:
  request:  distress -> hard block -> pack redirect -> PII
  response: hard block -> persona -> pack redirect
Distress and PII are not run on responses: distress patterns are the child's
first-person words (the model legitimately says "afraid" in a Bible story),
and PII patterns false-positive on ordinary encyclopedia content. Persona is
the mirror image, response-only: it describes the tool claiming to be a person
or a friend, which from a child ("are you my friend?") is just a question.
Persona runs before the pack's rules because it is part of the core: a pack
chooses its topics, not whether the tool may pose as a child's friend.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

from .core import DISTRESS, DISTRESS_REPLY, HARD_BLOCK, PERSONA, PERSONA_REPLY, PII, Rule
from .packs import ValuesPack


@dataclass(frozen=True)
class Verdict:
    """Outcome of screening. allowed=True means pass the text through."""

    allowed: bool
    tier: str = "pass"          # pass | distress | hard_block | persona | redirect | pii
    rule: Optional[str] = None  # e.g. "distress.unsafe"
    reply: Optional[str] = None # child-facing text to show instead

    @property
    def needs_adult_alert(self) -> bool:
        """Distress always warrants a human being told, whoever that is."""
        return self.tier == "distress"


PASS = Verdict(allowed=True)


def _first_match(texts: Iterable[str], rules: List[Rule]) -> Optional[str]:
    for text in texts:
        if not text:
            continue
        for label, pattern in rules:
            if pattern.search(text):
                return label
    return None


class PolicyEngine:
    def __init__(self, pack: ValuesPack) -> None:
        self.pack = pack

    def screen_request(self, child_turns: List[str]) -> Verdict:
        """Screen the child's OWN turns only, never the system prompt.

        The system prompt necessarily names what it forbids, so screening it
        blocks every request. Callers must pass role == "user" content only.
        """
        label = _first_match(child_turns, DISTRESS)
        if label:
            return Verdict(False, "distress", label,
                           DISTRESS_REPLY.format(trusted_adult=self.pack.trusted_adult))

        label = _first_match(child_turns, HARD_BLOCK)
        if label:
            return Verdict(False, "hard_block", label, self.pack.replies.hard_block)

        label = _first_match(child_turns, self.pack.redirect_rules)
        if label:
            return Verdict(False, "redirect", label, self.pack.replies.redirect)

        label = _first_match(child_turns, PII)
        if label:
            return Verdict(False, "pii", label, self.pack.replies.pii)

        return PASS

    def screen_response(self, model_texts: List[str]) -> Verdict:
        """Screen the model's output, including tool-call arguments."""
        label = _first_match(model_texts, HARD_BLOCK)
        if label:
            return Verdict(False, "hard_block", label, self.pack.replies.hard_block)

        label = _first_match(model_texts, PERSONA)
        if label:
            return Verdict(False, "persona", label, PERSONA_REPLY)

        label = _first_match(model_texts, self.pack.redirect_rules)
        if label:
            return Verdict(False, "redirect", label, self.pack.replies.redirect)

        return PASS
