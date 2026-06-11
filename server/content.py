"""Workshop content: nugget packs, phases, triggers, broadcasts, shell config.

The pack ships in the repo (content/default_pack.json), can be replaced at
deploy time (CONTENT_PACK_PATH env) and live via the admin API. State is
in-memory by design — a redeploy resets to the deployed pack, which is the
correct behaviour for tear-down-after-the-event infrastructure.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time

from pydantic import BaseModel, Field

from . import config

logger = logging.getLogger(__name__)

_DEFAULT_PACK = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "content", "default_pack.json")
)

KNOWN_TRIGGERS = {"always", "claude_active", "codex_active", "bash_active", "idle_10m"}
_ELAPSED_PREFIX = "elapsed_gt_"


class NuggetLink(BaseModel):
    url: str
    label: str = "Learn more"


class Nugget(BaseModel):
    id: str
    title: str
    markdown: str
    link: NuggetLink | None = None
    tags: list[str] = Field(default_factory=list)
    phases: list[str] = Field(default_factory=list)  # empty = all phases
    triggers: list[str] = Field(default_factory=list)  # empty = always
    weight: int = 1
    pinned: bool = False


class ShellLink(BaseModel):
    label: str
    url: str
    icon: str = "link"


class ShellConfig(BaseModel):
    links: list[ShellLink] = Field(default_factory=list)
    features: dict[str, bool] = Field(default_factory=dict)


class ContentPack(BaseModel):
    version: int = 1
    phases: list[str] = Field(default_factory=lambda: ["intro", "setup", "build", "wrap"])
    shell: ShellConfig = Field(default_factory=ShellConfig)
    nuggets: list[Nugget] = Field(default_factory=list)


class Broadcast(BaseModel):
    message: str
    level: str = "info"  # info | success | warning
    ttl_s: int = 300


class ContentService:
    def __init__(self):
        self._lock = threading.Lock()
        self._pack = self._load_initial_pack()
        self._phase = config.workshop_phase_default()
        self._broadcast: Broadcast | None = None
        self._broadcast_at = 0.0
        self.started_at = time.time()

    def _load_initial_pack(self) -> ContentPack:
        path = config.content_pack_path() or _DEFAULT_PACK
        try:
            with open(path) as f:
                pack = ContentPack.model_validate(json.load(f))
            logger.info("content pack loaded from %s (%d nuggets)", path, len(pack.nuggets))
            return pack
        except Exception as e:
            logger.error("content pack unreadable at %s: %s — starting empty", path, e)
            return ContentPack()

    # -- read --

    @property
    def pack(self) -> ContentPack:
        with self._lock:
            return self._pack

    @property
    def phase(self) -> str:
        with self._lock:
            return self._phase

    def active_broadcast(self) -> Broadcast | None:
        with self._lock:
            if self._broadcast and time.time() - self._broadcast_at < self._broadcast.ttl_s:
                return self._broadcast
            return None

    def nuggets_for(self, active_triggers: set[str], limit: int = 8) -> list[dict]:
        """Nuggets for the current phase whose triggers are satisfied —
        pinned first, then a stable weighted shuffle of the rest."""
        with self._lock:
            pack, phase = self._pack, self._phase

        elapsed_min = (time.time() - self.started_at) / 60

        def eligible(n: Nugget) -> bool:
            if n.phases and phase not in n.phases:
                return False
            if not n.triggers:
                return True
            for t in n.triggers:
                if t == "always" or t in active_triggers:
                    return True
                if t.startswith(_ELAPSED_PREFIX):
                    try:
                        if elapsed_min > int(t[len(_ELAPSED_PREFIX):]):
                            return True
                    except ValueError:
                        pass
            return False

        candidates = [n for n in pack.nuggets if eligible(n)]
        pinned = [n for n in candidates if n.pinned]
        rest = [n for n in candidates if not n.pinned]
        # Weighted shuffle, reseeded every 5 minutes so the rotation feels
        # alive but doesn't reshuffle on every poll.
        rng = random.Random(int(time.time() // 300))
        rest = sorted(rest, key=lambda n: rng.random() / max(n.weight, 1))
        return [n.model_dump() for n in (pinned + rest)[:limit]]

    # -- write (admin) --

    def set_pack(self, pack: ContentPack) -> None:
        with self._lock:
            self._pack = pack
            if self._phase not in pack.phases and pack.phases:
                self._phase = pack.phases[0]
        logger.info("content pack replaced (%d nuggets)", len(pack.nuggets))

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self._phase = phase
        logger.info("workshop phase -> %s", phase)

    def set_broadcast(self, broadcast: Broadcast) -> None:
        with self._lock:
            self._broadcast = broadcast
            self._broadcast_at = time.time()
        logger.info("broadcast (%s): %s", broadcast.level, broadcast.message[:80])


content_service = ContentService()
