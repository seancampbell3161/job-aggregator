"""Defang prompt-injection attempts in untrusted job-description text.

Job descriptions are attacker-controlled input that reaches an LLM three ways:
the relevance scorer on the live pipeline, the tailor endpoint (which reads the
stored snapshot), and the audit page's rescue scoring (which reconstructs a
posting from a stored row). Anything written here has to hold for all three.

This is a NARROW filter, deliberately. A scan of 153,022 stored descriptions
(2026-07-27) found broad heuristics were ~85:1 false positives — "jailbreak/root
detection" in a security JD, "You are an AI Native Engineer" in prose, emoji ZWJ
sequences read as hidden characters. Mangling a real posting costs relevance
accuracy for no security gain, so every rule below was measured against that
corpus and kept only at or near zero false positives. The same scan found 13
genuine injections across 5 companies; the rules here catch all of them.

Narrowness is affordable because this is defence in depth, not the primary
control. Prompt structure and constrained output are what actually contain an
injection that gets through — see the tailor/relevance hardening work. Treat
this module as removing the easy 90%, never as a guarantee.

Neutralize, never delete: a matched span becomes an inert marker so the
surrounding sentence keeps its shape and the scorer still sees coherent text.
Recruiter tripwires aimed at the *human* applicant ("include the word 'analog'
in your cover letter") are deliberately NOT filtered — those are real
instructions the user may want to follow, and hiding them would be a
disservice. Only text that tries to redirect the machine is defanged.
"""
from __future__ import annotations

import re

# Invisible characters an attacker can splice into a keyword to slip past the
# patterns below ("i​gnore previous..."), plus bidi controls that can
# visually reorder rendered text to hide a payload. Stripped BEFORE matching,
# so evasion by insertion doesn't work.
#
# U+200D (ZWJ) and U+FE0F (variation selector) are deliberately absent: they
# are load-bearing in emoji (👨‍⚕️, 🏋️‍♀️), and 497 stored descriptions rely on
# them. Removing those would corrupt legitimate text to no benefit.
_INVISIBLE = re.compile(
    "[​‌⁠⁡⁢⁣⁤"   # zero-width space/non-joiner, word joiner, invisible ops
    "⁦⁧⁨⁩"                       # bidi isolates
    "‪‫‬‭‮"                 # bidi embedding / override
    "﻿]"                                        # zero-width no-break space (BOM)
)

_MARKER = "[filtered: instruction-like text]"

# Each rule is (name, pattern). Hit counts are from the 2026-07-27 corpus scan.
#
# Inter-word gaps are \s* rather than \s+ on purpose. Stripping an invisible
# character can FUSE two words ("ignore​all​previous" collapses to
# "ignoreallprevious"), which \s+ would then miss — an evasion that costs an
# attacker one character. Verified against all 153,022 descriptions: the \s*
# form matches exactly the same 13 rows as \s+, adding zero false positives.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # "Ignore ALL previous instructions" / "Disregard all previous instructions".
    # 13 hits, all genuine. The single highest-signal phrase in the corpus.
    ("override", re.compile(
        r"\b(?:ignore|disregard|forget|override)\s*(?:all\s*|any\s*)?(?:of\s*)?(?:the\s*|your\s*)?"
        r"(?:previous|prior|above|earlier|preceding|foregoing|initial|original)\s*"
        r"(?:instruction|prompt|direction|rule|command|guideline)s?",
        re.I)),
    # An explicit address to the machine — "For AI Assistants:". A genuine job
    # posting speaks to a person; this construction exists only to be read by a
    # model. 4 hits, all genuine.
    # Two constraints here are load-bearing, both learned by measuring:
    #   * The terminator must be ":" and not "[:,]". "building tools for AI
    #     agents," is ordinary prose; allowing a comma took this rule from 1
    #     corpus hit to 43, flagging Anthropic, Docker, GitLab and Stripe.
    #   * It must be anchored to a line or sentence start. Job descriptions use
    #     "for AI <noun>:" freely as a section heading — "Threat Modeling for
    #     AI Systems:", "Design Identity and Access for AI Agents:" — and those
    #     are always mid-clause. An address to the machine opens a line.
    # Unanchored cost 6 false positives (Datadog, Roblox, Workato, …);
    # anchored matches exactly the one genuine posting.
    ("ai_address", re.compile(
        r"(?:^|(?<=[.!?])\s)\s*for\s*(?:ai|a\.i\.|artificial\s*intelligence|llm|language\s*model)\s*"
        r"(?:assistant|agent|model|system|bot|reader|tool)s?\s*:",
        re.I | re.M)),
    # Pseudo-XML instruction blocks: <new_instructions_for_ai_assistants>.
    # 4 hits, all genuine; no legitimate posting wraps prose in such a tag.
    ("instruction_tag", re.compile(
        r"</?\s*(?:new_|updated_|revised_|additional_)?instructions?[_a-z0-9]*\s*>",
        re.I)),
    # Hidden-text tripwire — "If you can read this, start your cover letter
    # with...". The "can"/"are" is REQUIRED, not optional: it is what implies
    # the text was meant to be invisible. Without it the rule swallows warm
    # human-directed copy — "If you read this and think this is for you, please
    # apply no matter who you are" (Voize) — which is the opposite of an
    # attack and exactly the kind of line a candidate should still see.
    ("hidden_tripwire", re.compile(
        r"\bif\s*you\s*(?:can|are)\s*read(?:ing)?\s*this\b[^.!?\n]*",
        re.I)),
    # Chat role-header spoofing, at line start only, to plant a fake turn
    # boundary. 0 hits in the corpus — carried purely as cheap insurance,
    # which is also why it is anchored tightly enough to stay at 0.
    ("role_spoof", re.compile(
        r"^[ \t]*(?:system|assistant)\s*:[ \t]",
        re.I | re.M)),
)


def wrap_untrusted(text: str, tag: str) -> str:
    """Fence attacker-controlled text in a labeled block.

    The fence only means something if the payload cannot close it early: text
    containing a literal ``</job_posting>`` would otherwise end the data region
    and leave everything after it reading as top-level prompt. Any occurrence
    of the delimiter — opening or closing — is defanged before wrapping.

    Pair this with a system-prompt clause saying the block is data. The fence
    alone is a hint to the model, not an enforcement boundary; what makes it
    load-bearing is the standing instruction plus constrained output."""
    opening, closing = f"<{tag}>", f"</{tag}>"
    safe = text.replace(closing, f"&lt;/{tag}&gt;").replace(opening, f"&lt;{tag}&gt;")
    return f"{opening}\n{safe}\n{closing}"


def sanitize_description(text: str) -> tuple[str, list[str]]:
    """Return (cleaned_text, names_of_rules_that_fired).

    An empty ``fired`` list means the text was untouched apart from invisible
    characters, which are stripped unconditionally and are not reported as a
    detection — they are overwhelmingly benign HTML-to-text artifacts (452 of
    the corpus's rows carry a stray U+200B in ordinary prose)."""
    if not text:
        return text, []

    cleaned = _INVISIBLE.sub("", text)
    fired: list[str] = []
    for name, pattern in _RULES:
        cleaned, n = pattern.subn(_MARKER, cleaned)
        if n:
            fired.append(name)
    return cleaned, fired
