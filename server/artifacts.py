"""Edge harvest of what an attendee actually did (contract C6, tier 3).

The tempting design is to ship agent transcripts to Control Tower and summarise
them there. Transcripts are the wrong input: they are enormous, ANSI-polluted,
and dominated by tool output, so a model reading them produces plausible mush
that is indistinguishable from a hallucination. They are also the most invasive
thing the instance holds.

What this module collects instead is the small, already-structured residue of the
session:

- **Attendee prompts, verbatim.** The highest signal density available anywhere —
  "how do I connect this to our Confluent cluster" is a use case and a stack fact
  in one line.
- **Agent-authored documents.** Promote handoff docs, PRDs, plan files, READMEs.
  Already summarised by the agent that wrote them, and two orders of magnitude
  smaller than the conversation they came from.
- **Plan and todo blocks.** The agent's own articulation of the attendee's intent.
- **Tool-call names and targets.** "created table X", "deployed app Y" — an
  enriched version of the resource census, never the tool's output.
- **Error first-lines, deduped.** A blocker is the most commercially useful thing
  a workshop can surface, and it is the one thing a successful build never shows.

Explicitly excluded: file contents, stdout, scrollback, stack-trace bodies,
dataframe dumps. Those are the bulk of a transcript and the whole of its risk.

Everything is bounded, every read is fail-soft, and every extracted string goes
through the same redaction pass as discovery records — the attendee has a shell,
so anything on disk here may contain something they pasted.
"""

from __future__ import annotations

import glob
import json
import re
import logging
import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field

from .discovery import redact
from .users import User

logger = logging.getLogger("workshop.artifacts")

# Caps. Sized for a human-paced day: a busy attendee produces a few hundred
# prompts, not thousands, and a summariser given more than this is being fed
# noise rather than signal.
MAX_PROMPTS = 200
MAX_PROMPT_CHARS = 600
MAX_PLANS = 30
MAX_PLAN_CHARS = 600
MAX_TOOL_TARGETS = 60
MAX_TARGET_CHARS = 120
MAX_ERRORS = 25
MAX_ERROR_CHARS = 240
MAX_DOCUMENTS = 24
MAX_DOCUMENT_CHARS = 4000
# Counted separately from documents. Sharing one cap would let a chatty commit
# history crowd out the architecture doc, which is the more valuable artifact.
MAX_COMMITS = 40
# Transcript files are read newest-first and abandoned past this budget. A
# runaway agent loop can write tens of megabytes of tool output, and the wrap
# transition is a foreground request an attendee is waiting on.
MAX_TRANSCRIPT_FILES = 40
MAX_TRANSCRIPT_BYTES = 24 * 1024 * 1024
MAX_LINE_BYTES = 512 * 1024

_DOCUMENT_ROOTS = ("promote", "projects", "docs")
_DOCUMENT_NAMES = ("readme", "prd", "plan", "architecture", "security", "design")
_DOCUMENT_SUFFIXES = (".md", ".markdown")
# Depth from the home directory. Deep enough for projects/<repo>/docs/x.md,
# shallow enough that a node_modules checkout cannot turn this into a full walk.
MAX_DOCUMENT_DEPTH = 4
_SKIP_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist",
    "build", "target", ".mypy_cache", ".pytest_cache", "site-packages",
})
# Codex keeps plugin and marketplace clones under ``.tmp``, test fixtures and
# all. They are not session logs.
_TRANSCRIPT_SKIP_DIRS = _SKIP_DIRS | {".tmp"}

# Content blocks that are tool traffic rather than authored text.
_TOOL_BLOCK_TYPES = frozenset({
    "tool_result", "tool_use", "tool-call", "tool_call", "function_call",
    "function_call_output", "image", "thinking", "redacted_thinking",
})
_TEXT_BLOCK_TYPES = frozenset({"text", "input_text", "output_text"})

# Harness-injected pseudo-turns. They arrive on the user role but nobody typed
# them, and a brief built from them would attribute the harness's words to a
# customer.
_SYNTHETIC_PREFIXES = (
    "<", "caveat:", "[request interrupted", "api error", "this session is being",
    "please continue", "system:",
    # Fallbacks for releases that do not flag these structurally: Claude Code
    # loads a skill's body as a user-role turn, and Codex sends AGENTS.md as one.
    "base directory for this skill:", "# agents.md instructions",
    # Workshop Terminal's automatic session openers: passed as the CLI's first
    # message so the agent speaks first, never typed by the attendee.
    "hi! i've just sat down for the workshop.",
    "hi! i'm back at the workshop.",
    "i've just switched to ",
)
# Claude Code's wrapper for a message the attendee sent while the agent was
# working. The words after it are the attendee's.
_QUEUED_WRAPPER = "the user sent a new message while you were working:"
# Claude Code's own flags for user-role entries nobody typed: skill bodies and
# pasted-image notes (``isMeta``), a sub-agent's task from its parent
# (``isSidechain``), and compaction summaries.
_INJECTED_FLAGS = ("isMeta", "isSidechain", "isCompactSummary", "isVisibleInTranscriptOnly")
# Codex sessions another program started rather than the attendee: its own
# sub-agents and guardian reviews, ``codex exec`` and MCP-served runs.
_CODEX_AUTOMATION_SOURCES = frozenset({"exec", "mcp"})
# A bare status line says a command failed, not why. Counted, never a blocker.
_BARE_EXIT = re.compile(r"^(?:exit code|exited with code|exit status)\s*:?\s*-?\d+\.?$", re.IGNORECASE)
# Omnigent 0.19's claude-native bridge, which logs every hook payload of the
# Claude Code sessions it runs as polly's sub-agents.
_OMNIGENT_BRIDGE_GLOB = os.path.join("omnigent-*", "claude-native", "*")
MAX_OMNIGENT_BRIDGES = 256
# The session id is in every record, and the first is written at SessionStart,
# so the head of each ledger is enough. ``state.json`` holds the current one.
MAX_OMNIGENT_LEDGER_BYTES = 64 * 1024
# Context-free continuations. Kept out of what the summariser reads because they
# carry no meaning standalone, but still counted: fifty of them is a real signal
# about how the session went.
_CONTINUATIONS = frozenset({
    "y", "n", "yes", "no", "ok", "okay", "sure", "go", "go ahead", "continue",
    "proceed", "next", "do it", "fix it", "fix that", "try again", "again",
    "thanks", "thank you", "stop", "wait", "yep", "yeah", "nope", "k",
})

# Tool inputs that name a target worth recording. Ordered: the first present key
# wins, so a Bash call reports its command and a Write call reports its path.
_TARGET_KEYS = (
    "file_path", "path", "notebook_path", "command", "table", "table_name",
    "url", "query", "pattern", "name",
)
_PLAN_TOOLS = frozenset({
    "todowrite", "exitplanmode", "update_plan", "plan", "apply_patch_plan",
})


@dataclass(frozen=True)
class Artifact:
    """One agent-authored document, described but not carried.

    ``bytes`` is the only size signal that leaves the instance. It answers "did
    they get far enough to produce a real architecture doc" without shipping the
    document, which is the whole point of harvesting metadata.
    """

    kind: str
    title: str
    bytes: int = 0

    def payload(self) -> dict:
        return {"kind": self.kind, "title": self.title, "bytes": self.bytes}


@dataclass
class Harvest:
    """Everything the summariser is allowed to see, already bounded and redacted."""

    prompts: list[str] = field(default_factory=list)
    plans: list[str] = field(default_factory=list)
    tool_targets: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    # Document excerpts stay local: they feed the on-instance summariser and are
    # never emitted. Only ``artifacts`` (kind/title/bytes) crosses the wire.
    documents: list[tuple[str, str]] = field(default_factory=list)
    # Every attendee turn, including the continuations withheld from ``prompts``.
    prompt_count: int = 0
    # W6 data collection: counts only, never arguments or output. Tool calls by
    # normalised tool name, failed tool results by coarse reason, and every
    # error occurrence (``errors`` keeps the distinct first lines).
    tool_calls: dict[str, int] = field(default_factory=dict)
    tool_errors: dict[str, int] = field(default_factory=dict)
    error_count: int = 0
    redactions: int = 0
    # Assurance metadata only. Paths and exception messages stay local; a final
    # closeout must distinguish an empty session from material it could not read.
    issues: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        """Nothing worth summarising.

        Not the same as "the attendee did nothing": an attendee who only read the
        landing page produces this, and so does an attendee whose agent wrote
        nothing to disk. Either way a summary would be invented rather than
        derived, and inventing one is worse than reporting the gap.
        """
        return not (
            self.prompts or self.plans or self.errors or self.artifacts
            or self.tool_targets
        )

    def artifact_payload(self) -> list[dict]:
        return [artifact.payload() for artifact in self.artifacts]


def _clean(text: str, limit: int) -> tuple[str, int]:
    collapsed = " ".join(str(text).split())[:limit]
    return redact(collapsed)


def _first_line(text: str, limit: int) -> tuple[str, int]:
    """The first non-empty line only — a stack-trace body is never insight."""
    for line in str(text).splitlines():
        if line.strip():
            return _clean(line, limit)
    return "", 0


def _content_blocks(container) -> list[str]:
    """Authored text out of a message body, in either harness's shape.

    Claude Code and Codex disagree on the envelope and both have changed it
    across releases, so this reads structurally — text-ish blocks in, tool traffic
    out — rather than matching a version's exact schema. A parser pinned to one
    shape fails silently on upgrade, and silence here looks identical to an
    attendee who said nothing.
    """
    if isinstance(container, str):
        return [container]
    if not isinstance(container, list):
        return []
    out: list[str] = []
    for block in container:
        if isinstance(block, str):
            out.append(block)
            continue
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "").lower()
        if kind in _TOOL_BLOCK_TYPES:
            continue
        text = block.get("text") or block.get("content")
        if isinstance(text, str) and (kind in _TEXT_BLOCK_TYPES or not kind):
            out.append(text)
    return out


_LEADING_TAG = re.compile(r"^\s*<([a-zA-Z_][\w-]*)\b[^>]*>.*?</\1>\s*", re.DOTALL)


def _authored(text: str) -> str:
    """The attendee's words, after any harness-injected ``<tag>…</tag>`` preamble.

    Codex can deliver an environment or instructions block in the same text as
    the prompt that follows it. Treating the whole text as synthetic because it
    starts with ``<`` silently drops the prompt (test1025 seat 3's summary
    stopped at three prompts while Codex kept working).
    """
    remainder = text
    while True:
        match = _LEADING_TAG.match(remainder)
        if match is None:
            break
        remainder = remainder[match.end():]
    remainder = remainder.strip()
    if remainder.lower().startswith(_QUEUED_WRAPPER):
        remainder = remainder[len(_QUEUED_WRAPPER):].strip()
    return remainder


def _is_synthetic(text: str) -> bool:
    lowered = _authored(text).lower()
    return not lowered or lowered.startswith(_SYNTHETIC_PREFIXES)


def _injected(entry: dict) -> bool:
    """A user-role entry the harness wrote, by the harness's own flags.

    ``origin`` names who sent a Claude Code turn; anything but a human (a
    background task's notification, another session, a coordinator) is not
    the attendee.
    """
    if any(entry.get(flag) for flag in _INJECTED_FLAGS):
        return True
    origin = entry.get("origin")
    kind = str(origin.get("kind") or "").lower() if isinstance(origin, dict) else ""
    return bool(kind) and kind != "human"


@dataclass
class _Session:
    """What one transcript file says about who drove it."""

    # False once the file shows a program, not the attendee, wrote its prompts.
    # Tool calls and errors still count: that work was done for the attendee.
    attendee: bool = True
    # Codex records what was typed as ``user_message`` events. Its model-visible
    # user-role copies also carry AGENTS.md, the environment and skills, so they
    # are only a fallback for a rollout with no such events.
    codex_typed: bool = False
    codex_fallback: list[str] = field(default_factory=list)


def _session_for(path: str, home: str, workers: frozenset[str]) -> _Session:
    """The file-level verdict, from where the file is and Omnigent's ledger."""
    parts = os.path.relpath(path, home).split(os.sep)
    stem = os.path.splitext(os.path.basename(path))[0]
    automated = (
        # Claude Code writes a sub-agent's transcript beside its parent's.
        "subagents" in parts[:-1]
        # Omnigent runs its Codex workers in a private CODEX_HOME.
        or (parts[:1] == [".omnigent"] and "codex-home" in parts)
        or stem in workers
        or os.path.realpath(path) in workers
    )
    return _Session(attendee=not automated)


def _note_driver(entry: dict, session: _Session) -> None:
    """Flip the file to automation when an entry says a program drove it."""
    if str(entry.get("type") or "") == "session_meta":
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        source = payload.get("source")
        originator = str(payload.get("originator") or "").lower()
        if (isinstance(source, dict) and "subagent" in source) or (
            isinstance(source, str) and source.lower() in _CODEX_AUTOMATION_SOURCES
        ) or "omnigent" in originator:
            session.attendee = False
    # ``claude -p``: a headless run a script or another agent started. (The
    # Agent SDK's sdk-py/sdk-ts are kept: Omnigent's polly runs on it, and its
    # turns are the attendee's messages.)
    elif str(entry.get("entrypoint") or "").lower() == "sdk-cli":
        session.attendee = False


def _omnigent_bridge_roots() -> set[str]:
    """Where Omnigent's runner puts its bridge tree: its temp dir, normally /tmp."""
    return {tempfile.gettempdir(), "/tmp"}


def _omnigent_worker_transcripts() -> frozenset[str]:
    """Claude Code sessions Omnigent runs as polly's sub-agents.

    polly hands a worker its task by typing into the worker's terminal, so the
    worker's transcript records polly's instructions as typed prompts and
    nothing in the file says otherwise. Omnigent's bridge does: it logs every
    hook payload of the sessions it runs, each naming the session and its
    transcript (``<tmp>/omnigent-<uid>/claude-native/<bridge>/hooks.jsonl`` and
    ``state.json``). A Claude Code transcript is named after its session id, so
    the id still matches after the worker moves it into a worktree. Read-only,
    bounded, fail-soft: no ledger means no known workers, which is how this
    behaved before. Exclusion only, so a ledger from another attendee on a
    shared container can never add prompts here.
    """
    found: set[str] = set()
    bridges = sorted({path for root in _omnigent_bridge_roots()
                      for path in glob.glob(os.path.join(root, _OMNIGENT_BRIDGE_GLOB))})
    for bridge in bridges[:MAX_OMNIGENT_BRIDGES]:
        try:
            with open(os.path.join(bridge, "state.json"), encoding="utf-8") as handle:
                state = json.load(handle)
            if isinstance(state, dict) and isinstance(state.get("claude_session_id"), str):
                found.add(state["claude_session_id"])
        except (OSError, ValueError):
            pass
        try:
            with open(os.path.join(bridge, "hooks.jsonl"), encoding="utf-8", errors="replace") as handle:
                ledger = handle.read(MAX_OMNIGENT_LEDGER_BYTES)
        except OSError:
            continue
        for line in ledger.splitlines():
            if "session_id" not in line and "transcript_path" not in line:
                continue
            try:
                payload = json.loads(line).get("payload")
            except (ValueError, AttributeError):
                continue
            if not isinstance(payload, dict):
                continue
            if isinstance(payload.get("session_id"), str):
                found.add(payload["session_id"])
            if isinstance(payload.get("transcript_path"), str):
                found.add(os.path.realpath(payload["transcript_path"]))
    found.discard("")
    return frozenset(found)


def _codex_user_message(entry: dict) -> str | None:
    """Codex's own record of what the attendee typed (``event_msg`` / ``user_message``)."""
    payload = entry.get("payload")
    if str(entry.get("type") or "") != "event_msg" or not isinstance(payload, dict):
        return None
    if str(payload.get("type") or "") != "user_message":
        return None
    message = payload.get("message")
    return message if isinstance(message, str) else None


# Coarse, content-free reasons for a failed tool result, checked in order.
_ERROR_REASONS = (
    ("permission_denied", ("permission denied", "forbidden", "403", "not authorized", "unauthorized", "401")),
    ("not_found", ("not found", "no such file", "does not exist", "404")),
    ("timeout", ("timed out", "timeout", "deadline exceeded")),
    ("rate_limited", ("rate limit", "429", "too many requests")),
    ("server_error", ("500", "502", "503", "504", "internal server error", "bad gateway")),
    ("syntax_or_type", ("syntaxerror", "typeerror", "error ts", "type error", "parse error")),
    ("command_failed", ("exit code", "exited with", "command failed", "non-zero")),
)


def error_reason(text: str) -> str:
    lowered = text.lower()
    for reason, needles in _ERROR_REASONS:
        if any(needle in lowered for needle in needles):
            return reason
    return "other"


def _tool_name(raw: str) -> str:
    """A bounded tool label: MCP tools by server, everything else by name."""
    name = raw.strip()
    if name.startswith("mcp__"):
        parts = name.split("__")
        return f"mcp:{parts[1]}" if len(parts) > 1 and parts[1] else "mcp"
    return name[:40] or "unknown"


def _role_of(entry: dict) -> str:
    for container in (entry, entry.get("message"), entry.get("payload")):
        if isinstance(container, dict):
            role = str(container.get("role") or "").lower()
            if role:
                return role
    kind = str(entry.get("type") or "").lower()
    return "user" if kind in ("user", "human") else kind


def _body_of(entry: dict):
    for container in (entry.get("message"), entry.get("payload"), entry):
        if isinstance(container, dict) and "content" in container:
            return container["content"]
    return None


def _tool_calls(entry: dict) -> list[dict]:
    """Tool-use blocks anywhere in this entry, whatever the envelope."""
    body = _body_of(entry)
    blocks = body if isinstance(body, list) else []
    calls = [
        block for block in blocks
        if isinstance(block, dict)
        and str(block.get("type") or "").lower() in ("tool_use", "tool_call", "function_call")
    ]
    # Codex puts the call at the top level rather than inside content.
    for container in (entry, entry.get("payload")):
        if isinstance(container, dict) and container.get("name") and (
            "arguments" in container or "input" in container
        ):
            calls.append(container)
    return calls


def _tool_input(call: dict) -> dict:
    raw = call.get("input")
    if raw is None:
        raw = call.get("arguments")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return raw if isinstance(raw, dict) else {}


def _plan_text(name: str, payload: dict) -> list[str]:
    """The agent's own statement of intent, out of a plan or todo tool call."""
    out: list[str] = []
    todos = payload.get("todos") or payload.get("plan") or payload.get("steps")
    if isinstance(todos, str):
        out.append(todos)
    elif isinstance(todos, list):
        for item in todos:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                text = item.get("content") or item.get("step") or item.get("title")
                if isinstance(text, str):
                    out.append(text)
    elif name == "exitplanmode":
        return []
    return out


def _errors_from(entry: dict) -> list[str]:
    """Error first-lines from tool results, never the body."""
    body = _body_of(entry)
    blocks = body if isinstance(body, list) else []
    out: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if not block.get("is_error"):
            continue
        content = block.get("content")
        for text in _content_blocks(content) or ([content] if isinstance(content, str) else []):
            out.append(text)
    if str(entry.get("type") or "").lower() in ("error", "agent.error"):
        message = entry.get("message") or entry.get("error")
        if isinstance(message, str):
            out.append(message)
    return out


def _issue(harvest: Harvest, code: str) -> None:
    if code not in harvest.issues:
        harvest.issues.append(code)


def _directory(path: str, harvest: Harvest, code: str) -> bool:
    try:
        return stat.S_ISDIR(os.stat(path).st_mode)
    except FileNotFoundError:
        return False
    except OSError:
        _issue(harvest, code)
        return False


def _transcript_files(home: str, harvest: Harvest | None = None) -> list[str]:
    """Agent session logs, newest first.

    Both harnesses write JSONL under their own dot-directory in HOME. Sorting by
    mtime means a truncated budget keeps today's workshop and drops a stale
    session from a previous restart.
    """
    found: list[tuple[float, str]] = []
    harvest = harvest if harvest is not None else Harvest()
    for root_name in (".claude", ".codex", ".omnigent"):
        root = os.path.join(home, root_name)
        if not _directory(root, harvest, "transcript_read_failed"):
            continue
        for directory, dirs, files in os.walk(
            root, onerror=lambda _error: _issue(harvest, "transcript_read_failed"),
        ):
            dirs[:] = [d for d in dirs if d not in _TRANSCRIPT_SKIP_DIRS]
            for name in files:
                if not name.endswith((".jsonl", ".ndjson")):
                    continue
                path = os.path.join(directory, name)
                try:
                    found.append((os.path.getmtime(path), path))
                except OSError:
                    _issue(harvest, "transcript_read_failed")
                    continue
    found.sort(reverse=True)
    # Each harness's newest transcript is read first, so one harness's many
    # small session files can never push another harness's live session out
    # of the file or byte budget.
    roots = (".claude", ".codex", ".omnigent")
    newest = []
    for root_name in roots:
        prefix = os.path.join(home, root_name) + os.sep
        first = next((path for _, path in found if path.startswith(prefix)), None)
        if first is not None:
            newest.append(first)
    ordered = newest + [path for _, path in found if path not in newest]
    return ordered[:MAX_TRANSCRIPT_FILES]


def _scan_transcripts(home: str, harvest: Harvest) -> None:
    budget = MAX_TRANSCRIPT_BYTES
    seen_errors: set[str] = set()
    seen_targets: set[str] = set()
    workers = _omnigent_worker_transcripts()
    for path in _transcript_files(home, harvest):
        seen_prompts: set[str] = set()
        session = _session_for(path, home, workers)
        if budget <= 0:
            break
        try:
            size = os.path.getsize(path)
        except OSError:
            _issue(harvest, "transcript_read_failed")
            continue
        budget -= size
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if len(line) > MAX_LINE_BYTES:
                        # A single line this large is a tool dump, not a turn.
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        _issue(harvest, "transcript_parse_failed")
                        continue
                    if isinstance(entry, dict):
                        _absorb(entry, harvest, seen_errors, seen_targets, seen_prompts, session)
        except OSError as exc:
            logger.debug("transcript unreadable (%s): %s", path, exc)
            _issue(harvest, "transcript_read_failed")
            continue
        if session.attendee and not session.codex_typed:
            _take_prompts(session.codex_fallback, harvest, seen_prompts)


def _take_prompts(texts: list[str], harvest: Harvest, seen_prompts: set[str]) -> None:
    for raw in texts:
        if _is_synthetic(raw):
            continue
        text = _authored(raw)
        # Codex writes each prompt twice: as a model-visible message and as
        # its own ``user_message`` event. Count it once per session file.
        key = " ".join(text.split())[:400].lower()
        if key in seen_prompts:
            continue
        seen_prompts.add(key)
        harvest.prompt_count += 1
        cleaned, hits = _clean(text, MAX_PROMPT_CHARS)
        harvest.redactions += hits
        if not cleaned or cleaned.lower() in _CONTINUATIONS:
            continue
        if len(harvest.prompts) < MAX_PROMPTS:
            harvest.prompts.append(cleaned)


def _absorb(
    entry: dict, harvest: Harvest, seen_errors: set[str], seen_targets: set[str],
    seen_prompts: set[str] | None = None, session: _Session | None = None,
) -> None:
    seen_prompts = seen_prompts if seen_prompts is not None else set()
    session = session if session is not None else _Session()
    _note_driver(entry, session)
    codex_typed = _codex_user_message(entry)
    if codex_typed is not None:
        session.codex_typed = True
        if session.attendee:
            _take_prompts([codex_typed], harvest, seen_prompts)
    elif _role_of(entry) == "user" and not _injected(entry):
        texts = _content_blocks(_body_of(entry))
        if str(entry.get("type") or "") == "response_item":
            session.codex_fallback.extend(texts)
        elif session.attendee:
            _take_prompts(texts, harvest, seen_prompts)

    # Errors and tool calls are read regardless of role. Claude Code delivers a
    # failed tool result on the *user* role, so an early return for user entries
    # would silently discard every blocker — the most commercially useful thing
    # this harvest produces.
    for call in _tool_calls(entry):
        name = str(call.get("name") or "").strip()
        label = _tool_name(name)
        if len(harvest.tool_calls) < 64 or label in harvest.tool_calls:
            harvest.tool_calls[label] = harvest.tool_calls.get(label, 0) + 1
        payload = _tool_input(call)
        if name.lower() in _PLAN_TOOLS:
            for text in _plan_text(name.lower(), payload):
                cleaned, hits = _clean(text, MAX_PLAN_CHARS)
                harvest.redactions += hits
                if cleaned and len(harvest.plans) < MAX_PLANS:
                    harvest.plans.append(cleaned)
            continue
        for key in _TARGET_KEYS:
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            target, hits = _first_line(value, MAX_TARGET_CHARS)
            harvest.redactions += hits
            entryline = f"{name} {target}".strip()
            if entryline and entryline not in seen_targets:
                seen_targets.add(entryline)
                if len(harvest.tool_targets) < MAX_TOOL_TARGETS:
                    harvest.tool_targets.append(entryline)
            break

    for text in _errors_from(entry):
        harvest.error_count += 1
        reason = error_reason(text)
        harvest.tool_errors[reason] = harvest.tool_errors.get(reason, 0) + 1
        first, hits = _first_line(text, MAX_ERROR_CHARS)
        harvest.redactions += hits
        # Deduped on the first line: one broken credential produces the same
        # error fifty times, and fifty copies would drown the other blockers.
        key = first.lower()
        if first and not _BARE_EXIT.match(first) and key not in seen_errors:
            seen_errors.add(key)
            if len(harvest.errors) < MAX_ERRORS:
                harvest.errors.append(first)


def _document_kind(path: str) -> str:
    parts = path.lower().split(os.sep)
    if "promote" in parts:
        return "promote_doc"
    name = os.path.basename(path).lower()
    for marker, kind in (
        ("readme", "readme"), ("prd", "prd"), ("plan", "plan"),
        ("architecture", "architecture"), ("security", "security"),
    ):
        if marker in name:
            return kind
    return "document"


def _interesting_document(path: str) -> bool:
    lowered = os.path.basename(path).lower()
    if not lowered.endswith(_DOCUMENT_SUFFIXES):
        return False
    if _document_kind(path) == "promote_doc":
        # Everything the promote skill writes qualifies, including jira-stories.md
        # and build-prompt.md — a name-marker list would silently drop the two
        # documents that state business value and restate the goal.
        return True
    return any(marker in lowered for marker in _DOCUMENT_NAMES)


def _document_roots(home: str, *, include_shared_tmp: bool) -> list[str]:
    roots = [os.path.join(home, name) for name in _DOCUMENT_ROOTS]
    if include_shared_tmp:
        # The promote skill's historical output location. Shared across attendees
        # on one container, so it is only trusted when this instance holds a
        # single attendee — otherwise one person's architecture doc would be
        # attributed to another, and to their employer.
        roots.append(os.path.join("/tmp", "promote"))
    return roots


def _scan_documents(home: str, harvest: Harvest, *, include_shared_tmp: bool) -> None:
    for root in _document_roots(home, include_shared_tmp=include_shared_tmp):
        if not _directory(root, harvest, "document_read_failed"):
            continue
        base_depth = root.rstrip(os.sep).count(os.sep)
        for directory, dirs, files in os.walk(
            root, onerror=lambda _error: _issue(harvest, "document_read_failed"),
        ):
            dirs[:] = [
                d for d in dirs
                if d not in _SKIP_DIRS and not d.startswith(".")
            ]
            if directory.count(os.sep) - base_depth >= MAX_DOCUMENT_DEPTH:
                dirs[:] = []
            for name in sorted(files):
                if len(harvest.artifacts) >= MAX_DOCUMENTS:
                    return
                if not _interesting_document(os.path.join(directory, name)):
                    continue
                _absorb_document(os.path.join(directory, name), harvest)


def _absorb_document(path: str, harvest: Harvest) -> None:
    try:
        size = os.path.getsize(path)
        with open(path, encoding="utf-8", errors="replace") as handle:
            body = handle.read(MAX_DOCUMENT_CHARS)
    except OSError as exc:
        logger.debug("document unreadable (%s): %s", path, exc)
        _issue(harvest, "document_read_failed")
        return
    title = os.path.basename(path)
    harvest.artifacts.append(
        Artifact(kind=_document_kind(path), title=title, bytes=size)
    )
    excerpt, hits = redact(body)
    harvest.redactions += hits
    harvest.documents.append((title, excerpt))


def _commit_subjects(home: str, harvest: Harvest) -> None:
    """Commit subjects from the attendee's repos — their own words about the work."""
    projects = os.path.join(home, "projects")
    if not _directory(projects, harvest, "commit_read_failed"):
        return
    try:
        names = sorted(os.listdir(projects))
    except OSError:
        _issue(harvest, "commit_read_failed")
        return
    commits = 0
    for name in names:
        repo = os.path.join(projects, name)
        workflow_file = os.path.join(repo, ".workshop", "design-workflow.json")
        if os.path.isfile(workflow_file) and not os.path.islink(workflow_file):
            try:
                with open(workflow_file, encoding="utf-8", errors="replace") as wf_handle:
                    wf_doc = json.load(wf_handle)
                preview_url = (
                    ((wf_doc.get("evidence") or {}).get("build") or {}).get("preview")
                    if isinstance(wf_doc, dict)
                    else None
                )
                if isinstance(preview_url, str) and preview_url.strip():
                    cleaned_url, hits = _clean(preview_url.strip(), MAX_TARGET_CHARS)
                    harvest.redactions += hits
                    if cleaned_url:
                        harvest.artifacts.append(
                            Artifact(
                                kind="deployed_app",
                                title=f"{name}: {cleaned_url}",
                                bytes=len(cleaned_url),
                            )
                        )
            except (OSError, ValueError):
                pass
    for name in names:
        if commits >= MAX_COMMITS:
            return
        repo = os.path.join(projects, name)
        if not _directory(os.path.join(repo, ".git"), harvest, "commit_read_failed"):
            continue
        try:
            result = subprocess.run(
                ["git", "-C", repo, "log", "--no-merges", "--format=%s", "-n", "50"],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            _issue(harvest, "commit_read_failed")
            continue
        if result.returncode != 0:
            from .stats import _unborn_head

            if result.returncode == 128 and _unborn_head(repo):
                continue
            _issue(harvest, "commit_read_failed")
            continue
        for subject in result.stdout.splitlines():
            if commits >= MAX_COMMITS:
                break
            cleaned, hits = _clean(subject, MAX_TARGET_CHARS)
            harvest.redactions += hits
            if not cleaned:
                continue
            harvest.artifacts.append(Artifact(kind="commit", title=cleaned, bytes=0))
            commits += 1


def activity(home: str) -> dict:
    """Counts-only agent activity from transcripts (W6), for stats and telemetry. Never raises."""
    result = Harvest()
    try:
        _scan_transcripts(home, result)
    except Exception as exc:  # noqa: BLE001
        logger.debug("agent activity scan failed: %s", exc)
        _issue(result, "transcript_read_failed")
    return {"prompts": result.prompt_count, "tool_calls": dict(sorted(result.tool_calls.items())),
            "tool_errors": dict(sorted(result.tool_errors.items())), "errors": result.error_count,
            "issues": list(result.issues)}


def scan_new_tool_targets(
    home: str, cursors: dict[str, int] | None = None
) -> list[str]:
    """Incrementally read newly appended tool targets from agent transcripts.

    Unlike ``harvest_user()``, which caps ``tool_targets`` at the first
    ``MAX_TOOL_TARGETS`` entries for summary payloads, this tracks per-file byte
    offsets in ``cursors`` and returns every new tool target appended since the
    previous scan so long-running sessions continue spotting services past 60
    tool calls.
    """
    state = cursors if cursors is not None else {}
    budget = MAX_TRANSCRIPT_BYTES
    targets: list[str] = []
    seen: set[str] = set()
    try:
        for path in _transcript_files(home):
            if budget <= 0:
                break
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            offset = state.get(path, 0)
            if size < offset:
                offset = 0
            if size == offset:
                continue
            budget -= size - offset
            try:
                with open(path, "rb") as handle:
                    handle.seek(offset)
                    pos = offset
                    while True:
                        raw_line = handle.readline()
                        if not raw_line:
                            break
                        if len(raw_line) > MAX_LINE_BYTES:
                            pos = handle.tell()
                            continue
                        line = raw_line.decode("utf-8", errors="replace")
                        try:
                            entry = json.loads(line)
                        except ValueError:
                            if not raw_line.endswith(b"\n"):
                                break
                            pos = handle.tell()
                            continue
                        pos = handle.tell()
                        if not isinstance(entry, dict):
                            continue
                        for call in _tool_calls(entry):
                            name = str(call.get("name") or "").strip()
                            if name.lower() in _PLAN_TOOLS:
                                continue
                            payload = _tool_input(call)
                            for key in _TARGET_KEYS:
                                value = payload.get(key)
                                if not isinstance(value, str) or not value.strip():
                                    continue
                                target, _ = _first_line(value, MAX_TARGET_CHARS)
                                entryline = f"{name} {target}".strip()
                                if entryline and entryline not in seen:
                                    seen.add(entryline)
                                    targets.append(entryline)
                                break
                    state[path] = pos
            except OSError as exc:
                logger.debug("transcript unreadable (%s): %s", path, exc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("incremental transcript target scan failed: %s", exc)
    return targets


def harvest_user(user: User, *, single_attendee: bool = True) -> Harvest:
    """Collect one attendee's residue. Never raises.

    Called on the wrap transition with the attendee present, and again as a
    teardown backstop. Both callers are best-effort paths where an exception would
    cost something more important than insight — the certificate moment in one
    case, the final stats harvest in the other.
    """
    result = Harvest()
    try:
        _scan_transcripts(user.home, result)
        _scan_documents(user.home, result, include_shared_tmp=single_attendee)
        _commit_subjects(user.home, result)
        if result.errors:
            from . import friction_scanner

            friction_scanner.scan_error_lines(user.email, result.errors)
    except Exception as exc:  # noqa: BLE001 — insight is never worth a 500
        logger.warning("artifact harvest failed for %s: %s", user.email, exc)
        _issue(result, "artifact_harvest_failed")
    return result


__all__ = [
    "Artifact",
    "Harvest",
    "activity",
    "error_reason",
    "MAX_DOCUMENTS",
    "MAX_PROMPTS",
    "harvest_user",
    "scan_new_tool_targets",
]
