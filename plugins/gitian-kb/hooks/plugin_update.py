#!/usr/bin/env python3
"""plugin_update.py -- the "your gitian-kb plugin is out of date" nudge, shared by two hooks.

A plugin cannot learn mid-session that it is stale: Claude Code only auto-updates a marketplace
that has auto-update switched on, and a third-party marketplace (gitian-tools is one) ships with
it OFF. So the server advertises the current version on every successful tool envelope
(`plugin_latest`, packages/kb/src/plugin-version.ts) and this module does the comparing:

  - harvest.py (PostToolUse) records the advertised version as servers.<key>.pluginLatest and, when
    it is newer than the installed manifest and the call was the PRIMARY's, emits the nudge;
  - session_digest.py (SessionStart) emits the same nudge from the CACHED value, so the next
    session hears it before its first KB call, with no network involved.

Both go through `claim_nudge`, which bounds the nudge to once per NUDGE_INTERVAL_HOURS PER
MACHINE for a given advertised version -- not once per session. The message is for the USER, and
an owner running several sessions at once (or clearing one repeatedly: `/clear` wipes a session's
flags, which is why a session flag re-fired on every clear) should hear "update your plugin" once
a day, not once per agent. A NEWER advertised version nudges again immediately.

harvest.py stays silent inside a SUBAGENT (the hook payload carries `agent_id` there): a
subagent's MCP traffic is recorded under the parent's session id, and a nudge injected into
kb-librarian's context reaches nobody -- it cannot address the user -- while still spending the
day's allowance. It caches the version regardless, and the next SessionStart delivers it.

The second signal is auto-update itself: `autoupdate_hint` reads Claude Code's
~/.claude/plugins/known_marketplaces.json and, when the gitian-tools entry is present and its
`autoUpdate` is not true, says so -- at most once every AUTOUPDATE_HINT_DAYS, because that file is
an undocumented Claude Code internal and a format change must degrade to "one stray line a week",
never "a wrong line every session". A missing file, a missing entry (a --plugin-dir or local
install), or any parse failure is silence.

Comparison is strict semver `MAJOR.MINOR.PATCH`, and the nudge fires ONLY when installed < latest.
A client AHEAD of the server is normal and silent: the mirror syncs on merge while prod deploys on a
release tag, so prod routinely advertises an older version than the one installed. Anything that
does not parse as three integers is silence too.

Fail-open everywhere: every public function returns None/False rather than raising.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone

import state as state_mod

MARKETPLACE = "gitian-tools"
NUDGE_INTERVAL_HOURS = 24
AUTOUPDATE_HINT_DAYS = 7
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse_version(value):
    """"0.22.0" -> (0, 22, 0); anything else (pre-release tags included) -> None."""
    if not isinstance(value, str):
        return None
    m = VERSION_RE.match(value.strip())
    if not m:
        return None
    return tuple(int(part) for part in m.groups())


def installed_version():
    """The version in THIS plugin's own manifest, resolved from this file's location (hooks/ sits
    beside .claude-plugin/) so it is right under the plugin cache, --plugin-dir and the repo alike."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        manifest = os.path.join(here, os.pardir, ".claude-plugin", "plugin.json")
        with open(manifest, "r", encoding="utf-8") as fh:
            version = json.load(fh).get("version")
        return version if parse_version(version) else None
    except Exception:
        return None


def is_outdated(installed, latest):
    a, b = parse_version(installed), parse_version(latest)
    return a is not None and b is not None and a < b


def latest_from_blocks(decoded_blocks):
    """The `plugin_latest` string off the first decoded tool-response block that carries a valid
    one. `decoded_blocks` is harvest.py's _decoded_blocks() iterable."""
    try:
        for parsed in decoded_blocks:
            if isinstance(parsed, dict) and parse_version(parsed.get("plugin_latest")):
                return parsed["plugin_latest"].strip()
    except Exception:
        pass
    return None


def update_message(installed, latest):
    return (
        "gitian-kb plugin %s is installed and the server reports %s is current. Tell the user -- "
        "do not update their install yourself: run `/plugin marketplace update %s`, then "
        "`/reload-plugins` (a fresh session picks it up too). To get updates automatically, "
        "`/plugin` -> Marketplaces -> %s -> Enable auto-update; third-party marketplaces ship with "
        "it off. Advisory, at most once a day."
    ) % (installed, latest, MARKETPLACE, MARKETPLACE)


def claim_nudge(latest):
    """True iff THIS call may nudge for `latest`: nothing was nudged for this same version within
    NUDGE_INTERVAL_HOURS on this machine. Top-level state (`pluginNudge`), one locked
    read-modify-write, so concurrent sessions yield exactly one nudge."""
    path = state_mod.state_path()
    now = datetime.now(timezone.utc)

    def mutate():
        state = state_mod.load(path)
        last = state.get("pluginNudge")
        last = last if isinstance(last, dict) else {}
        at = _parse_iso(last.get("at"))
        if (
            last.get("version") == latest
            and at is not None
            and now - at < timedelta(hours=NUDGE_INTERVAL_HOURS)
        ):
            return False
        state["pluginNudge"] = {"version": latest, "at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
        state_mod.save(path, state_mod.finalize(state))
        return True

    try:
        return bool(state_mod.with_lock(path, mutate))
    except Exception:
        return False


def nudge_for(latest):
    """The update message iff `latest` is newer than the installed manifest AND the daily
    allowance for that version is unspent; else None. Checks the version BEFORE claiming, so a
    current install never spends anything."""
    installed = installed_version()
    if not is_outdated(installed, latest):
        return None
    if not claim_nudge(latest):
        return None
    return update_message(installed, latest)


def _marketplaces_file():
    override = os.environ.get("GITIAN_KB_MARKETPLACES_FILE")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".claude", "plugins", "known_marketplaces.json")


def autoupdate_is_off():
    """True ONLY when the gitian-tools marketplace entry exists and its autoUpdate is not true."""
    try:
        with open(_marketplaces_file(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        entry = data.get(MARKETPLACE) if isinstance(data, dict) else None
        if not isinstance(entry, dict):
            return False
        return entry.get("autoUpdate") is not True
    except Exception:
        return False


def _parse_iso(value):
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def autoupdate_hint():
    """One line when auto-update is off and no hint went out in the last AUTOUPDATE_HINT_DAYS;
    else None. The timestamp is top-level state (`autoUpdateHintAt`), not per-session: the point
    is to bound the line across sessions."""
    if not autoupdate_is_off():
        return None
    path = state_mod.state_path()
    now = datetime.now(timezone.utc)

    def mutate():
        state = state_mod.load(path)
        last = _parse_iso(state.get("autoUpdateHintAt"))
        if last is not None and now - last < timedelta(days=AUTOUPDATE_HINT_DAYS):
            return False
        state["autoUpdateHintAt"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        state_mod.save(path, state_mod.finalize(state))
        return True

    try:
        if not state_mod.with_lock(path, mutate):
            return None
    except Exception:
        return None
    return (
        "gitian-kb: auto-update is OFF for the %s marketplace, so plugin fixes reach this install "
        "only by hand. Mention it to the user once: `/plugin` -> Marketplaces -> %s -> Enable "
        "auto-update."
    ) % (MARKETPLACE, MARKETPLACE)
