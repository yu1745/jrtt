"""Subscriber state and registry.

A Subscriber represents one active CLI `tail` connection. The registry is
queried by the broadcaster after each ring-buffer append to decide which
subscribers should receive the new entry.

Thread-safety: registry mutations (add/remove) MUST happen on the pipe
server thread; reads (collect_new) happen on the broadcaster thread. We
guard mutations with a lock; reads lock-free via dict snapshot.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Optional

from jrtt.daemon.ring_buffer import RingEntry


@dataclass
class Subscriber:
    """One active tail subscriber."""

    id: str
    channel: int = 0
    regex: Optional[re.Pattern] = None
    replay_last_n: int = 0  # on connect, replay this many historical entries
    last_index: int = 0     # index into ring-buffer snapshot last seen (advances each broadcast)
    # Opaque writer-side handle used by the broadcaster. Typically a queue
    # or a write function. The dataclass doesn't know about transport.
    sender = None

    def matches(self, data: bytes) -> bool:
        """Filter applied to the line's bytes (channel already filtered elsewhere)."""
        if self.regex is None:
            return True
        try:
            return bool(self.regex.search(data))
        except re.error:
            return False

    def matches_entry(self, entry: RingEntry) -> bool:
        if entry.channel != self.channel:
            return False
        return self.matches(entry.data)

    def filter_new(self, snapshot: list[RingEntry]) -> list[RingEntry]:
        """Return entries from snapshot that this subscriber has not yet seen
        AND that pass its filter."""
        # Replay-on-attach: start cursor at len(snapshot) - replay_last_n
        if self.last_index == 0 and self.replay_last_n > 0:
            self.last_index = max(0, len(snapshot) - self.replay_last_n)
        out: list[RingEntry] = []
        for i in range(self.last_index, len(snapshot)):
            e = snapshot[i]
            if self.matches_entry(e):
                out.append(e)
        return out


class SubscriberRegistry:
    """Tracks active subscribers; broadcast queries which entries to send."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, Subscriber] = {}

    def add(self, sub: Subscriber) -> None:
        with self._lock:
            self._subs[sub.id] = sub

    def remove(self, sub_id: str) -> None:
        with self._lock:
            self._subs.pop(sub_id, None)

    def get(self, sub_id: str) -> Optional[Subscriber]:
        with self._lock:
            return self._subs.get(sub_id)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._subs)

    def all_ids(self) -> list[str]:
        with self._lock:
            return list(self._subs.keys())

    def collect_new(self, snapshot: list[RingEntry], *, since_ts: float = 0.0) -> dict[str, list[RingEntry]]:
        """Compute, for each subscriber, which new entries to send.

        since_ts: only entries with ts >= since_ts are eligible (typically
        set to "now at connect time" to drop stale history).

        Updates each subscriber's last_index cursor. Returns
        {subscriber_id: [entries...]}. Subscribers with no new entries are
        omitted from the result (caller checks len()).
        """
        out: dict[str, list[RingEntry]] = {}
        with self._lock:
            for sid, sub in self._subs.items():
                # First call: advance past anything older than since_ts so we
                # don't replay stale data the subscriber didn't ask for.
                if sub.last_index == 0:
                    sub.last_index = sum(1 for e in snapshot if e.ts < since_ts)
                matches = sub.filter_new(snapshot)
                if matches:
                    # Advance cursor past everything we just returned (whether or not it matched)
                    sub.last_index = len(snapshot)
                    out[sid] = matches
        return out