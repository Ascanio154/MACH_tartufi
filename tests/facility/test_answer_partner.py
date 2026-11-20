"""Facilities answer another ASST's booking centre: one offer message or a cancel.

Run from the repo root with the city-cli Python (it ships city_sdk):
    ~/.local/share/uv/tools/city-cli/bin/python -m unittest tests.facility.test_answer_partner -v
"""

from __future__ import annotations

import importlib.util
import itertools
import unittest
from pathlib import Path

from city_sdk.city import LEDGER, Message

_ROOT = Path(__file__).resolve().parents[2] / "asst"


def _load(folder: str):
    spec = importlib.util.spec_from_file_location(f"{folder}_brain", _ROOT / folder / "agent" / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._PULL_PAUSE = 0
    return module


hospital = _load("hospital")
specialist = _load("specialist")

HOSP = "agent:hosp"
SPEC = "agent:spec"
PBC = "agent:partner-bc"
NEED = "N-2026-11-23-0099"
NOW = "2026-11-23T08:00:00+01:00"  # Monday
_ids = itertools.count(1)


def msg(sender: str, text: str, recipient: str = "") -> Message:
    return Message(type="chat", sender=sender, msg_id=f"f-{next(_ids)}", need_id=NEED,
                   body={"text": text}, recipient=recipient)


class FacilityCity:
    def __init__(self, agent_id: str, chat: list[Message]) -> None:
        self.agent_id = agent_id
        self.address = ""
        self.chat = chat
        self.inbound = chat[-1]
        self.attempts: list[tuple[str, str]] = []

    async def say(self, to: str, text: str, *, status: str | None = None) -> str:
        self.attempts.append((to, text))
        self.chat.append(msg(self.agent_id, text, recipient=to))
        return "ok"

    async def history(self, need_id: str | None = None) -> list[Message]:
        return list(self.chat)

    def sent_to(self, to: str) -> list[str]:
        return [text for target, text in self.attempts if target == to]


def hslot(sid: str, doc: str, at: str, day: str, *, facility: str = HOSP, proc: str = "VIS-FIS",
          extra: str = "") -> str:
    return (f"slot_id={sid} doctor_id=ASST-1-{doc} facility={facility} procedure={proc} "
            f"at={at} day={day} free=1 {extra}").strip()


HOSP_SLOTS = [
    hslot("H-1", "h1", "2026-11-23T14:00:00+01:00", "mon"),
    hslot("H-2", "h1", "2026-11-23T16:00:00+01:00", "mon", extra="urgent_reserve=true"),
    hslot("H-3", "h3", "2026-11-24T10:30:00+01:00", "tue"),
    hslot("H-4", "h2", "2026-11-25T09:00:00+01:00", "wed"),
    hslot("H-5", "h1", "2026-11-24T12:00:00+01:00", "tue"),
    hslot("H-X", "h1", "2026-11-24T09:00:00+01:00", "tue", facility="agent:other"),
]


def facility_chat(agent: str, slots: list[str], *, proc: str = "VIS-FIS",
                  service: str = "hospital.physiatry") -> list[Message]:
    world = f"slots procedure={proc} page=1/1\n" + "\n".join(slots)
    return [
        msg(agent, f"world procedure={proc} page=1", LEDGER),
        msg(agent, "agenda", LEDGER),
        msg(LEDGER, world, agent),
        msg(LEDGER, f"agenda agenda_now={NOW}", agent),
        msg(PBC, f"need.request service_type={service} procedure={proc} priority=B request_id=x-1", agent),
    ]


async def run(brain, city: FacilityCity) -> None:
    await brain._facility(city, city.inbound, city.chat, reports=False, hold_self=False)


def offer_rows(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("offer ")]


class AnswerPartner(unittest.IsolatedAsyncioTestCase):
    async def test_partner_request_gets_one_offer(self) -> None:
        city = FacilityCity(HOSP, facility_chat(HOSP, HOSP_SLOTS))
        await run(hospital, city)
        sent = city.sent_to(PBC)
        self.assertEqual(len(sent), 1)
        rows = offer_rows(sent[0])
        slots = {tok for row in rows for tok in row.split() if tok.startswith("slot_id=")}
        self.assertEqual(slots, {"slot_id=H-1", "slot_id=H-3", "slot_id=H-5"})
        for row in rows:
            tokens = row.split()
            self.assertIn(f"facility={HOSP}", tokens)
            self.assertIn("request_id=x-1", tokens)
            self.assertTrue(any(tok.startswith("offer_id=p-") for tok in tokens))
            self.assertTrue(any(tok.startswith("doctor_lang=") for tok in tokens))
            self.assertTrue(any(tok.startswith("doctor_gender=") for tok in tokens))
        h3 = next(row for row in rows if "slot_id=H-3" in row.split())
        self.assertIn("doctor_lang=it/si", h3.split())
        self.assertIn("doctor_gender=m", h3.split())

    async def test_no_slot_cancel(self) -> None:
        late = [hslot("H-4", "h2", "2026-11-25T09:00:00+01:00", "wed")]
        city = FacilityCity(HOSP, facility_chat(HOSP, late))
        await run(hospital, city)
        self.assertEqual(city.sent_to(PBC), ["cancel request_id=x-1 reason=no_availability"])

    async def test_answered_once(self) -> None:
        city = FacilityCity(HOSP, facility_chat(HOSP, HOSP_SLOTS))
        await run(hospital, city)
        await run(hospital, city)
        self.assertEqual(len(city.sent_to(PBC)), 1)

    async def test_specialist_block_same(self) -> None:
        cardio = [hslot("C-1", "s1", "2026-11-23T14:30:00+01:00", "mon", facility=SPEC,
                        proc="VIS-CARD")]
        chat = facility_chat(SPEC, cardio, proc="VIS-CARD", service="specialist_care.cardiology")
        city = FacilityCity(SPEC, chat)
        await specialist._facility(city, city.inbound, city.chat, reports=True, hold_self=False)
        rows = offer_rows(city.sent_to(PBC)[0])
        self.assertEqual(len(rows), 1)
        self.assertIn("slot_id=C-1", rows[0].split())


if __name__ == "__main__":
    unittest.main()
