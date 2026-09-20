#!/usr/bin/env python3
"""transcript_extract.py -- reduce a Claude Code session transcript to its conversation.

Not a hook: this is a CLI the PRIMARY hands to `kb-scribe` in a brief, so the scribe can author
from what was actually said instead of the primary re-emitting it. session-context.sh injects both
halves of that handoff (the transcript path and this command line) into SessionStart context.

    python3 transcript_extract.py <transcript.jsonl> [--last N] [--since ISO8601] [--max-chars N]

Output is plain text on stdout, one block per surviving message, in TIMESTAMP order (the real
JSONL is not strictly chronological -- 68 of 5225 kept records were out of order in the largest
real transcript, and a mid-run interjection is appended well after the reply it redirects):

    ## user
    <text>

    ## assistant
    <text>

What survives is ONLY what the user and the assistant actually said. Dropped, deliberately and
in every case:

  - tool calls (`tool_use`), tool results (`tool_result`) and images -- bulk, not conversation;
  - assistant `thinking` blocks -- never the author's stated position;
  - sidechain records (`isSidechain: true`), i.e. a subagent's own conversation. A subagent
    transcript is a separate file under `<session>/subagents/`, but older Claude Code versions
    inline those records here, so the field is checked rather than assumed absent;
  - hook feedback and local-command envelopes (`isMeta: true`, plus any message whose whole text
    is one of the INJECTION_TAGS envelopes) -- a nudge the model was shown is not something the
    user or the model said;
  - **compaction summaries** (`isCompactSummary: true`, and `isVisibleInTranscriptOnly: true`
    treated the same). These are stored as ordinary `type: "user"` records, so they used to be
    emitted under `## user` as if the owner had typed them -- but the author is the HARNESS.
    They are also enormous: across 259 real transcripts every one of the 40 such records was a
    `user` record carrying both flags with 17k-36k chars of machine prose, and in one 37 MB
    transcript they were 84% of everything this tool kept, evicting the owner's own words from
    `--max-chars`. No real-user counterexample of `isVisibleInTranscriptOnly` exists in that
    corpus, which is why the weaker flag is enough on its own;
  - `<system-reminder>` spans, stripped IN PLACE so the surrounding prompt survives.

What survives that is NOT a `user`/`assistant` record: a **mid-run interjection**. A prompt typed
while the assistant is still working is recorded ONLY as
`{"type": "attachment", "attachment": {"type": "queued_command", "prompt": ...}}` (plus
bookkeeping `queue-operation` records that carry no prompt) and, measured over 107 real enqueues,
essentially never reappears as a `user` record. Those are the owner's course corrections -- the
most load-bearing sentences in a long session -- so this shape is read as role `user`, placed by
its timestamp, and deduped against text already kept (the rare enqueue that DOES also land as a
`user` record must not be emitted twice). The harness also queues agent completion reports the
same way (`commandMode: "task-notification"`, 94 of those 107); they need no special case,
because their whole prompt is one `<task-notification>` envelope and cleans to empty.

Record shape, from the real JSONL (synthetic fixtures mirroring it live in
tests/test_transcript_extract.py): one JSON object per line; `type` names the record kind
(`user`/`assistant`/`attachment`/`system`/`file-history-snapshot`/...); the model message nests
under `message.content`, which is a bare string for a typed human prompt and a list of content
blocks otherwise; `timestamp` is an ISO-8601 instant with a trailing `Z`.

Flags (each independently fail-open -- an unparsable value is ignored, never fatal):
  --last N        keep only the N most recent SURVIVING messages (dropped records never consume
                  the budget, or a thinking-heavy tail would return nothing).
  --since ISO     keep messages at or after that instant. A bare date is accepted; a message with
                  no parsable timestamp is KEPT, since dropping substance over a missing field is
                  the worse failure.
  --max-chars N   default DEFAULT_MAX_CHARS. Keeps the most recent messages that fit and prints
                  `[truncated: N earlier messages omitted]` as the first line. The budget bounds
                  the whole output, banner included -- EXCEPT that the newest message is always
                  emitted whole, so a long final answer is never reduced to a banner.

Fail-open, always: a missing/unreadable/malformed transcript yields whatever parsed (possibly
nothing) and the process always exits 0. Never a traceback, never a non-zero exit.

Tests: plugins/gitian-kb/hooks/tests/test_transcript_extract.py
"""

import json
import re
import sys
from datetime import datetime, timezone

DEFAULT_MAX_CHARS = 120000
BLOCK_SEPARATOR = "\n\n"
# Headroom reserved for the truncation banner whenever the budget forces an omission, so the
# printed banner can never push the output past --max-chars (the banner is ~45 chars).
BANNER_RESERVE = 64

MESSAGE_TYPES = ("user", "assistant")

# The attachment shape a mid-run interjection is stored as -- see the module docstring.
QUEUED_COMMAND_TYPE = "queued_command"

# Flags marking a record the HARNESS authored and merely rendered as the user: a compaction
# summary. Either one is disqualifying; see the module docstring for the corpus measurement
# behind treating the weaker flag as sufficient.
HARNESS_AUTHORED_FLAGS = ("isCompactSummary", "isVisibleInTranscriptOnly")

# Envelopes the harness injects into a message's text rather than into its own record: stripped
# wherever they appear, which also means a message consisting only of one of them reduces to
# empty text and is dropped whole. `<bash-input>` is deliberately ABSENT -- that is the user's own
# typed command, and it is often the substance of the turn.
#
# `command-name`/`command-message` are the chrome of a slash-command invocation (`/handoff`, a
# canned one-word description): the harness's words, not the caller's. Its third sibling
# `command-args` is the OPPOSITE case and lives in UNWRAP_TAGS below.
INJECTION_TAGS = (
    "system-reminder",
    "local-command-caveat",
    "local-command-stdout",
    "local-command-stderr",
    "bash-stdout",
    "bash-stderr",
    "task-notification",
    "command-name",
    "command-message",
)
INJECTION_RE = re.compile(
    "|".join(r"<%s>.*?</%s>" % (tag, tag) for tag in INJECTION_TAGS),
    re.DOTALL,
)

# Envelopes whose WRAPPER is chrome but whose CONTENT is conversation: unwrapped in place rather
# than stripped. `<command-args>` carries whatever the user typed after the slash command -- the
# only part of that three-envelope record they wrote -- and a real slash-command message is
# nothing BUT those three envelopes (measured: 0 chars outside them), so stripping all three
# would erase the turn. An empty args still collapses the message to nothing, which is right: a
# bare `/status` is an action, not something said.
UNWRAP_TAGS = ("command-args",)
UNWRAP_RE = re.compile(
    r"<(%s)>(.*?)</\1>" % "|".join(UNWRAP_TAGS),
    re.DOTALL,
)

# Sort floor for a record with no parsable timestamp and no predecessor to inherit one from.
_TIME_FLOOR = datetime.min.replace(tzinfo=timezone.utc)


def _parse_args(argv):
    """Hand-rolled rather than argparse: argparse exits non-zero and prints usage on a bad value,
    which would break the fail-open contract. Unknown flags and unparsable values are ignored."""
    path = None
    last = None
    since = None
    max_chars = DEFAULT_MAX_CHARS

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--last" and i + 1 < len(argv):
            last = _positive_int(argv[i + 1])
            i += 2
        elif arg == "--since" and i + 1 < len(argv):
            since = _parse_iso(argv[i + 1])
            i += 2
        elif arg == "--max-chars" and i + 1 < len(argv):
            max_chars = _positive_int(argv[i + 1]) or DEFAULT_MAX_CHARS
            i += 2
        elif arg.startswith("-"):
            i += 1  # unknown flag -- ignored, not fatal
        else:
            if path is None:
                path = arg
            i += 1

    return path, last, since, max_chars


def _positive_int(value):
    """int(value) when it is a positive integer, else None -- a 0/negative/garbage value falls
    back to the flag's default rather than producing an empty extract."""
    try:
        parsed = int(str(value).strip())
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _parse_iso(value):
    """Best-effort ISO-8601 parse (accepts a trailing 'Z', an explicit offset, or a bare date);
    None on anything unparsable. A naive value is read as UTC, matching the transcript's own Z."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        candidate = value.strip()
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _clean_text(text):
    """Unwrap keep-the-content envelopes, strip injected ones, trim; "" when nothing substantive
    is left. Unwrapping runs FIRST so an injected envelope nested inside the kept content is
    still seen by the strip pass -- and so the strip pass, which matches on the outer tags, can
    still remove a whole span whose inner args envelope has already been unwrapped."""
    if not isinstance(text, str):
        return ""
    text = UNWRAP_RE.sub(lambda m: m.group(2), text)
    return INJECTION_RE.sub("", text).strip()


def _is_harness_authored(record):
    """True for a record the harness wrote and merely rendered as the user -- a compaction
    summary. Checked BEFORE anything else reads `message`, since the disqualifying evidence is
    on the record, not in its content."""
    return any(record.get(flag) is True for flag in HARNESS_AUTHORED_FLAGS)


def _message_text(record):
    """The conversational text of one transcript record, or "" when it contributes none."""
    if not isinstance(record, dict):
        return ""
    if record.get("type") not in MESSAGE_TYPES:
        return ""
    if record.get("isSidechain") is True:
        return ""
    if record.get("isMeta") is True:
        return ""
    if _is_harness_authored(record):
        return ""

    message = record.get("message")
    if not isinstance(message, dict):
        return ""

    content = message.get("content")
    if isinstance(content, str):
        return _clean_text(content)
    if not isinstance(content, list):
        return ""

    parts = []
    for block in content:
        # Type-gated, not key-gated: a `thinking` block carries its prose under "thinking" and a
        # `tool_result` under "content", so only an explicit {"type": "text"} is conversation.
        if isinstance(block, dict) and block.get("type") == "text":
            cleaned = _clean_text(block.get("text"))
            if cleaned:
                parts.append(cleaned)
    return "\n\n".join(parts)


def _interjection_text(record):
    """The text of a mid-run user interjection, or "" when this record is not one. Same
    exclusions as a `user` record where they apply (sidechain, harness-authored) and the same
    envelope cleaning, which is what makes a queued agent-completion report drop itself."""
    if not isinstance(record, dict):
        return ""
    if record.get("type") != "attachment":
        return ""
    if record.get("isSidechain") is True:
        return ""
    if _is_harness_authored(record):
        return ""
    attachment = record.get("attachment")
    if not isinstance(attachment, dict):
        return ""
    if attachment.get("type") != QUEUED_COMMAND_TYPE:
        return ""
    return _clean_text(attachment.get("prompt"))


def _dedupe(entries):
    """Collapse the one event recorded twice -- an enqueued prompt that ALSO landed as a `user`
    record -- to a single message, keeping whichever came first. Deliberately scoped to
    interjection/record PAIRS rather than applied globally: two separate turns that both say
    "continue" are two things the user said, and a blanket text-dedupe would silently eat one."""
    kept = []
    kept_texts = set()
    interjection_texts = set()
    for role, timestamp, text, is_interjection in entries:
        if is_interjection:
            if text in kept_texts:
                continue
            interjection_texts.add(text)
        elif text in interjection_texts:
            continue
        kept_texts.add(text)
        kept.append((role, timestamp, text))
    return kept


def read_messages(path):
    """Stream the JSONL and return [(role, timestamp_or_None, text)] for surviving messages, in
    TIMESTAMP order. Never slurps the file -- real transcripts reach tens of megabytes; only the
    surviving messages are held, which is a small fraction. Unparsable lines are skipped
    individually; a missing/unreadable file raises, and main() treats that as silence.

    Ordering is a stable sort on (timestamp, line index), where a record with no parsable
    timestamp inherits the last one seen: that keeps such a record adjacent to the record it
    followed in the file instead of sinking it to the front, and the line-index tiebreak keeps
    same-instant records in file order."""
    entries = []
    carried = _TIME_FLOOR
    index = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            index += 1
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except Exception:
                continue

            timestamp = None
            if isinstance(record, dict):
                timestamp = _parse_iso(record.get("timestamp"))
            if timestamp is not None:
                carried = timestamp

            text = _message_text(record)
            role = record.get("type") if isinstance(record, dict) else None
            is_interjection = False
            if not text:
                text = _interjection_text(record)
                if text:
                    # An interjection IS the user talking; the attachment wrapper is transport.
                    role = "user"
                    is_interjection = True
            if not text:
                continue

            entries.append((carried, index, role, timestamp, text, is_interjection))

    entries.sort(key=lambda entry: (entry[0], entry[1]))
    return _dedupe([(e[2], e[3], e[4], e[5]) for e in entries])


def _filter_since(messages, since):
    if since is None:
        return messages
    return [m for m in messages if m[1] is None or m[1] >= since]


def _render_block(message):
    return "## %s\n%s" % (message[0], message[2])


def _fit(blocks, max_chars):
    """Select the newest blocks that fit in max_chars, returning (kept, omitted_count). The
    newest block is always kept, even when it alone exceeds the budget."""
    if not blocks:
        return [], 0

    kept = []
    total = 0
    for index in range(len(blocks) - 1, -1, -1):
        block = blocks[index]
        cost = len(block) + (len(BLOCK_SEPARATOR) if kept else 0)
        # Reserve banner headroom while any older block could still be dropped; reaching index 0
        # means nothing is omitted, so no banner is needed and no reserve applies.
        reserve = 0 if index == 0 else BANNER_RESERVE
        if kept and total + cost + reserve > max_chars:
            break
        total += cost
        kept.append(block)

    kept.reverse()
    return kept, len(blocks) - len(kept)


def render(messages, last=None, since=None, max_chars=DEFAULT_MAX_CHARS):
    """The whole reduction, as a pure function of already-parsed messages."""
    messages = _filter_since(messages, since)
    if last is not None:
        messages = messages[-last:]

    blocks = [_render_block(m) for m in messages]
    kept, omitted = _fit(blocks, max_chars)
    if not kept:
        return ""

    body = BLOCK_SEPARATOR.join(kept)
    if omitted > 0:
        banner = "[truncated: %d earlier message%s omitted]" % (
            omitted,
            "" if omitted == 1 else "s",
        )
        return banner + BLOCK_SEPARATOR + body + "\n"
    return body + "\n"


def main(argv):
    path, last, since, max_chars = _parse_args(argv)
    if not path:
        return
    try:
        messages = read_messages(path)
    except Exception:
        return  # missing file, a directory, an unreadable path -- silent, exit 0
    out = render(messages, last=last, since=since, max_chars=max_chars)
    if out:
        sys.stdout.write(out)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception:
        pass  # fail-open: never a traceback, never a non-zero exit
    sys.exit(0)
