#!/usr/bin/env python3
"""pr_poll.py -- ask GitHub, in ONE call, which of a session's watched pull requests have landed.

Why it exists: a pull request is merged ON GITHUB, usually long after the command that started it.
`gh pr merge --auto` only ARMS a merge; the merge itself happens when CI goes green, and nothing the
session runs afterwards ever shows it. The only way to know a PR merged is to ask, so the Stop
checkpoint (publish_reminder.py) polls the session's watch list through this module (ledger.py
keeps the list: `{repo, number|null, branch|null, why, addedAt, polledAt}`). The poll runs inside a
hook that gates a turn end, so the budget is one `gh api graphql` process, a 4 s cap and no retry: a
poll that fails costs one missed detection, and the next checkpoint asks again.

One query, many questions. Entries are grouped by repository -- one `repository(owner:, name:)`
alias per repo (`r0`, `r1`, ...) -- and each entry becomes one field inside its repository: `p<N>`
for PR number N, `b<J>` for a branch (the newest PR whose head is that branch, in any state; J
counts the branches of that repository). The keymap `build_query` returns is
`{repo alias: {field alias: entry key}}`, which is how `parse_results` maps an answer back to its
entry without ever deriving anything from the response text. An entry's key is `owner/name#650` or
`owner/name@branch`; `entry_key` is the single place that key is spelled, exposed so ledger.py can
key its watch identically and a result can never be filed under a key the watch does not know.

Why validation is strict: the values spliced into the document come from commands the session ran
and from branch names, i.e. text somebody other than this code chose. Each piece must match a tight
allow-list (owner and name `[A-Za-z0-9_.-]+`, branch `[A-Za-z0-9._/-]{1,200}`, number a positive
int) and anything else is SKIPPED -- never escaped and hoped for -- so a hostile value costs the
poll one entry and the query nothing. The anchors are `fullmatch`, not `$`: `$` also matches
before a trailing newline, which would let `main\\n` into the document.

Fail-open everywhere: every public function returns None / `{}` instead of raising, and nothing in
here prints. `gh` exits non-zero when GitHub answers with `errors` beside `data` (a repository the
token cannot see, a number that does not exist) yet still prints `data`; that answer is KEPT,
because one dead entry must not blind the poll for the rest.

Stdlib only; imported as a sibling module by the hooks that run from this directory.
Tests: plugins/gitian-kb/hooks/tests/test_pr_poll.py (one class runs the real subprocess path
against a fake `gh` placed first on PATH).
"""

import json
import os
import re
import subprocess

DEFAULT_TIMEOUT = 4.0

# ledger.py caps a watch at 12; this bounds the query (and the argv carrying it) should a
# hand-edited state file hold more. The entries past it wait for the next poll.
MAX_ENTRIES = 20

# GraphQL's Int is a signed 32-bit integer. A larger literal makes GitHub reject the WHOLE document,
# so one absurd number would blind the poll for every other entry.
MAX_PR_NUMBER = 2147483647

# Matched with fullmatch, never `$` (see the module docstring).
_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]{1,200}")

_STATES = ("OPEN", "MERGED", "CLOSED")
_PR_FIELDS = "number state title mergedAt"


def _valid_number(value):
    """A real positive int that GraphQL can represent. `bool` is an int subclass and `True` would
    format as `1`, so it is refused explicitly; a numeric string is refused too -- the caller owns
    producing an int, and this is the one gate between a watch entry and the query text."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= MAX_PR_NUMBER
    )


def _split_repo(repo):
    """`(owner, name)` for a clean `owner/name` string, else None: exactly one slash, both halves
    inside the allow-list."""
    if not isinstance(repo, str) or repo.count("/") != 1:
        return None
    owner, name = repo.split("/")
    if _NAME_RE.fullmatch(owner) and _NAME_RE.fullmatch(name):
        return owner, name
    return None


def _resolve(entry):
    """`(owner, name, number, branch, key)` for a watch entry that is safe to poll, else None.

    The ONE place an entry is judged, so `entry_key` and `build_query` cannot disagree about which
    entries exist. A number, when present, decides everything: it must be valid or the entry is
    dropped whole (a corrupt number does not quietly fall back to the branch). Only without a
    number does the branch count.
    """
    if not isinstance(entry, dict):
        return None
    parts = _split_repo(entry.get("repo"))
    if parts is None:
        return None
    owner, name = parts
    number = entry.get("number")
    if number is not None:
        if not _valid_number(number):
            return None
        return owner, name, number, None, "%s/%s#%d" % (owner, name, number)
    branch = entry.get("branch")
    if isinstance(branch, str) and _BRANCH_RE.fullmatch(branch):
        return owner, name, None, branch, "%s/%s@%s" % (owner, name, branch)
    return None


def entry_key(entry):
    """The result key for a watch entry -- `owner/name#650` for a PR number, `owner/name@branch`
    for a branch -- or None when the entry cannot be polled safely (not a dict, a repo that is not
    a clean `owner/name`, a number that is not a positive int, no usable branch). A key exists
    exactly when `build_query` would include the entry, so ledger.py can use this to key (and to
    refuse) watch entries and be sure a poll result will find them."""
    resolved = _resolve(entry)
    return resolved[4] if resolved else None


def build_query(entries):
    """`(query, keymap)` for the pollable entries, or `(None, {})` when there are none.

    `entries` are watch dicts. Entries that fail `_resolve` are skipped, a repeated key collapses
    to one field, and at most MAX_ENTRIES are asked about (input order; the rest wait for the next
    poll). The document is deterministic: repositories alias `r0`, `r1`, ... in first-seen order,
    a PR number is `p<number>`, a branch is `b<index within its repository>`, so the same watch
    always produces the same text. The keymap is `{repo alias: {field alias: entry key}}`.
    """
    if not isinstance(entries, (list, tuple)):
        return None, {}
    groups = []  # one dict per repository, in first-seen order
    by_repo = {}
    keymap = {}
    seen = set()
    for entry in entries:
        if len(seen) >= MAX_ENTRIES:
            break
        resolved = _resolve(entry)
        if resolved is None or resolved[4] in seen:
            continue
        owner, name, number, branch, key = resolved
        seen.add(key)
        group = by_repo.get((owner, name))
        if group is None:
            group = {
                "alias": "r%d" % len(groups),
                "owner": owner,
                "name": name,
                "lines": [],
                "branches": 0,
            }
            by_repo[(owner, name)] = group
            groups.append(group)
        if number is not None:
            field = "p%d" % number
            group["lines"].append(
                "    %s: pullRequest(number: %d) { %s }" % (field, number, _PR_FIELDS)
            )
        else:
            field = "b%d" % group["branches"]
            group["branches"] += 1
            group["lines"].append(
                "    %s: pullRequests(headRefName: %s, last: 1, states: [OPEN, MERGED, CLOSED]) "
                "{ nodes { %s } }" % (field, json.dumps(branch), _PR_FIELDS)
            )
        keymap.setdefault(group["alias"], {})[field] = key
    if not groups:
        return None, {}
    lines = ["query {"]
    for group in groups:
        lines.append(
            "  %s: repository(owner: %s, name: %s) {"
            % (group["alias"], json.dumps(group["owner"]), json.dumps(group["name"]))
        )
        lines.extend(group["lines"])
        lines.append("  }")
    lines.append("}")
    return "\n".join(lines), keymap


def run_query(query, timeout=DEFAULT_TIMEOUT, runner=None):
    """Run `gh api graphql` once and return the parsed JSON response (a dict), or None.

    `-f query=...` (a string field; `-F` would read `@file` and coerce types) passes the document
    as one argv element, so no shell ever sees it. The environment gets GH_PROMPT_DISABLED and
    GH_NO_UPDATE_NOTIFIER so gh can neither stop to ask a question nor spend the budget on an
    update check, and stdin is /dev/null so it can never wait on the hook's own input. None on a
    missing `gh`, a timeout, any OS-level failure, unparsable output, or a non-zero exit that
    brought no `data` object -- but a non-zero exit WITH `data` is a partial answer (see the module
    docstring) and is returned. `runner` is a `subprocess.run`-shaped callable for tests.
    """
    if not isinstance(query, str) or not query:
        return None
    run = runner if runner is not None else subprocess.run
    env = dict(os.environ)
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    try:
        proc = run(
            ["gh", "api", "graphql", "-f", "query=" + query],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        parsed = json.loads(proc.stdout)
        returncode = proc.returncode
    except Exception:
        return None  # no gh, timed out, unreadable output -- all fail open
    if not isinstance(parsed, dict):
        return None
    if returncode != 0 and not isinstance(parsed.get("data"), dict):
        return None  # a real failure (auth, network, HTTP error): nothing worth keeping
    return parsed


def _data_object(response):
    """The GraphQL `data` object of a parsed response; a bare `data` object is accepted as well, so
    a caller holding either shape gets the same answer."""
    if not isinstance(response, dict):
        return {}
    inner = response.get("data")
    return inner if isinstance(inner, dict) else response


def _pull_request(raw):
    """One field's answer -> `{"state", "number", "title", "mergedAt"}`, or None when nothing is
    usable. `mergedAt` is GitHub's ISO timestamp, or None for a PR that has not merged: the ledger
    stamps a merge the session made itself with it (see ledger.apply_poll).

    A number field answers with the PR node itself; a branch field answers `{nodes: [...]}` and the
    last node is taken (the query asks for `last: 1`, the newest). The title is untrusted text that
    ends up in a nudge, so it is collapsed to a single line.
    """
    if isinstance(raw, dict) and "nodes" in raw:
        nodes = raw.get("nodes")
        raw = None
        if isinstance(nodes, list):
            for node in reversed(nodes):
                if isinstance(node, dict):
                    raw = node
                    break
    if not isinstance(raw, dict):
        return None
    state = raw.get("state")
    number = raw.get("number")
    if state not in _STATES or not _valid_number(number):
        return None
    title = raw.get("title")
    merged_at = raw.get("mergedAt")
    return {
        "state": state,
        "number": number,
        "title": " ".join(title.split()) if isinstance(title, str) else "",
        "mergedAt": merged_at if isinstance(merged_at, str) and merged_at else None,
    }


def parse_results(data, keymap):
    """`{entry key: {"state": "OPEN"|"MERGED"|"CLOSED", "number", "title", "mergedAt"}}` from a response and the
    keymap `build_query` returned. Anything GitHub could not answer is simply absent: a null
    repository (an errored or invisible alias), a null `pullRequest`, a branch with no nodes, an
    unknown state. Never raises."""
    results = {}
    if not isinstance(keymap, dict):
        return results
    inner = _data_object(data)
    for repo_alias, fields in keymap.items():
        repository = inner.get(repo_alias)
        if not isinstance(repository, dict) or not isinstance(fields, dict):
            continue
        for field_alias, key in fields.items():
            found = _pull_request(repository.get(field_alias))
            if found is not None:
                results[key] = found
    return results


def poll(entries, timeout=DEFAULT_TIMEOUT, runner=None):
    """Build the query, run it, parse the answer: `{key: {"state", "number", "title"}}` for every
    entry GitHub answered, `{}` when nothing was pollable or anything at all went wrong. Callers
    treat `{}` as "learned nothing this time" and ask again at the next checkpoint."""
    try:
        query, keymap = build_query(entries)
        if query is None:
            return {}
        response = run_query(query, timeout=timeout, runner=runner)
        if response is None:
            return {}
        return parse_results(response, keymap)
    except Exception:
        return {}
