"""Text normalisation for the rule tiers: what a person reads, not what was typed.

The rules are regexes over the characters a message contains. A message can
carry the same words in other characters: a zero-width space in the middle of a
word, a full-width or accented letter, a Cyrillic "o", a digit for a letter
("p0rn"), spaced-out letters ("p o r n"), a JSON escape in a tool argument. A
child (or a model told to) only has to find one of those once. So the engine
screens the text as typed AND this normalised form of it, and the first match
in either blocks.

The original is always checked too, so normalising can only add matches: a
rule that fired before still fires.

Every step is linear in the length of the text. The gateway sets no request
length limit, so nothing here may backtrack (tests/test_rules.py holds the
budget). The source is ASCII, like core.py: a look-alike character in this file
would be invisible in review, so every non-ASCII character is an escape.
"""
from __future__ import annotations

import re
import unicodedata

# Look-alikes from other scripts, mapped to the Latin letter they are used for.
# Not a full confusables table: the ones a keyboard or a copy-paste gives you.
# Cyrillic first, then Greek, then a few Latin extensions.
_CONFUSABLES = str.maketrans({
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0445": "x",
    "\u0443": "y", "\u0456": "i", "\u0458": "j", "\u0455": "s", "\u04bb": "h", "\u0501": "d",
    "\u051b": "q", "\u051d": "w", "\u043a": "k", "\u043c": "m", "\u043d": "h", "\u0442": "t",
    "\u0432": "b", "\u04cf": "l", "\u0491": "r",
    "\u03bf": "o", "\u03b1": "a", "\u03bd": "v", "\u03c1": "p", "\u03c4": "t", "\u03b9": "i",
    "\u03ba": "k", "\u03c5": "u", "\u03c7": "x", "\u03b5": "e", "\u03b2": "b",
    "\u0261": "g", "\u0131": "i", "\u0237": "j",
})
# Characters that draw nothing but are not format characters (Cf is stripped as
# a category): fillers and blanks that survive NFKC.
_BLANKS = dict.fromkeys(map(ord, "\u3164\u2800\u115f\u1160\uffa0\u180e\u00ad"), None)

# A digit or symbol standing in for a letter. Applied only inside a word that
# also has a letter in it, so "0800 1111" and "999" are left alone.
_LEET_DIGITS = str.maketrans("013457", "oieast")
# "1" is an "i" in "sh1t" and an "l" in "1ove"; the second table reads it as "l".
_LEET_DIGITS_ELL = str.maketrans("013457", "oleast")
_LEET_SYMBOLS = {"$": "s", "@": "a", "!": "i", "|": "i"}
_LEET_SYMBOL = re.compile(r"[$@!|](?=[a-z0-9])")

# "\u0070" written out inside a JSON string: a tool call's arguments arrive as
# text, and the receiving tool decodes them.
_JSON_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")

# Letters spaced or dotted apart: "p o r n", "p.o.r.n", "p-o-r-n". Three or
# more single characters in a row, so ordinary short words are not joined.
_SPACED = re.compile(r"(?<![^\W_])(?:[^\W_][ .\-_*]){2,}[^\W_](?![^\W_])")
_SEPARATORS = re.compile(r"[ .\-_*]")
_SPACE_RUN = re.compile(r"\s+")


def _unescape(match: "re.Match[str]") -> str:
    return chr(int(match.group(1), 16))


def _unleet(word: str, ell: bool) -> str:
    if not any(ch.isalpha() for ch in word):
        return word
    word = _LEET_SYMBOL.sub(lambda m: _LEET_SYMBOLS[m.group(0)], word)
    return word.translate(_LEET_DIGITS_ELL if ell else _LEET_DIGITS)


def fold(text: str) -> str:
    """Unicode and whitespace only: escapes decoded, accents and styling folded,
    invisible characters removed, look-alike letters mapped, runs of space one.

    Digits and punctuation are left as they were, so this is safe to run the
    digit-sensitive tiers (phone numbers, postcodes) over.
    """
    if not text:
        return text
    text = _JSON_ESCAPE.sub(_unescape, text)
    # NFKD splits an accented letter into the letter and a combining mark, and
    # folds full-width and styled letters to plain ones.
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text
                   if unicodedata.category(ch) not in ("Cf", "Mn", "Me", "Cc") or ch in "\t\n\r")
    text = text.translate(_BLANKS).casefold().translate(_CONFUSABLES)
    return _SPACE_RUN.sub(" ", text)


def normalise(text: str, ell: bool = False) -> str:
    """The text as a reader would see it: folded, spaced letters joined, digits
    and symbols read as the letters they stand in for.

    `ell` reads "1" as "l" instead of "i" ("1ove" rather than "sh1t"). Only for
    the content tiers: "1960s" becomes "i96os", which is not a year.
    """
    if not text:
        return text
    text = _SPACED.sub(lambda m: _SEPARATORS.sub("", m.group(0)), fold(text))
    return " ".join(_unleet(word, ell) for word in text.split(" "))


def variants(texts, leet: bool = True):
    """Each text as typed and, when they differ, in its normalised forms.

    leet=False gives the typed text and its folded form only: for tiers whose
    patterns are made of digits.
    """
    out = []
    for text in texts:
        if not text:
            continue
        out.append(text)
        forms = [normalise(text, False), normalise(text, True)] if leet else [fold(text)]
        for form in forms:
            if form != text and form not in out:
                out.append(form)
    return out
