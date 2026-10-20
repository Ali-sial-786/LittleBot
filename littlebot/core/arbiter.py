"""Resource arbitration so behaviours never fight over the face, speaker or screen.

A *behaviour* (activity, media playback, conversation turn, motion gesture)
acquires a lease on the resources it needs before starting. A request with
priority >= the current holder preempts it: the holder's ``on_preempt``
callback is invoked (it should stop itself promptly) and the lease moves.
A lower-priority request is refused.

Direct parent commands from the dashboard (e.g. "make a happy face") do not
need leases; they are one-shot and may be overridden by the next behaviour step.
"""

from __future__ import annotations

import enum
import itertools
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable

log = logging.getLogger(__name__)


class Resource(str, enum.Enum):
    FACE = "face"  # expression control
    AUDIO_OUT = "audio_out"  # speaker
    MICROPHONE = "microphone"
    SCREEN = "screen"  # full-screen ownership (video playback)
    MOTION = "motion"  # servos


class Priority(enum.IntEnum):
    AMBIENT = 0  # idle animations
    REACTIVE = 20  # reactions to camera events
    CONVERSATION = 40
    ACTIVITY = 50
    MEDIA = 60
    PARENT = 80  # explicit parent action from the dashboard
    SYSTEM = 100  # emergency stop, shutdown, errors


PreemptCallback = Callable[["Lease", str], None]


@dataclass(eq=False)
class Lease:
    owner: str
    resources: frozenset[Resource]
    priority: int
    on_preempt: PreemptCallback | None = None
    id: int = field(default_factory=itertools.count(1).__next__)
    active: bool = True
    _arbiter: "ResourceArbiter | None" = field(default=None, repr=False)

    def release(self) -> None:
        if self.active and self._arbiter is not None:
            self._arbiter.release(self)

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class ResourceArbiter:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._holders: dict[Resource, Lease] = {}

    def acquire(
        self,
        owner: str,
        resources: Iterable[Resource | str],
        priority: int,
        on_preempt: PreemptCallback | None = None,
    ) -> Lease | None:
        res = frozenset(Resource(r) for r in resources)
        to_preempt: list[Lease] = []
        with self._lock:
            for r in res:
                holder = self._holders.get(r)
                if holder is None or holder.owner == owner:
                    continue
                if priority < holder.priority:
                    log.info("lease for %s refused: %s held by %s (prio %d > %d)", owner, r.value, holder.owner, holder.priority, priority)
                    return None
                if holder not in to_preempt:
                    to_preempt.append(holder)
            for old in to_preempt:
                self._drop(old)
            # An owner re-acquiring replaces its own previous lease(s) on these resources.
            for r in res:
                prev = self._holders.get(r)
                if prev is not None and prev.owner == owner:
                    self._drop(prev)
            lease = Lease(owner=owner, resources=res, priority=int(priority), on_preempt=on_preempt, _arbiter=self)
            for r in res:
                self._holders[r] = lease
        for old in to_preempt:
            log.info("%s preempted by %s", old.owner, owner)
            if old.on_preempt is not None:
                try:
                    old.on_preempt(old, owner)
                except Exception:  # noqa: BLE001
                    log.exception("preempt callback for %s failed", old.owner)
        return lease

    def release(self, lease: Lease) -> None:
        with self._lock:
            self._drop(lease)

    def release_owner(self, owner: str) -> None:
        with self._lock:
            for lease in {l for l in self._holders.values() if l.owner == owner}:
                self._drop(lease)

    def preempt_all(self, by: str = "system") -> list[str]:
        """Revoke every lease (used by the parent STOP button and emergency stop)."""
        with self._lock:
            leases = list({l for l in self._holders.values()})
            for l in leases:
                self._drop(l)
        for l in leases:
            if l.on_preempt is not None:
                try:
                    l.on_preempt(l, by)
                except Exception:  # noqa: BLE001
                    log.exception("preempt callback for %s failed", l.owner)
        return [l.owner for l in leases]

    def holder(self, resource: Resource | str) -> Lease | None:
        with self._lock:
            return self._holders.get(Resource(resource))

    def is_free(self, resource: Resource | str) -> bool:
        return self.holder(resource) is None

    def snapshot(self) -> dict[str, str | None]:
        with self._lock:
            return {r.value: (self._holders[r].owner if r in self._holders else None) for r in Resource}

    def _drop(self, lease: Lease) -> None:
        lease.active = False
        for r in lease.resources:
            if self._holders.get(r) is lease:
                del self._holders[r]
