"""The switchboard: every call between agents goes through Hub.consult.

It works out who is calling (environment or explicit ``sender``), which thread
and hop this is, applies the policy and budgets, picks an agent by skill when
asked, adds the header and thread history, runs the agent, journals every step
and tells the notifiers (Telegram).
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import envelope
from .config import COSTS, Agent, Config
from .envelope import USER, Context, Event
from .runner import HailError, advice_enforced, ask
from .store import Store

Notifier = Callable[[Event], None]
MIN_COUNCIL = 2
MAX_COUNCIL = 6
SYNTH_BUDGET = 6000  # characters of answers handed to the synthesizer
ANONYMOUS = "unknown-agent"  # an MCP caller that could not be identified: never the user
SECRET_ENV = ("HAIL_TELEGRAM_TOKEN",)


def _declared_hop(hop: int | None) -> int:
    if hop is None:
        return 0
    if not isinstance(hop, int) or isinstance(hop, bool) or not 0 <= hop <= 100:
        raise HailError("'hop' must be a number between 0 and 100")
    return hop


class Refused(HailError):
    """The call was blocked by a guard rail (hops, budget, policy, stop)."""


@dataclass(frozen=True)
class Request:
    message: str
    agent: str | None = None
    skill: str | None = None
    thread: str | None = None
    mode: str | None = None
    sender: str | None = None  # honoured only when hail is not running inside an agent
    hop: int | None = None  # the caller's own hop from its [hail] header; can only raise the depth


@dataclass(frozen=True)
class Outcome:
    thread: str
    hop: int
    agent: str
    text: str
    ok: bool


@dataclass(frozen=True)
class CouncilResult:
    thread: str
    answers: tuple[Outcome, ...]
    synthesis: Outcome | None


class Hub:
    def __init__(
        self,
        config: Config,
        store: Store,
        environ: Mapping[str, str],
        notify: Notifier | None = None,
        trust_user: bool = True,
    ) -> None:
        """``trust_user``: an unidentified caller is the human (CLI) rather than an unknown agent (MCP)."""
        self.config = config
        self.store = store
        self.caller = envelope.from_env(environ)
        self.notify = notify
        self.trust_user = trust_user
        token_env = str((config.telegram or {}).get("token_env", SECRET_ENV[0]))
        self.secret_env = tuple(dict.fromkeys((*SECRET_ENV, token_env)))

    # public -------------------------------------------------------------------

    def identify(self, explicit: str | None) -> tuple[str, bool]:
        """Who is calling, and whether hail can vouch for it (environment) rather than take its word."""
        if self.caller.name != USER:
            return self.caller.name, True
        if explicit and explicit != USER:
            if not envelope.AGENT_RE.match(explicit):
                raise HailError(f"invalid sender name '{explicit}'")
            return explicit, False
        return (USER, True) if self.trust_user else (ANONYMOUS, False)

    def sender_for(self, explicit: str | None) -> str:
        return self.identify(explicit)[0]

    def thread_for(self, explicit: str | None) -> str:
        if explicit:
            if not envelope.valid_thread(explicit):
                raise HailError(f"invalid thread id '{explicit}' (letters, digits, - and _ only)")
            return explicit
        return self.caller.thread or envelope.new_thread_id()

    def consult(self, req: Request) -> Outcome:
        if not req.message.strip():
            raise HailError("empty message")
        sender, verified = self.identify(req.sender)
        thread = self.thread_for(req.thread)
        if not verified and sender != ANONYMOUS:
            verified = self.store.depth_of(thread, sender) > 0  # it was really asked something here
        mode = req.mode or ("action" if sender == USER else "advice")
        agent = self._target(req, sender, mode)
        base = max(self._base_hop(thread, sender), _declared_hop(req.hop) if sender != USER else 0)
        ctx = Context(thread, base + 1, sender, agent.name, mode, self.config.policy.max_hops)
        self._guard(ctx, verified, req.message.strip())
        history = self.store.history(thread, agent.name)
        notes = self.store.notes(thread)
        budget = min(envelope.DEFAULT_BUDGET, max(agent.max_chars - len(req.message) - 10, 0))
        text = envelope.compose(ctx, req.message.strip(), history, notes, budget)
        try:
            reply = ask(agent, text, env=envelope.callee_env(ctx), mode=mode, drop_env=self.secret_env)
        except HailError as exc:
            self._log(ctx, "error", str(exc), reverse=True)
            raise
        self._log(ctx, "reply" if reply.ok else "error", reply.text, reverse=True)
        return Outcome(thread, ctx.hop, agent.name, reply.text, reply.ok)

    def council(
        self,
        question: str,
        names: Sequence[str],
        synth: str | None = None,
        thread: str | None = None,
        mode: str | None = None,
    ) -> CouncilResult:
        """Ask the same question to several agents in parallel, then optionally have one synthesize."""
        unique = tuple(dict.fromkeys(names))
        if not MIN_COUNCIL <= len(unique) <= MAX_COUNCIL:
            raise HailError(f"a council needs two to {MAX_COUNCIL} different agents")
        sender = self.sender_for(None)
        thread_id = self.thread_for(thread)
        header = Event(thread_id, 0, sender, ", ".join(unique), "council", mode or "advice", question)
        self._emit(header)
        with ThreadPoolExecutor(max_workers=len(unique)) as pool:
            answers = tuple(pool.map(lambda n: self._safe(question, n, thread_id, mode), unique))
        good = [a for a in answers if a.ok]
        if not synth or not good:
            return CouncilResult(thread_id, answers, None)
        share = SYNTH_BUDGET // len(good)
        joined = "\n\n".join(f"[{a.agent}]\n{envelope.clip(a.text, share)}" for a in good)
        prompt = (
            f"Several agents answered this question:\n{question}\n\nTheir answers:\n{joined}\n\n"
            "Synthesize: where they agree, where they disagree, and the answer you recommend."
        )
        synthesis = self._safe(prompt, synth, thread_id, mode)
        return CouncilResult(thread_id, answers, synthesis)

    def route(self, skill: str, sender: str, mode: str) -> Agent:
        wanted = skill.strip().lower()
        candidates = [
            a
            for a in self.config.agents.values()
            if wanted in a.skills
            and a.name != sender
            and self.config.policy.check(sender, a.name, mode) is None
            and self._strict(sender, a.name, mode) is None
        ]
        if not candidates:
            known = sorted({s for a in self.config.agents.values() for s in a.skills})
            raise HailError(f"no agent with skill '{wanted}' is available (skills: {', '.join(known) or 'none'})")
        return min(candidates, key=lambda a: (COSTS.index(a.cost), a.name))

    # internals ----------------------------------------------------------------

    def _target(self, req: Request, sender: str, mode: str) -> Agent:
        if req.agent:
            agents = self.config.agents
            if req.agent not in agents:
                raise HailError(f"unknown agent '{req.agent}' (configured: {', '.join(agents) or 'none'})")
            return agents[req.agent]
        if req.skill:
            return self.route(req.skill, sender, mode)
        raise HailError("name an agent or a skill")

    def _strict(self, sender: str, recipient: str, mode: str) -> str | None:
        """Why [policy] strict_advice refuses this advice call, or None."""
        agent = self.config.agents.get(recipient)
        if mode != "advice" or sender == USER or not self.config.policy.strict_advice:
            return None
        if agent is None or advice_enforced(agent):
            return None
        return (
            f"'{recipient}' cannot enforce advice mode (read-only) and [policy] strict_advice is on; "
            "give it a read-only variant (advice_command, advice_agent_id) or ask the user"
        )

    def _base_hop(self, thread: str, sender: str) -> int:
        if sender == USER:
            return 0
        depth = self.store.depth_of(thread, sender)
        if self.caller.name == sender:
            return max(self.caller.hop, depth)  # a new thread id does not reset the chain
        return depth

    def _guard(self, ctx: Context, verified: bool, message: str) -> None:
        """Refuse (and journal why) or record the ask; budgets are checked and counted atomically."""
        policy = self.config.policy
        if ctx.hop > policy.max_hops:
            reason: str | None = f"hop limit reached ({ctx.hop} > {policy.max_hops}): answer with what you have"
        elif ctx.mode == "action" and ctx.sender != USER and not verified:
            reason = (
                f"'{ctx.sender}' is not a verified agent identity (HAIL_SELF), "
                "so it may ask for advice, not for an action"
            )
        else:
            reason = policy.check(ctx.sender, ctx.recipient, ctx.mode) or self._strict(
                ctx.sender, ctx.recipient, ctx.mode
            )
        if reason:
            self._log(ctx, "refused", reason)
            raise Refused(reason)
        reason = self.store.try_ask(
            ctx.thread,
            ctx.hop,
            ctx.sender,
            ctx.recipient,
            ctx.mode,
            message,
            policy.max_calls_per_thread,
            policy.rate_per_minute,
        )
        kind = "refused" if reason else "ask"
        event = Event(ctx.thread, ctx.hop, ctx.sender, ctx.recipient, kind, ctx.mode, reason or message, ctx.max_hops)
        self._emit(event)
        if reason:
            raise Refused(reason)

    def _safe(self, message: str, name: str, thread: str, mode: str | None) -> Outcome:
        try:
            return self.consult(Request(message=message, agent=name, thread=thread, mode=mode))
        except HailError as exc:
            return Outcome(thread, 0, name, str(exc), False)

    def _log(self, ctx: Context, kind: str, text: str, reverse: bool = False) -> None:
        sender, recipient = (ctx.recipient, ctx.sender) if reverse else (ctx.sender, ctx.recipient)
        self.store.add(ctx.thread, ctx.hop, sender, recipient, kind, ctx.mode, text)
        self._emit(Event(ctx.thread, ctx.hop, sender, recipient, kind, ctx.mode, text, ctx.max_hops))

    def _emit(self, event: Event) -> None:
        if self.notify is None:
            return
        try:
            self.notify(event)
        except Exception as exc:  # a notifier must never break a call
            print(f"hail: notifier failed: {exc}", file=sys.stderr)
