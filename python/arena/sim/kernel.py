"""The discrete-event simulation kernel.

Agents do not call each other. They do not read a clock, share memory, or see
anything except messages addressed to them. Everything that happens is an entry
in one priority queue, processed in strict order, and the only way to influence
the future is to put something else in the queue.

That restriction is what buys three properties the project depends on:

**Reproducibility.** A seeded run produces an identical event trace every time,
on any machine. Every ordering decision is made by the queue's key, never by
dictionary order, object identity, or wall-clock time.

**Honest information timing.** An agent learns things when a message reaches it,
not when they happen. That is the whole substrate for the latency and
information-asymmetry experiments -- a slow agent is slow because its messages
arrive later, not because it was told to pretend.

**Composability.** The exchange is just an agent. So is a data feed, and so is a
research probe. Nothing in the kernel knows what a market is.

Ordering is by ``(timestamp, sequence)``. The sequence is a monotonic insertion
counter, so it is unique and total: two events scheduled for the same nanosecond
still have a defined order, and it is the order they were created in. Falling
back to comparing payloads -- which is what a naive heap of tuples does when
timestamps tie -- would be a correctness bug that only shows up under load.
"""

from __future__ import annotations

import heapq
import random
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from arena.exchange.types import AgentId
from arena.sim.latency import LatencyModel, UniformLatency, _stable_seed
from arena.sim.time import Duration, Timestamp, format_timestamp

__all__ = ["Agent", "SimulationContext", "Kernel", "ScheduledEvent"]


@dataclass(frozen=True, slots=True)
class ScheduledEvent:
    """One entry in the queue. ``kind`` is 'wakeup' or 'deliver'."""

    timestamp: Timestamp
    sequence: int
    kind: str
    recipient: AgentId
    sender: AgentId | None = None
    payload: Any = None


class Agent(Protocol):
    """What the kernel requires of a participant.

    An agent may only act during a callback, and only through the context it is
    handed. It has no reference to the kernel, to other agents, or to the clock
    between callbacks -- so it cannot accidentally read state from a time it
    should not know about.
    """

    agent_id: AgentId

    def on_start(self, ctx: SimulationContext) -> None:
        """Called once before the simulation runs. Schedule first wakeups here."""

    def on_wakeup(self, ctx: SimulationContext) -> None:
        """A wakeup the agent previously requested has come due."""

    def on_message(self, ctx: SimulationContext, sender: AgentId, message: Any) -> None:
        """A message addressed to this agent has arrived."""

    def on_finish(self, ctx: SimulationContext) -> None:
        """Called once after the simulation ends. For flushing state, not acting."""


@dataclass(slots=True)
class SimulationContext:
    """The only interface an agent has to the world.

    Deliberately narrow. An agent can learn the current time, send a message,
    ask to be woken, and draw from its own random stream. It cannot inspect the
    queue, address the kernel, or reach another agent directly.
    """

    _kernel: Kernel
    _agent_id: AgentId

    @property
    def now(self) -> Timestamp:
        return self._kernel.now

    @property
    def agent_id(self) -> AgentId:
        return self._agent_id

    def send(self, recipient: AgentId, message: Any) -> None:
        """Send a message, which arrives after the configured latency."""
        self._kernel.send(self._agent_id, recipient, message)

    def request_wakeup(self, delay: Duration) -> None:
        """Ask to be woken ``delay`` from now.

        A delay rather than an absolute time, so an agent cannot schedule itself
        into the past -- which the queue would accept and then process out of
        order relative to everything already pending.
        """
        self._kernel.schedule_wakeup(self._agent_id, delay)

    @property
    def rng(self) -> random.Random:
        """This agent's private random stream.

        Seeded from the kernel seed and the agent's own id, so streams are
        independent: adding an agent to a configuration cannot shift the draws
        any other agent makes. Without that, changing the population would
        silently change every agent's behaviour and no two runs would be
        comparable.
        """
        return self._kernel.rng_for(self._agent_id)


class Kernel:
    """Drives the simulation. Owns the clock, the queue, and the agents."""

    def __init__(
        self,
        seed: int = 0,
        latency: LatencyModel | None = None,
    ) -> None:
        self.seed = seed
        self.latency = latency if latency is not None else UniformLatency(seed=seed)
        self._queue: list[tuple[int, int, ScheduledEvent]] = []
        self._sequence = 0
        self._now: Timestamp = Timestamp(0)
        self._agents: dict[AgentId, Agent] = {}
        self._rngs: dict[AgentId, random.Random] = {}
        self._processed = 0
        self._trace: list[ScheduledEvent] = []
        self.record_trace = False

    # -- registration ------------------------------------------------------

    def add(self, agent: Agent) -> None:
        if agent.agent_id in self._agents:
            raise ValueError(f"duplicate agent id {agent.agent_id!r}")
        self._agents[agent.agent_id] = agent

    def add_all(self, agents: Iterable[Agent]) -> None:
        for agent in agents:
            self.add(agent)

    @property
    def agent_ids(self) -> tuple[AgentId, ...]:
        """Registered agents in sorted order.

        Sorted, not insertion-ordered: any broadcast iterates this, and using
        insertion order would make the result depend on how a configuration file
        happened to be written.
        """
        return tuple(sorted(self._agents))

    def rng_for(self, agent_id: AgentId) -> random.Random:
        rng = self._rngs.get(agent_id)
        if rng is None:
            rng = random.Random(_stable_seed(self.seed, "agent", agent_id))
            self._rngs[agent_id] = rng
        return rng

    # -- clock and queue ---------------------------------------------------

    @property
    def now(self) -> Timestamp:
        return self._now

    @property
    def processed(self) -> int:
        return self._processed

    @property
    def trace(self) -> tuple[ScheduledEvent, ...]:
        return tuple(self._trace)

    def _push(self, event: ScheduledEvent) -> None:
        heapq.heappush(self._queue, (int(event.timestamp), event.sequence, event))

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def schedule_wakeup(self, agent_id: AgentId, delay: Duration) -> None:
        if delay < 0:
            raise ValueError("cannot schedule a wakeup in the past")
        self._push(
            ScheduledEvent(
                timestamp=Timestamp(int(self._now) + int(delay)),
                sequence=self._next_sequence(),
                kind="wakeup",
                recipient=agent_id,
            )
        )

    def send(self, sender: AgentId, recipient: AgentId, message: Any) -> None:
        """Queue a message for delivery after the pair's latency."""
        if recipient not in self._agents:
            raise KeyError(f"no such agent {recipient!r}")
        delay = self.latency.delay(sender, recipient)
        self._push(
            ScheduledEvent(
                timestamp=Timestamp(int(self._now) + max(1, int(delay))),
                sequence=self._next_sequence(),
                kind="deliver",
                recipient=recipient,
                sender=sender,
                payload=message,
            )
        )

    # -- running -----------------------------------------------------------

    def run(self, until: Timestamp | None = None, max_events: int | None = None) -> None:
        """Process the queue until it empties, time runs out, or the cap is hit.

        The ``max_events`` cap is a safety net, not a feature: an agent that
        wakes itself with a zero delay would otherwise spin forever, and an
        unbounded loop inside a test suite is much harder to diagnose than a
        loud stop.
        """
        for agent_id in self.agent_ids:
            agent = self._agents[agent_id]
            agent.on_start(SimulationContext(self, agent_id))

        while self._queue:
            if max_events is not None and self._processed >= max_events:
                break
            timestamp, _sequence, event = self._queue[0]
            if until is not None and timestamp > int(until):
                break
            heapq.heappop(self._queue)

            # Time never moves backwards. If it did, an agent could observe an
            # earlier state after a later one and the whole information-timing
            # story would be false.
            if timestamp < int(self._now):
                raise RuntimeError(
                    f"event at {timestamp} scheduled before current time {self._now}"
                )
            self._now = Timestamp(timestamp)
            self._processed += 1
            if self.record_trace:
                self._trace.append(event)

            agent = self._agents[event.recipient]
            ctx = SimulationContext(self, event.recipient)
            if event.kind == "wakeup":
                agent.on_wakeup(ctx)
            else:
                agent.on_message(ctx, event.sender, event.payload)

        if until is not None:
            self._now = Timestamp(max(int(self._now), int(until)))

        for agent_id in self.agent_ids:
            agent = self._agents[agent_id]
            agent.on_finish(SimulationContext(self, agent_id))

    def summary(self) -> str:
        return (
            f"kernel seed={self.seed} agents={len(self._agents)} "
            f"events={self._processed} clock={format_timestamp(self._now)}"
        )
