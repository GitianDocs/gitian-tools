#!/usr/bin/env python3
"""bash_actions.py -- what a Bash command DID, read from its text, for the gitian-kb plugin.

The Bash PostToolUse hook (bash_activity.py) turns a command that succeeded into ledger activity:
a commit, a pull request opened or armed to merge, a release published, a file edited by a command
instead of by the Edit tool. All it has is the command string, so this module answers one
question -- which of those did the command EXECUTE? -- and never lets that be confused with what
the command merely MENTIONS (`echo "git commit"`, a `--body` that says "run git commit", a heredoc
documenting a workflow). Pure parsing: no subprocess, no filesystem, no state. `parse` never
raises; a command it cannot read yields [].

`parse(command, cwd)` returns the actions in command order, each a dict with a "kind":

  commit     {dir}
  pr_create  {dir, repo, title, head}           gh pr create (--repo/-R, --title/-t, --head/-H)
  pr_merge   {dir, repo, number, branch, auto}  gh pr merge, armed or not (the poll decides)
  release    {dir, repo, tag, draft, edit}      gh release create / gh release edit --draft=false
  write      {dir, paths}                       a file edited by sed -i, perl -i, tee, `>`, `>>`,
                                                patch or git apply (absolute paths)

`dir` is where the command ran: `cwd`, moved by `cd`/`pushd`/`popd` (undone when a subshell or a
substitution ends) and, for one git segment, by `git -C`. A value the text cannot settle (a `$VAR`,
a `$(...)`, `~user`) leaves the directory where it was and drops a write path rather than guess
one -- except the operand of `sed -i` / `perl -i`, which edits something that exists in the tree it
runs in and so counts as the directory itself.

Why a lexer and not a regex split followed by `shlex.split` per segment, which is what
commit_nudge.py did: splitting on `&&`, `;` and newlines BEFORE reading the quotes cuts a quoted
string in half. That hid every commit made as `git commit -m "$(cat <<'EOF' ... EOF)"` -- the form a
multi-line message nearly always takes, whose first half has an unbalanced quote and so tokenised
to nothing -- and invented commits out of `echo "a; git commit; b"`. `_Lexer` reads the quotes
first, drops heredoc bodies, looks inside `$(...)`, backticks and `( ... )` for the commands they
run, and keeps redirection operators out of the words, so a quoted `>` is an argument and an
unquoted one is a write. As with an unparsable shlex segment, a command with an unterminated quote
contributes nothing from that quote on.

Deliberately NOT understood: wrappers (`sudo`, `env`, `bash -c "..."`), shell variables other than
`$HOME`, loops that expand a path at run time. An action missed is a nudge missed; an action
invented is a nudge for work that never happened, so every doubtful case resolves to "not seen".

The response helpers (`response_text`, `commit_failed`, `release_failed`, `find_pr_url`,
`find_release_url`) read what the command PRINTED: a failed command, or one whose output names the
object it made, is told apart from a successful one without a second look at the repository.

Tests: plugins/gitian-kb/hooks/tests/test_bash_actions.py (direct unit tests; nothing here does I/O).
"""

import json
import os
import posixpath
import re
from urllib.parse import unquote

# A command longer than this is a pasted file, not a command worth lexing inside a hook's budget.
MAX_COMMAND_CHARS = 1000000
# Subshells and substitutions nested deeper than this are not a real command: parse gives up.
MAX_NESTING = 16

# Case-insensitive: real git output capitalizes some of these ("Aborting commit due to an empty
# commit message"), and matching case-insensitively only ever suppresses more false actions.
COMMIT_FAILURE_MARKERS = ("nothing to commit", "error:", "fatal:", "aborting commit")
RELEASE_FAILURE_MARKERS = (
    "error:",
    "fatal:",
    "http 4",
    "http 5",
    "graphql:",
    "already exists",
    "not found",
)

# Leading `VAR=value` words before the real command word, e.g. `FOO=bar git commit`.
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Shell words that only introduce a command (`then git commit`, `! git diff --quiet`): looked
# through, so a commit inside an if/for/while still counts.
_LEADING_WORDS = frozenset(("{", "!", "if", "then", "elif", "else", "do", "while", "until", "time"))
# Commands whose `>` is a comparison, not a redirection.
_TEST_COMMANDS = frozenset(("[[", "[", "test"))

# git global options that consume the FOLLOWING token (`--opt=value` forms are one token).
_GIT_OPTS_WITH_ARG = frozenset(
    ("-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix",
     "--attr-source")
)
_APPLY_READ_ONLY = frozenset(("--check", "--stat", "--numstat", "--summary"))
_PATCH_READ_ONLY = frozenset(("--dry-run", "--check", "-C"))
# `git commit` flags that make no commit: recording one would name HEAD's existing commit as new.
_COMMIT_NO_OP = frozenset(("--dry-run", "-h", "--help"))

# Redirection operators, longest first. Only a bare `>` / `>>` / `>|` (no fd digit in front) is a
# write: `>&2`, `2>/dev/null`, `&>log` and `2>&1` are plumbing around a command, not its work.
_REDIRECT_OPS = ("&>>", "&>", ">>", ">&", ">|", ">", "<<<", "<<-", "<<", "<&", "<>", "<")
_WRITE_REDIRECTS = frozenset((">", ">>", ">|"))

# What a command or process substitution contributes to the word holding it: its output is unknown,
# and the `$` is what keeps that word from being taken for a path or a ref (see _expand, _known).
_SUBST = "$(...)"

_PLAIN_RUN_RE = re.compile(r"""[^ \t\r\n\\'"$`;|&()<>#]+""")
_QUOTED_RUN_RE = re.compile(r'[^"\\$`]+')
_HOME_RE = re.compile(r"\$HOME\b|\$\{HOME\}")
_SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PR_NUMBER_RE = re.compile(r"#?([0-9]{1,9})")
_PR_URL_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([0-9]{1,9})(?![0-9])",
    re.IGNORECASE,
)
_RELEASE_URL_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/releases/tag/([^\s\"'<>)\]]+)",
    re.IGNORECASE,
)

_OPEN = ("open",)
_CLOSE = ("close",)


class _TooDeep(Exception):
    """Raised by the lexer past MAX_NESTING; `parse` turns it into []."""


class _Segment(object):
    """One simple command under construction: its words, its redirections, and the word being
    built (`cur`, None between words). `target` is a redirection operator waiting for its word."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.words = []
        self.redirs = []
        self.cur = None
        self.quoted = False
        self.target = None
        self.broken = False

    def add(self, text, quoted=False):
        if self.cur is None:
            self.cur = []
        self.cur.append(text)
        if quoted:
            self.quoted = True


class _Lexer(object):
    """A small shell lexer: `lex()` returns the commands in the order they run, as a list of
    ("cmd", words, redirs) items plus _OPEN/_CLOSE markers around every subshell and substitution
    (so the caller can scope `cd` to it). `words` are unquoted strings; `redirs` are
    (fd or None, operator, target word) triples, and heredoc delimiters never reach either list.

    It reads only what decides whether a word is a command: quotes (single, double, `$'...'`),
    backslash escapes and line continuations, `#` comments, the separators `&&` `||` `|` `|&` `;`
    `&` and newline, parentheses, `$(...)` / backticks / `<(...)` (their commands come out before
    the command that holds them, the word they sit in gets _SUBST), `$((...))` arithmetic (opaque),
    redirections, and heredocs (the body is skipped at the next newline)."""

    def __init__(self, text):
        self.s = text
        self.n = len(text)
        self.i = 0
        self.items = []

    def lex(self):
        self.run(None, 0)
        return self.items

    def run(self, closing, depth):
        """Lex commands until the `closing` character (consumed) or the end of the text."""
        if depth > MAX_NESTING:
            raise _TooDeep()
        s, n = self.s, self.n
        seg = _Segment()
        heredocs = []  # (delimiter, strip leading tabs) waiting for the next newline

        def end_word():
            if seg.cur is None:
                return
            text = "".join(seg.cur)
            seg.cur = None
            seg.quoted = False
            if seg.target is None:
                seg.words.append(text)
                return
            fd, op = seg.target
            seg.target = None
            if op in ("<<", "<<-"):
                heredocs.append((text, op == "<<-"))
            else:
                seg.redirs.append((fd, op, text))

        def end_segment():
            end_word()
            if not seg.broken and (seg.words or seg.redirs):
                self.items.append(("cmd", seg.words, seg.redirs))
            seg.reset()

        while self.i < n:
            i = self.i
            m = _PLAIN_RUN_RE.match(s, i)
            if m:
                seg.add(m.group())
                self.i = m.end()
                continue
            c = s[i]
            if c in " \t\r":
                end_word()
                self.i = i + 1
            elif c == "\n":
                end_segment()
                self.i = i + 1
                self.skip_heredocs(heredocs)
            elif c == "\\":
                nxt = s[i + 1 : i + 2]
                if nxt == "\n":
                    self.i = i + 2  # a line continuation: the shell deletes both characters
                elif nxt:
                    seg.add(nxt, True)
                    self.i = i + 2
                else:
                    seg.add("\\")
                    self.i = i + 1
            elif c == "'":
                j = s.find("'", i + 1)
                if j < 0:
                    seg.broken = True
                    self.i = n
                else:
                    seg.add(s[i + 1 : j], True)
                    self.i = j + 1
            elif c == '"':
                self.double_quoted(seg, depth)
            elif c == "$":
                self.dollar(seg, depth)
            elif c == "`":
                self.i = i + 1
                if closing == "`":
                    end_segment()
                    return
                self.substitution(seg, "`", depth)
            elif c == "#" and seg.cur is None and not _is_digit(s[i + 1 : i + 2]):
                # A comment runs to the end of the line. `#123` is kept as a word: it is how a
                # pull request is named, and the shell would not have been asked to run it anyway.
                j = s.find("\n", i)
                self.i = n if j < 0 else j
            elif c == ";":
                end_segment()
                self.i = i + 1
            elif c == "|":
                end_segment()
                self.i = i + (2 if s[i + 1 : i + 2] in ("|", "&") else 1)
            elif c == "&":
                nxt = s[i + 1 : i + 2]
                if nxt == ">":
                    self.redirect(seg, end_word)  # &> and &>> send both streams to a file
                else:
                    end_segment()  # `&&`, or `&` putting the command in the background
                    self.i = i + (2 if nxt == "&" else 1)
            elif c == "(":
                end_segment()
                if s.startswith("((", i):
                    self.i = self.after_parens(i)  # (( arithmetic )): no commands inside
                else:
                    self.i = i + 1
                    self.group(")", depth)
            elif c == ")":
                end_segment()
                self.i = i + 1
                if closing == ")":
                    return
            elif c in "<>":
                if s[i + 1 : i + 2] == "(":  # process substitution: <(cmd) / >(cmd)
                    self.i = i + 2
                    self.substitution(seg, ")", depth)
                else:
                    self.redirect(seg, end_word)
            else:  # a lone `#` inside a word, or any character the plain-run pattern leaves out
                seg.add(c)
                self.i = i + 1
        end_segment()

    def group(self, closing, depth):
        """A subshell or substitution body: its commands go into `items` between the markers."""
        self.items.append(_OPEN)
        self.run(closing, depth + 1)
        self.items.append(_CLOSE)

    def substitution(self, seg, closing, depth):
        self.group(closing, depth)
        seg.add(_SUBST)

    def after_parens(self, start):
        """Index just past the `)` matching the `(` at `start` (the end of the text if none)."""
        level = 0
        for j in range(start, self.n):
            ch = self.s[j]
            if ch == "(":
                level += 1
            elif ch == ")":
                level -= 1
                if level == 0:
                    return j + 1
        return self.n

    def dollar(self, seg, depth):
        s, i = self.s, self.i
        nxt = s[i + 1 : i + 2]
        if s.startswith("$((", i):
            seg.add(_SUBST)
            self.i = self.after_parens(i + 1)
        elif nxt == "(":
            self.i = i + 2
            self.substitution(seg, ")", depth)
        elif nxt == "{":
            j = s.find("}", i + 2)
            if j < 0:
                seg.add("$")
                self.i = i + 1
            else:
                seg.add(s[i : j + 1])
                self.i = j + 1
        elif nxt == "'":  # $'...': ANSI-C quoting, where \' does not end the string
            j = i + 2
            while j < self.n and s[j] != "'":
                j += 2 if s[j] == "\\" else 1
            if j >= self.n:
                seg.broken = True
                self.i = self.n
            else:
                seg.add(s[i + 2 : j], True)
                self.i = j + 1
        else:
            seg.add("$")
            self.i = i + 1

    def double_quoted(self, seg, depth):
        """A "..." string: literal except backslash escapes and the substitutions inside it."""
        s, n = self.s, self.n
        i = self.i + 1
        seg.add("", True)  # `""` is a word of its own, empty
        while i < n:
            m = _QUOTED_RUN_RE.match(s, i)
            if m:
                seg.add(m.group())
                i = m.end()
                continue
            c = s[i]
            if c == '"':
                self.i = i + 1
                return
            if c == "\\":
                nxt = s[i + 1 : i + 2]
                if nxt == "\n":
                    i += 2
                elif nxt in ('"', "\\", "$", "`"):
                    seg.add(nxt)
                    i += 2
                else:
                    seg.add("\\")
                    i += 1
            elif c == "$" and s.startswith("$((", i):
                seg.add(_SUBST)
                i = self.after_parens(i + 1)
            elif c == "$" and s[i + 1 : i + 2] == "(":
                self.i = i + 2
                self.substitution(seg, ")", depth)
                i = self.i
            elif c == "`":
                self.i = i + 1
                self.substitution(seg, "`", depth)
                i = self.i
            else:
                seg.add(c)
                i += 1
        seg.broken = True
        self.i = n

    def redirect(self, seg, end_word):
        """Read a redirection operator at `self.i`; its target is the next word."""
        s, i = self.s, self.i
        fd = None
        word = "".join(seg.cur) if seg.cur is not None else ""
        if word.isascii() and word.isdigit() and not seg.quoted:
            fd = word  # `2>file`: the digits touch the operator, so they name a file descriptor
            seg.cur = None
        else:
            end_word()
        op = "<"
        for candidate in _REDIRECT_OPS:
            if s.startswith(candidate, i):
                op = candidate
                break
        seg.target = (fd, op)
        self.i = i + len(op)

    def skip_heredocs(self, heredocs):
        """At a newline: drop the body of every heredoc opened on the line just ended."""
        s, n = self.s, self.n
        for delimiter, strip_tabs in heredocs:
            while self.i < n:
                j = s.find("\n", self.i)
                line = s[self.i :] if j < 0 else s[self.i : j]
                self.i = n if j < 0 else j + 1
                line = line.rstrip("\r")
                if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                    break
        del heredocs[:]


def _is_digit(ch):
    return len(ch) == 1 and "0" <= ch <= "9"


def _expand(raw):
    """`raw` with a leading `~` and any `$HOME` / `${HOME}` expanded, or None while it still
    depends on something the text cannot tell us (another variable, a substitution, `~user`)."""
    home = os.environ.get("HOME") or ""
    path = raw
    if path == "~" or path.startswith("~/"):
        if not home:
            return None
        path = home + path[1:]
    elif path.startswith("~"):
        return None
    if home and "$" in path:
        path = _HOME_RE.sub(lambda match: home, path)
    if "$" in path or "`" in path:
        return None
    return path


def _abs_path(base, raw):
    """`raw` resolved against `base` (lexically: `..` is folded, symlinks are not looked at)."""
    expanded = _expand(raw)
    if not expanded:
        return None
    return posixpath.normpath(posixpath.join(base, expanded))


def _or_here(raw):
    """An in-place editor's operand, or "." (the effective directory) when the text cannot say
    which file it is -- `sed -i ... $(git ls-files)`, `"$f"` in a loop. Such a command edits
    something that exists, in the tree it runs in, so the directory is a fair place to count the
    edit; a redirect or `tee` target (which may just as well be a temp file) is dropped instead."""
    return raw if _expand(raw) else "."


def _resolve_dir(base, raw):
    """The directory `raw` names from `base`; `base` itself when `raw` is empty or unknowable."""
    return _abs_path(base, raw) or base


def _known(text):
    """`text` unless it is empty or still holds an unexpanded `$...` / backtick."""
    if isinstance(text, str) and text and "$" not in text and "`" not in text:
        return text
    return None


def _slug(value):
    """`owner/name` from a `--repo` value (`[HOST/]OWNER/REPO`), else None."""
    if not isinstance(value, str):
        return None
    parts = value.strip().strip("/").split("/")
    if len(parts) == 3:
        parts = parts[1:]
    candidate = "/".join(parts)
    return candidate if _SLUG_RE.fullmatch(candidate) else None


def _flag_value(raw):
    """True / False for a boolean flag's spelling (`true`, `0`, ...), None when absent or odd."""
    if not isinstance(raw, str):
        return None
    lowered = raw.lower()
    if lowered in ("true", "1", "t"):
        return True
    if lowered in ("false", "0", "f"):
        return False
    return None


def _values(wanted, skipped=()):
    """Spelling -> key table for the flags of ONE gh subcommand that take a value: those this
    module reads (`wanted`: key -> spellings) and those it only has to step over (`skipped`,
    key ""), so a flag's value is never taken for a positional or for another flag."""
    table = dict.fromkeys(skipped, "")
    for key, spellings in wanted.items():
        for spelling in spellings:
            table[spelling] = key
    return table


_PR_CREATE_VALUES = _values(
    {"repo": ("-R", "--repo"), "title": ("-t", "--title"), "head": ("-H", "--head")},
    (
        "-a", "--assignee", "-B", "--base", "-b", "--body", "-F", "--body-file", "-l", "--label",
        "-m", "--milestone", "-p", "--project", "-r", "--reviewer", "-T", "--template", "--recover",
    ),
)
_PR_MERGE_VALUES = _values(
    {"repo": ("-R", "--repo")},
    ("-b", "--body", "-F", "--body-file", "-t", "--subject", "-A", "--author-email",
     "--match-head-commit"),
)
_PR_MERGE_BOOLS = {"--auto": "auto"}
_RELEASE_VALUES = _values(
    {"repo": ("-R", "--repo")},
    ("-n", "--notes", "-F", "--notes-file", "-t", "--title", "--target", "--notes-start-tag",
     "--discussion-category", "--tag"),
)
_RELEASE_BOOLS = {"-d": "draft", "--draft": "draft"}


def _flags(args, values, bools):
    """Split a gh subcommand's arguments into ({key: value}, [positionals]). `values` and `bools`
    map flag spellings to keys: `-R x`, `-Rx`, `-R=x`, `--repo x`, `--repo=x` all read the same, a
    bool flag reads "true" (or its `=value`), clustered short flags (`-dp`) are taken one by one,
    and any flag in neither table is a bare switch that is simply stepped over."""
    opts = {}
    positionals = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            positionals.extend(args[i:])
            break
        if a.startswith("--"):
            name, eq, value = a.partition("=")
            if name in values:
                key = values[name]
                if eq:
                    given = value
                elif i < len(args):
                    given = args[i]
                    i += 1
                else:
                    given = None
                if key and given is not None:
                    opts[key] = given
            elif name in bools:
                opts[bools[name]] = value if eq else "true"
        elif a.startswith("-") and len(a) > 1:
            for k in range(1, len(a)):
                flag = "-" + a[k]
                rest = a[k + 1 :]
                if flag in values:
                    key = values[flag]
                    if rest:
                        given = rest[1:] if rest.startswith("=") else rest
                    elif i < len(args):
                        given = args[i]
                        i += 1
                    else:
                        given = None
                    if key and given is not None:
                        opts[key] = given
                    break
                if flag in bools:
                    opts[bools[flag]] = "true"
        else:
            positionals.append(a)
    return opts, positionals


def _head(words):
    """(index of the command word, its leading VAR=value assignments). The index is None when the
    segment is nothing but assignments and shell keywords."""
    env = {}
    for index, word in enumerate(words):
        if _ENV_ASSIGN_RE.match(word):
            key, _, value = word.partition("=")
            env[key] = value
        elif word not in _LEADING_WORDS:
            return index, env
    return None, env


def _git(args, cur):
    """(actions, write paths) for `git ARGS` run from `cur`. Global options are stepped over to
    reach the subcommand: `-C X` moves the directory for THIS command only (several stack, as in
    git), the options in _GIT_OPTS_WITH_ARG eat the next token, any other `-...` is a switch. A
    plain word that is not `commit` (`git log --grep commit`) means `commit` is just an argument."""
    where = cur
    j = 0
    while j < len(args) and args[j].startswith("-"):
        if args[j] == "-C":
            if j + 1 < len(args):
                where = _resolve_dir(where, args[j + 1])
            j += 2
        elif args[j] in _GIT_OPTS_WITH_ARG:
            j += 2
        else:
            j += 1
    if j >= len(args):
        return [], []
    sub, rest = args[j], args[j + 1 :]
    if sub == "commit" and not _COMMIT_NO_OP.intersection(rest):
        return [{"kind": "commit", "dir": where}], []
    if sub == "apply" and not _APPLY_READ_ONLY.intersection(rest):
        # The files a patch touches are named inside it, not on the command line: the working
        # tree it is applied to is the only location the text gives.
        return [], [where]
    return [], []


def _gh(args, cur, env_repo):
    """Actions for `gh ARGS`: only `pr create`, `pr merge`, `release create` and `release edit
    --draft=false` count. Anchored on the subcommand pair right after `gh`, so a quoted mention of
    "pr merge" among the arguments of something else never matches."""
    if len(args) < 2:
        return []
    group, verb, rest = args[0], args[1], args[2:]
    if group == "pr" and verb == "create":
        opts, _ = _flags(rest, _PR_CREATE_VALUES, {})
        return [
            {
                "kind": "pr_create",
                "dir": cur,
                "repo": _slug(opts.get("repo")) or _slug(env_repo),
                "title": opts.get("title") or None,
                "head": _known(opts.get("head")),
            }
        ]
    if group == "pr" and verb == "merge":
        opts, positionals = _flags(rest, _PR_MERGE_VALUES, _PR_MERGE_BOOLS)
        repo = _slug(opts.get("repo")) or _slug(env_repo)
        number = branch = None
        if positionals:
            target = positionals[0]
            url = _PR_URL_RE.match(target)
            plain = _PR_NUMBER_RE.fullmatch(target)
            if url:
                repo = url.group(1) + "/" + url.group(2)
                number = int(url.group(3))
            elif plain:
                number = int(plain.group(1))
            else:
                branch = _known(target)
        return [
            {
                "kind": "pr_merge",
                "dir": cur,
                "repo": repo,
                "number": number,
                "branch": branch,
                "auto": _flag_value(opts.get("auto")) is True,
            }
        ]
    if group == "release" and verb in ("create", "edit"):
        opts, positionals = _flags(rest, _RELEASE_VALUES, _RELEASE_BOOLS)
        draft = _flag_value(opts.get("draft"))
        if verb == "edit" and draft is not False:
            return []  # only `--draft=false` (publishing a draft) is a release event
        return [
            {
                "kind": "release",
                "dir": cur,
                "repo": _slug(opts.get("repo")) or _slug(env_repo),
                "tag": _known(positionals[0]) if positionals else None,
                "draft": verb == "create" and draft is True,
                "edit": verb == "edit",
            }
        ]
    return []


def _sed(args):
    """The files an in-place `sed` edits ([] for one that only prints). `-i` / `-i.bak` / `-Ei` /
    `-ni` / `--in-place[=SUF]`: the backup suffix is always attached, so `-ie` is suffix `e`, not
    `-i -e`. Without `-e` / `-f` the first positional is the script, not a file. BSD sed's
    `-i ''` (an empty suffix as a separate word, the macOS spelling) is stepped over too."""
    in_place = scripted = False
    files = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            files.extend(args[i:])
            break
        if a == "-i" and args[i : i + 1] == [""]:
            in_place = True
            i += 1
        elif a.startswith("--"):
            name = a.split("=", 1)[0]
            if name == "--in-place":
                in_place = True
            elif name in ("--expression", "--file", "--line-length"):
                scripted = scripted or name != "--line-length"
                if "=" not in a:
                    i += 1
        elif a.startswith("-") and len(a) > 1:
            for k in range(1, len(a)):
                ch = a[k]
                if ch == "i":
                    in_place = True
                    break
                if ch in "efl":
                    scripted = scripted or ch != "l"
                    if k == len(a) - 1:
                        i += 1
                    break
        else:
            files.append(a)
    if not in_place:
        return []
    return files if scripted else files[1:]


def _perl(args):
    """The files an in-place `perl -i` / `-pi` / `-i.bak -pe` edits ([] for one that does not).
    Switch clusters are read left to right the way perl does: `i` takes the rest of the cluster as
    a backup suffix, `e` / `E` / `I` take it as their argument, `M` `m` `F` `C` ... swallow it."""
    in_place = coded = False
    files = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            files.extend(args[i:])
            break
        if a.startswith("--"):
            continue
        if a.startswith("-") and len(a) > 1:
            for k in range(1, len(a)):
                ch = a[k]
                if ch == "i":
                    in_place = True
                    break
                if ch in "eEI":
                    coded = coded or ch != "I"
                    if k == len(a) - 1:
                        i += 1
                    break
                if ch in "MmFCVdDxX":
                    break
        else:
            files.append(a)
    if not in_place:
        return []
    return files if coded else files[1:]


def _tee(args):
    """The files `tee` writes: every operand that is not a switch (and not the `-` stdout mark)."""
    files = []
    options_done = False
    for a in args:
        if not options_done and a == "--":
            options_done = True
        elif options_done or not a.startswith("-"):
            files.append(a)
    return files


def _change_dir(name, args, cur, pushed):
    """The directory after `cd` / `pushd` / `popd`. `cd -` and anything unknowable leave it as it
    was; a bare `cd` goes HOME; `popd` returns to what the last `pushd` left."""
    if name == "popd":
        return pushed.pop() if pushed else cur
    target = None
    for k, a in enumerate(args):
        if a == "--":
            target = args[k + 1] if k + 1 < len(args) else None
            break
        if a.startswith("-") and a != "-":
            continue
        target = a
        break
    if target is None:
        if name != "cd":
            return cur  # a bare `pushd` swaps the top two directories: not knowable here
        target = "~"
    if name == "pushd":
        pushed.append(cur)
    if target == "-":
        return cur
    return _resolve_dir(cur, target)


def _ephemeral_roots():
    """Where a write is NOT the user's work: scratch space, device files, and Claude Code's own
    state -- `<config>/projects/` (memory, transcripts) and `<config>/plans/`, `<config>` being
    $CLAUDE_CONFIG_DIR or ~/.claude. The same allowlist orientation-check.sh exempts, on purpose:
    <config>/skills, agents and plugins are authored source and stay counted."""
    roots = ["/tmp", "/var/tmp", "/dev", "/proc"]
    config = os.environ.get("CLAUDE_CONFIG_DIR") or (os.environ.get("HOME") or "") + "/.claude"
    if config.startswith("/"):
        config = config.rstrip("/")
        roots.extend((config + "/projects", config + "/plans"))
    return roots


def _under(path, root):
    return path == root or path.startswith(root + "/")


def _write_action(cur, raw_paths):
    """The `write` action for these targets, or None when none survives: unresolvable paths and
    anything under _ephemeral_roots are dropped, repeats are folded."""
    kept = []
    seen = set()
    roots = None
    for raw in raw_paths:
        path = _abs_path(cur, raw)
        if path is None or path in seen:
            continue
        seen.add(path)
        if roots is None:
            roots = _ephemeral_roots()
        if any(_under(path, root) for root in roots):
            continue
        kept.append(path)
    return {"kind": "write", "dir": cur, "paths": kept} if kept else None


def _segment(words, redirs, cur, pushed, actions):
    """Append the actions one simple command performs to `actions`; return the directory the NEXT
    command starts in (only `cd` / `pushd` / `popd` change it)."""
    paths = []
    head, env = _head(words)
    if head is not None:
        name = words[head].rsplit("/", 1)[-1]
        args = words[head + 1 :]
        if name in ("cd", "pushd", "popd"):
            return _change_dir(name, args, cur, pushed)
        if name in _TEST_COMMANDS:
            return cur
        if name == "git":
            found, paths = _git(args, cur)
            actions.extend(found)
        elif name == "gh":
            actions.extend(_gh(args, cur, env.get("GH_REPO")))
        elif name == "sed":
            paths = [_or_here(path) for path in _sed(args)]
        elif name == "perl":
            paths = [_or_here(path) for path in _perl(args)]
        elif name == "tee":
            paths = _tee(args)
        elif name == "patch" and not _PATCH_READ_ONLY.intersection(args):
            paths = [cur]
    for fd, op, target in redirs:
        if fd is None and op in _WRITE_REDIRECTS and target:
            paths.append(target)
    write = _write_action(cur, paths)
    if write:
        actions.append(write)
    return cur


def _parse(command, cwd):
    if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND_CHARS:
        return []
    cur = cwd if isinstance(cwd, str) and cwd else "."
    pushed = []  # the pushd stack
    scopes = []  # (directory, pushd stack) saved when a subshell or substitution opens
    actions = []
    for item in _Lexer(command).lex():
        if item is _OPEN:
            scopes.append((cur, list(pushed)))
        elif item is _CLOSE:
            if scopes:
                cur, pushed = scopes.pop()
        else:
            cur = _segment(item[1], item[2], cur, pushed, actions)
    return actions


def parse(command, cwd=None):
    """The actions `command` performs, in command order (see the module docstring for the dict
    shapes). `cwd` is where the session was when it ran; None or "" means ".". Never raises: a
    command that cannot be read (not a string, absurdly long or deeply nested) yields [], and
    one with an unterminated quote yields what ran before the quote."""
    try:
        return _parse(command, cwd)
    except Exception:
        return []


def response_text(tool_response):
    """One flat string out of a Bash tool_response, whatever shape it arrived in: a string as it
    is; a dict with stdout / stderr as `stdout + "\\n" + stderr` (so a marker is found in either
    stream); any other dict or list JSON-rendered; None as ""."""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict) and ("stdout" in tool_response or "stderr" in tool_response):
        out = tool_response.get("stdout")
        err = tool_response.get("stderr")
        return (out if isinstance(out, str) else "") + "\n" + (err if isinstance(err, str) else "")
    if tool_response is None:
        return ""
    try:
        return json.dumps(tool_response)
    except Exception:
        return ""


def _mentions(text, markers):
    if not isinstance(text, str):
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


def commit_failed(text):
    """True when a commit's output reads as a failure (nothing to commit, an error, an abort)."""
    return _mentions(text, COMMIT_FAILURE_MARKERS)


def release_failed(text):
    """True when `gh release` output reads as a failure (an API error, a duplicate tag, ...)."""
    return _mentions(text, RELEASE_FAILURE_MARKERS)


def find_pr_url(text):
    """(`owner/name`, number) of the first `https://github.com/o/n/pull/N` in `text`, else None --
    what `gh pr create` prints on success."""
    match = _PR_URL_RE.search(text) if isinstance(text, str) else None
    if not match:
        return None
    return match.group(1) + "/" + match.group(2), int(match.group(3))


def find_release_url(text):
    """(`owner/name`, tag) of the first `https://github.com/o/n/releases/tag/T` in `text`, else
    None -- what `gh release create` prints on success (a draft's `untagged-...` URL included)."""
    match = _RELEASE_URL_RE.search(text) if isinstance(text, str) else None
    if not match:
        return None
    tag = unquote(match.group(3)).rstrip(".,;:!?")
    if not tag:
        return None
    return match.group(1) + "/" + match.group(2), tag
