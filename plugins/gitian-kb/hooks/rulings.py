#!/usr/bin/env python3
"""rulings.py -- does a prompt state a standing rule? The owner-ruling heuristic.

prompt_checkpoint.py (UserPromptSubmit) asks `detect(prompt)` for every prompt; an excerpt becomes
a `ruling` event in the session's activity ledger (ledger.py) and costs one short context line on
that same prompt, so the agent can fold the rule into the kb-scribe brief it is already writing. A
rule the owner states in passing ("from now on, never merge with --auto") is exactly what a long
session forgets to write down, and nothing else in the hook layer can see it: it is a sentence in
a prompt, not a tool call. A ruling is a hint -- later checkpoints list it while it is open, but it
never blocks a turn.

PRECISION OVER RECALL. A missed ruling costs nothing (a rule that matters gets restated, and the
agent's own briefs carry it); a false hit costs one context line about something that was never a
rule -- a text heuristic will always have some, which is why a ruling never blocks a turn. Still,
every cue is narrow and a sentence is rejected on any doubt: a question, a leading `/command`,
first-person narration, pasted / injected / quoted text, code, logs and acronym shouting never
count.

What is judged: the prompt is cleaned (injected `<tag ...>...</tag>` blocks, fenced code, inline
code spans and `>` quote lines are dropped -- tags FIRST, so a block goes whole whatever backticks
it carries), the first SCAN_CHARS characters left are split into sentences, and the first sentence
that carries a cue is the excerpt. The five cues sit beside their regexes below.

Stdlib only; imported as a sibling module by the hook scripts that run from this directory.
Fail-open: `detect` never raises -- any internal error is "no ruling".
"""

import re

# Ceiling on the prompt considered at all: every pass below is linear, this bounds the constant.
MAX_PROMPT_CHARS = 1000000
# Only the first 4000 characters LEFT after stripping are scanned -- a rule is stated up front.
SCAN_CHARS = 4000
# The excerpt handed to the ledger, ellipses included.
EXCERPT_MAX = 160
# When the cue sits past the head of a long sentence, the excerpt starts this far before it.
CUE_CONTEXT = 40

# ---- cleaning -----------------------------------------------------------------------------------

# Open and close tokens of an injected block (`<pasted_content ...>`, `<cross-session-message ...>`,
# `<system-reminder>`, `<task-notification>`): arbitrary text that is never the owner's own words.
TAG_TOKEN_RE = re.compile(r"<(/?)([A-Za-z][\w:.\-]*)(?=[\s>/])[^<>]*>")
# A fence line (``` or ~~~, 3+, indented or not, optional info string): fenced code is not prose.
FENCE_LINE_RE = re.compile(r"[ \t]*(`{3,}|~{3,})(.*)")
# An inline code span: a backtick run closed by a run of exactly the same length (CommonMark).
INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.S)
# A span never crosses a blank line, so one stray backtick cannot swallow the rest of the prompt.
PARAGRAPH_BREAK_RE = re.compile(r"\n[ \t]*\n")
# Windows and old-Mac line ends, and the unicode line/paragraph separators, all become "\n".
NEWLINE_RE = re.compile(r"\r\n?|[\u2028\u2029]")
# Typographic quotes and apostrophes become ASCII, so a curly "don't" matches the "don'?t" cues.
QUOTE_TABLE = {0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"'}

# ---- sentences ----------------------------------------------------------------------------------

# A sentence ends at . ! ? (not inside an ellipsis) plus closing quotes/brackets and whitespace, or
# at a newline.
SENTENCE_SPLIT_RE = re.compile(r"(?:(?<=[!?])|(?<=[^.]\.))[\"')\]]*\s+|\n+")
# What may precede a sentence's first word without being part of it: space, bullets, list numbers,
# quote marks -- single characters inside the repeat, so it cannot backtrack catastrophically.
LEAD_RE = re.compile(r"^(?:[\s\-*+#>\u2022\u2013\u2014\"'(\[]|\d{1,3}[.)]\s)+")
# Closing marks that may follow a question mark ("... rebase?)").
CLOSERS = " \"')]}*_"
# Narration about oneself is not an order to the agent: patterns 2-4 skip these first words.
FIRST_PERSON = frozenset(["i", "i'm", "im", "i'll", "i've", "i'd"])
FIRST_WORD_RE = re.compile(r"[A-Za-z']+")
# Code or a log line says nothing about the owner's rules, whatever words it contains.
CODE_MARKERS = ("{", "}", "=>", "==", "::", "$ ")
LOG_START_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|\d{1,2}:\d{2}:\d{2}"
    r"|(?:error|fatal|warn|info|debug|trace)\s*[:\]]",
    re.I,
)

# ---- the cues (case-insensitive unless noted) ---------------------------------------------------

# 1. Durable scope: the sentence says it outlives this turn, which nobody says of a one-off ask.
DURABLE_RE = re.compile(
    r"\b(?:from now on|going forward|from here on(?: out)?|henceforth|in (?:the )?future"
    r"|for future reference|as a (?:general )?rule|standing (?:order|rule|instruction)"
    r"|new rule|the rule is|(?:every|each|any) ?time you|whenever you)\b",
    re.I,
)
# 2. Imperative at a clause start: "never X" / "and always X" is an order, "it never X" a report.
#    "never mind" is a dismissal, not a prohibition.
IMPERATIVE_RE = re.compile(
    r"(?:^|[,;:]\s*|\b(?:and|but|so|please|just|also|then)\s+)"
    r"(?:always|never(?!\s+mind\b)|don'?t\s+ever|do\s+not\s+ever)\b",
    re.I,
)
# 3. Modal: "must never" / "should always" states an obligation whoever the subject is.
MODAL_RE = re.compile(
    r"\b(?:must|should|shall|has\s+to|have\s+to|needs?\s+to)\s+(?:always|never)\b", re.I
)
# 4. Conditional prohibition: "X cannot happen until Y" gates an action on a condition, which is
#    the shape of a rule.
CONDITIONAL_RE = re.compile(
    r"\b(?:never(?!\s+mind\b)|cannot|can'?t|can\s+not|must\s+not|mustn'?t|may\s+not|should\s+not"
    r"|shouldn'?t|do\s+not|don'?t)\b[^.?!\n]{0,80}\b(?:until|unless|before|without)\b",
    re.I,
)
# 5a. (case-sensitive) A lone shouted verdict word is a rule; shouted "never mind" is still not.
SHOUT_WORD_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:NEVER(?!\s+(?i:mind)\b)|ALWAYS|MUSTN'T|MUST|CANNOT)(?![A-Za-z0-9_])"
)
# 5b. (case-sensitive) A shouted run of 2+ words is one when a word in it negates or restricts.
SHOUT_RUN_TRIGGERS = frozenset(["NOT", "DON'T", "DONT", "NO", "ONLY", "STOP", "DO", "CAN'T"])
# Shouted words are letters and apostrophes, 2+ chars; a chunk with a digit or underscore is an
# identifier (MAX_RETRIES), not shouting.
SHOUT_CHUNK_RE = re.compile(r"[\w']+")
SHOUT_CAPS_RE = re.compile(r"[A-Z][A-Z']*[A-Z]")
# Initialisms are capitalised by habit, not emphasis: they never join (nor bridge) a shouted run.
ACRONYMS = frozenset(
    "PR CI CD API URL KB ID OK SSH VPN DNS AWS GCP UI UX DB SQL JSON YAML HTTP HTTPS MCP LLM JWT"
    " PAT TTY GPU CPU RAM APK USB TODO FYI ASAP".split()
)


def _drop_tag_blocks(text):
    """Remove every `<name ...>...</name>` block. A closer pairs with the nearest unclosed opener
    of the same name, so same-name nesting stays inside the outer block; an opener nobody closes
    (`Promise<void>`, `<br>`) is left in place -- dropping to the end would hide the rest of the
    prompt for nothing. One pass over the tags, so a prompt full of unmatched openers is no worse
    than any other."""
    unclosed = {}
    spans = []
    for m in TAG_TOKEN_RE.finditer(text):
        if m.group(0).endswith("/>"):
            continue
        name = m.group(2).lower()
        if not m.group(1):
            unclosed.setdefault(name, []).append(m.start())
        elif unclosed.get(name):
            spans.append((unclosed[name].pop(), m.end()))
    if not spans:
        return text
    merged = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    kept, pos = [], 0
    for start, end in merged:
        kept.append(text[pos:start])
        pos = end
    kept.append(text[pos:])
    return "\n".join(kept)  # a dropped block is a sentence break, never a join


def _drop_fenced_code(text):
    """Remove fenced code blocks line by line. A closing fence is the same character, at least as
    long, with nothing after it; an unterminated fence runs to the end of the prompt (the safe
    side: the rest was probably code). A line that opens AND closes a backtick run on itself
    (```x``` mid-prose) is an inline span, not a fence."""
    kept, fence = [], None
    for line in text.split("\n"):
        m = FENCE_LINE_RE.fullmatch(line)
        if fence is None:
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence = (m.group(1)[0], len(m.group(1)))
                kept.append("")
            else:
                kept.append(line)
        elif (
            m
            and m.group(1)[0] == fence[0]
            and len(m.group(1)) >= fence[1]
            and not m.group(2).strip()
        ):
            fence = None
    return "\n".join(kept)


def _drop_inline_code(text):
    """Remove inline code spans (replaced by a space, so the words around one never fuse). Each
    paragraph is handled alone: a span never crosses a blank line."""
    return "\n\n".join(INLINE_CODE_RE.sub(" ", para) for para in PARAGRAPH_BREAK_RE.split(text))


def _drop_quote_lines(text):
    """Remove `>` lines: a quoted reply or a pasted email is somebody else's words."""
    return "\n".join(line for line in text.split("\n") if not line.lstrip().startswith(">"))


def _clean(prompt):
    """The prose of the prompt: everything that is somebody else's text or code removed."""
    text = NEWLINE_RE.sub("\n", prompt[:MAX_PROMPT_CHARS]).translate(QUOTE_TABLE)
    for drop in (_drop_tag_blocks, _drop_fenced_code, _drop_inline_code, _drop_quote_lines):
        text = drop(text)
    return text[:SCAN_CHARS]


def _shouted_at(body):
    """Offset of the first ALL-CAPS cue (pattern 5) in the sentence, or None. Runs are words
    separated by whitespace only: punctuation, a digit, a lowercase word or an acronym ends one."""
    found = []
    m = SHOUT_WORD_RE.search(body)
    if m:
        found.append(m.start())
    runs, run, prev_end = [], None, None
    for chunk in SHOUT_CHUNK_RE.finditer(body):
        word = chunk.group().strip("'")
        if SHOUT_CAPS_RE.fullmatch(word) is None or word in ACRONYMS:
            run = None
            continue
        if run is None or not body[prev_end:chunk.start()].isspace():
            run = []
            runs.append(run)
        run.append((chunk.start(), word))
        prev_end = chunk.end()
    for run in runs:
        if len(run) >= 2 and run[0][1] in SHOUT_RUN_TRIGGERS:
            found.append(run[0][0])
            break
    return min(found) if found else None


def _cue_at(body):
    """Offset of the cue that makes this sentence a ruling, or None. Patterns 2-4 skip a sentence
    that opens with a first-person word; pattern 1 does not ("I want this from now on: ..." is
    still a ruling), nor does the shouting of pattern 5."""
    m = DURABLE_RE.search(body)
    if m:
        return m.start()
    first = FIRST_WORD_RE.match(body)
    if first is None or first.group().lower() not in FIRST_PERSON:
        for rx in (IMPERATIVE_RE, MODAL_RE, CONDITIONAL_RE):
            m = rx.search(body)
            if m:
                return m.start()
    return _shouted_at(body)


def _judge(sentence):
    """(normalised sentence, offset of its cue) when it is a ruling, else None."""
    body = LEAD_RE.sub("", " ".join(sentence.split()))
    if not body or body.rstrip(CLOSERS).endswith("?"):
        return None
    if any(mark in body for mark in CODE_MARKERS) or LOG_START_RE.match(body):
        return None
    anchor = _cue_at(body)
    return None if anchor is None else (body, anchor)


def _excerpt(body, anchor):
    """The sentence as the ledger shows it: at most EXCERPT_MAX characters including the ellipses,
    cut on a word boundary. It starts at the sentence start; only when the cue would fall in the cut
    tail does it start CUE_CONTEXT characters before the cue instead (with a leading `...`), so the
    line always shows WHY it is a ruling."""
    if len(body) <= EXCERPT_MAX:
        return body
    start = 0
    if anchor >= EXCERPT_MAX // 2:
        start = body.rfind(" ", 0, anchor - CUE_CONTEXT) + 1
    lead = "..." if start else ""
    text = body[start:]
    room = EXCERPT_MAX - len(lead)
    if len(text) <= room:
        return lead + text
    room -= 3
    head = text[:room]
    if text[room] != " ":
        cut = head.rfind(" ")
        if cut > 0:
            head = head[:cut]
    return lead + head.rstrip(" ,;:") + "..."


def detect(prompt):
    """The first sentence of `prompt` that states a standing rule, whitespace-collapsed and at most
    EXCERPT_MAX characters, or None. A non-string, a prompt that starts with `/` (a slash command's
    arguments are not the owner talking) and any internal error are all None."""
    if not isinstance(prompt, str) or prompt.lstrip().startswith("/"):
        return None
    try:
        for sentence in SENTENCE_SPLIT_RE.split(_clean(prompt)):
            hit = _judge(sentence)
            if hit is not None:
                return _excerpt(*hit)
    except Exception:
        return None
    return None
