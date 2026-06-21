"""Tests for subscriber state and matching/broadcasting.

The pipe server orchestration is tested at integration level (real pipes).
Here we test the pure logic: per-subscriber filter + ring buffer snapshot.
"""

from __future__ import annotations

import re
import threading
import time
import uuid

import pytest

from jrtt.daemon.subscriber import Subscriber, SubscriberRegistry
from jrtt.daemon.ring_buffer import RingBuffer, RingEntry


def make_entry(s: str, channel: int = 0, ts: float = 0.0) -> RingEntry:
    return RingEntry(ts=ts, channel=channel, data=s.encode())


def test_subscriber_default_filter_passes_everything() -> None:
    sub = Subscriber(id="s1", channel=0, regex=None)
    assert sub.matches(b"hello world\n")
    assert sub.matches(b"")


def test_subscriber_regex_filter() -> None:
    """Regex must be a bytes pattern (we feed it raw line bytes)."""
    sub = Subscriber(id="s1", channel=0, regex=re.compile(rb"^\[ERR\]"))
    assert sub.matches(b"[ERR] something went wrong\n")
    assert not sub.matches(b"[INFO] all good\n")


def test_subscriber_channel_filter() -> None:
    sub = Subscriber(id="s1", channel=1)
    e0 = make_entry("x", channel=0)
    e1 = make_entry("y", channel=1)
    assert not sub.matches_entry(e0)
    assert sub.matches_entry(e1)


def test_subscriber_no_double_match_on_already_seen_index() -> None:
    """If broadcaster advances the cursor, subscriber should not see same line twice."""
    rb = RingBuffer(capacity=10)
    rb.append(make_entry("a", ts=1.0))
    rb.append(make_entry("b", ts=2.0))
    rb.append(make_entry("c", ts=3.0))
    sub = Subscriber(id="s1", channel=0, regex=None)
    sub.last_index = 0
    snap = rb.snapshot()
    out = sub.filter_new(snap)
    assert [e.data for e in out] == [b"a", b"b", b"c"]
    sub.last_index = len(snap)
    out2 = sub.filter_new(snap)
    assert out2 == []  # already saw them


def test_subscriber_last_n_replay() -> None:
    """A new subscriber with last_n > 0 sees the last N entries on connect."""
    rb = RingBuffer(capacity=100)
    for i in range(10):
        rb.append(make_entry(f"line{i}", ts=float(i)))
    sub = Subscriber(id="s1", channel=0, regex=None, replay_last_n=3)
    snap = rb.snapshot()
    out = sub.filter_new(snap)
    assert [e.data for e in out] == [b"line7", b"line8", b"line9"]


# ---- SubscriberRegistry ----


def test_registry_register_remove() -> None:
    reg = SubscriberRegistry()
    s1 = Subscriber(id="a", channel=0, regex=None)
    s2 = Subscriber(id="b", channel=0, regex=None)
    reg.add(s1)
    reg.add(s2)
    assert reg.count == 2
    reg.remove("a")
    assert reg.count == 1
    assert reg.get("a") is None
    assert reg.get("b") is s2


def test_registry_broadcasts_to_all_matching() -> None:
    """A new ring entry is fanned out to every subscriber that matches it."""
    rb = RingBuffer(capacity=10)
    rb.append(make_entry("old", ts=1.0))
    reg = SubscriberRegistry()
    reg.add(Subscriber(id="all", channel=0, regex=None))
    reg.add(Subscriber(id="err", channel=0, regex=re.compile(rb"ERR")))
    reg.add(Subscriber(id="ch1", channel=1))

    new_entry = make_entry("new ERR", ts=2.0)
    rb.append(new_entry)
    snap = rb.snapshot()
    # Manually fan out (registry.broadcast does this)
    out = reg.collect_new(snap, since_ts=1.5)
    # 'all' sees new ERR, 'err' sees it too, 'ch1' doesn't (wrong channel)
    assert len(out["all"]) == 1
    assert len(out["err"]) == 1
    # ch1 has no matching entry; should be absent from result
    assert "ch1" not in out


def test_registry_collect_only_new() -> None:
    """Subsequent collect calls do not return already-seen entries."""
    rb = RingBuffer(capacity=10)
    reg = SubscriberRegistry()
    sub = Subscriber(id="s", channel=0, regex=None)
    reg.add(sub)

    for i in range(3):
        rb.append(make_entry(f"line{i}", ts=float(i)))
    snap = rb.snapshot()

    first = reg.collect_new(snap, since_ts=0.0)
    assert len(first["s"]) == 3

    # Nothing new in ring; collect again — subscriber has no new entries
    second = reg.collect_new(snap, since_ts=0.0)
    assert second.get("s", []) == []


def test_registry_broadcast_handles_subscriber_removal_during_iteration() -> None:
    """If a subscriber is removed while we hold a reference, broadcast must not crash."""
    reg = SubscriberRegistry()
    sub = Subscriber(id="s1", channel=0, regex=None)
    reg.add(sub)
    snap = [make_entry("x", ts=1.0)]
    # Remove before broadcast
    reg.remove("s1")
    # Should not raise even though subscriber is gone
    out = reg.collect_new(snap, since_ts=0.0)
    assert out == {}