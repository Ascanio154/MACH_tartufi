"""Hospital/specialist: a confirm for a slot another citizen is already booking gets an
immediate "taken" cancel, instead of queueing behind that booking (Sat 21/11: five citizens
confirmed s2 22 Dec 10:30; the losers heard nothing for 12-17 minutes).

    ~/.local/share/uv/tools/city-cli/bin/python -m unittest tests.facility.test_slot_taken -v
"""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import unittest
from pathlib import Path

from city_sdk.city import LEDGER, Message

_ROOT = Path(__file__).resolve().parents[2] / "asst"


def _load(folder: str):
    spec = importlib.util.spec_from_file_location(f"{folder}_taken", _ROOT / folder / "agent" / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._PULL_PAUSE = 0
    return module


BC = "agent:bc"
SLOT = "W01QQQ-asst-greco-s2-20261222-1030"
_ids = itertools.count(1)


def msg(sender: str, text: str, need: str, recipient: str = "") -> Message:
    return Message(type="chat", sender=sender, msg_id=f"t-{next(_ids)}", need_id=need,
                   body={"text": text}, recipient=recipient)


class GatedCity:
    """Per-need mailboxes. The Ledger never answers; history for ``gated`` needs blocks until
    the gate opens, so that booking stays in flight."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.address = ""
        self.chats: dict[str, list[Message]] = {}
        self.sent: list[tuple[str, str, str]] = []
        self.gate = asyncio.Event()
        self.gated: set[str] = set()
        self.reads: dict[str, int] = {}

    async def say(self, to: str, text: str, *, status: str | None = None) -> str:
        need = self._need
        self.sent.append((need, to, text))
        row = msg(self.agent_id, text, need, recipient=to)
        self.chats.setdefault(need, []).append(row)
        return row.msg_id

    async def history(self, need_id: str | None = None) -> list[Message]:
        self.reads[need_id or ""] = self.reads.get(need_id or "", 0) + 1
        if need_id in self.gated:
            await self.gate.wait()
        return list(self.chats.get(need_id or "", []))

    def confirm(self, need: str, citizen: str) -> Message:
        offer = f"of-{SLOT}"
        row = msg(BC, f"confirm offer_id={offer} slot_id={SLOT} citizen={citizen} "
                      f"consent_ref=cons-{need} facility={self.agent_id}", need, self.agent_id)
        self.chats.setdefault(need, []).append(row)
        return row

    def holds(self, need: str) -> list[str]:
        return [t for n, to, t in self.sent if n == need and to == LEDGER and t.startswith("hold ")]

    def to_bc(self, need: str) -> list[str]:
        return [t for n, to, t in self.sent if n == need and to == BC]


class RefusingCity(GatedCity):
    """The Ledger refuses every hold at once."""

    async def say(self, to: str, text: str, *, status: str | None = None) -> str:
        sent = await super().say(to, text, status=status)
        if to == LEDGER and text.startswith("hold "):
            self.chats[self._need].append(
                msg(LEDGER, "refused op=hold code=SLOT_UNAVAILABLE", self._need, self.agent_id))
        return sent


class _Taken:
    module = None
    agent = ""

    def setUp(self) -> None:
        self.module._SLOT_LOCKS.clear()
        self.module._SLOT_OWNER.clear()

    async def run_confirm(self, city: GatedCity, need: str, citizen: str) -> None:
        city._need = need
        message = city.confirm(need, citizen)
        await self.module._on_confirm(city, message, list(city.chats[need]), hold_self=False)

    async def test_second_citizen_gets_taken_at_once(self) -> None:
        city = GatedCity(self.agent)
        city.gated.add("N-A")
        first = asyncio.create_task(self.run_confirm(city, "N-A", "citizen:a"))
        await asyncio.sleep(0)  # A claims the slot and waits inside its booking
        city._need = "N-B"
        await asyncio.wait_for(self.run_confirm(city, "N-B", "citizen:b"), timeout=1.0)
        self.assertEqual(city.holds("N-B"), [], "no hold for a slot another citizen is booking")
        self.assertEqual(city.to_bc("N-B"), [f"cancel offer_id=of-{SLOT} reason=taken"])
        city.gate.set()
        await first

    async def test_refused_hold_frees_the_slot_for_the_next_confirm(self) -> None:
        city = RefusingCity(self.agent)
        await self.run_confirm(city, "N-A", "citizen:a")
        self.assertEqual(len(city.holds("N-A")), 1)
        await self.run_confirm(city, "N-C", "citizen:c")
        self.assertEqual(len(city.holds("N-C")), 1, "after the Ledger refused A, C may try")

    async def test_silent_ledger_keeps_the_claim_and_ends_the_turn_quickly(self) -> None:
        # Sat 21/11: Ledger answers arrive after ~3 min (delivery). Waiting 40 s in the turn
        # never caught them; the late held/committed deliveries finish the booking instead.
        city = GatedCity(self.agent)
        await self.run_confirm(city, "N-A", "citizen:a")
        self.assertLessEqual(city.reads["N-A"], self.module._BOOK_TRIES + 2)
        await self.run_confirm(city, "N-C", "citizen:c")
        self.assertEqual(city.holds("N-C"), [], "A's hold is still pending: C gets taken")
        self.assertEqual(city.to_bc("N-C"), [f"cancel offer_id=of-{SLOT} reason=taken"])

    async def test_stale_claim_lapses(self) -> None:
        city = GatedCity(self.agent)
        await self.run_confirm(city, "N-A", "citizen:a")
        old = self.module._CLAIM_FOR
        self.module._CLAIM_FOR = 0.0
        try:
            await self.run_confirm(city, "N-C", "citizen:c")
        finally:
            self.module._CLAIM_FOR = old
        self.assertEqual(len(city.holds("N-C")), 1)

    async def test_late_held_delivery_commits(self) -> None:
        city = GatedCity(self.agent)
        await self.run_confirm(city, "N-A", "citizen:a")
        held = msg(LEDGER, f"held hold_id=bk-late slot_id={SLOT} fence=1", "N-A", self.agent)
        city.chats["N-A"].append(held)
        city._need = "N-A"
        await self.module._facility(city, held, list(city.chats["N-A"]), reports=False, hold_self=False)
        commits = [t for n, to, t in city.sent if n == "N-A" and to == LEDGER and t.startswith("commit ")]
        self.assertEqual(len(commits), 1)
        self.assertIn("hold_id=bk-late", commits[0].split())

    async def test_same_citizen_twice_is_not_taken(self) -> None:
        city = GatedCity(self.agent)
        city.gated.add("N-A")
        first = asyncio.create_task(self.run_confirm(city, "N-A", "citizen:a"))
        await asyncio.sleep(0)
        second = asyncio.create_task(self.run_confirm(city, "N-A", "citizen:a"))
        await asyncio.sleep(0)
        city.gate.set()
        await asyncio.gather(first, second)
        self.assertEqual(city.to_bc("N-A"), [], "a duplicate confirm never gets 'taken'")
        self.assertEqual(len(city.holds("N-A")), 1)


class HospitalTaken(_Taken, unittest.IsolatedAsyncioTestCase):
    module = _load("hospital")
    agent = "agent:xtj1dk9x3qej"


class SpecialistTaken(_Taken, unittest.IsolatedAsyncioTestCase):
    module = _load("specialist")
    agent = "agent:q18es2d82f8r"


if __name__ == "__main__":
    unittest.main()
