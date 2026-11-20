"""Booking centre quick wins: one offer message, P1 renewal, rules, facility down.

Run from the repo root with the city-cli Python (it ships city_sdk):
    ~/.local/share/uv/tools/city-cli/bin/python -m unittest tests.booking_centre.test_quick_wins -v
"""

from __future__ import annotations

import importlib.util
import itertools
import unittest
from pathlib import Path

from city_sdk.city import LEDGER, CapError, Message

_BRAIN = Path(__file__).resolve().parents[2] / "asst" / "booking_centre" / "agent" / "agent.py"
_spec = importlib.util.spec_from_file_location("booking_brain", _BRAIN)
bc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bc)
bc._PULL_PAUSE = 0


def reset_memory() -> None:
    """The brain keeps per-process memory in module globals: start every test clean."""

    for name in ("_SENT", "_GATES", "_CROSS_ASKED", "_RECEIPTS", "_LAST_OFFERS", "_BOOK_ASKED",
                 "_PENDING", "_CONFIRMED_BY", "_BOOKED", "_AGENT_NOTED", "_CANCELS_SENT", "_CONFIRMED_TO"):
        getattr(bc, name).clear()

AGENT = "agent:bc"
HOSP = "agent:hosp"
HOSP2 = "agent:hosp2"
CIT = "citizen:c1"
NEED = "N-2026-11-23-0001"
RX = "prescription_number-ABCD"
NOW = "2026-11-23T08:00:00+01:00"  # Monday
_ids = itertools.count(1)


def msg(sender: str, text: str, recipient: str = "") -> Message:
    return Message(
        type="chat",
        sender=sender,
        msg_id=f"m-{next(_ids)}",
        need_id=NEED,
        body={"text": text},
        recipient=recipient,
    )


class FakeCity:
    """Records every send attempt; a send to a target in ``down`` raises like the Relay."""

    def __init__(self, chat: list[Message], down: set[str] | None = None) -> None:
        self.agent_id = AGENT
        self.address = ""
        self.inbound = chat[-1] if chat else None
        self.chat = chat
        self.down = down or set()
        self.attempts: list[tuple[str, str]] = []

    async def say(self, to: str, text: str, *, status: str | None = None) -> str:
        self.attempts.append((to, text))
        if to in self.down:
            raise CapError("TARGET_NOT_VISIBLE", to)
        self.chat.append(msg(AGENT, text, recipient=to))
        return "ok"

    async def history(self, need_id: str | None = None) -> list[Message]:
        return list(self.chat)

    async def find(self, query: str) -> list:
        # No partner ASSTs in these tests: the Index returns nobody.
        return []

    def sent_to(self, to: str) -> list[str]:
        return [text for target, text in self.attempts if target == to and target not in self.down]


def slot(sid: str, doc: str, at: str, day: str, facility: str = HOSP) -> str:
    return (
        f"slot_id={sid} doctor_id=ASST-1-{doc} facility={facility} procedure=VIS-FIS "
        f"at={at} day={day} free=1"
    )


DEFAULT_SLOTS = [
    slot("S-1", "h1", "2026-11-23T14:00:00+01:00", "mon"),
    slot("S-2", "h1", "2026-11-23T15:00:00+01:00", "mon"),
    slot("S-3", "h1", "2026-11-24T09:00:00+01:00", "tue"),
    slot("S-4", "h3", "2026-11-24T10:00:00+01:00", "tue"),
    slot("S-5", "h2", "2026-11-25T09:00:00+01:00", "wed"),
    slot("S-6", "h1", "2026-11-25T10:00:00+01:00", "wed"),
    slot("S-7", "h2", "2026-11-26T09:00:00+01:00", "thu"),
    slot("S-8", "h3", "2026-11-26T11:00:00+01:00", "thu"),
]


def booking_chat(
    *,
    rx_reply: str | None = None,
    slots: list[str] | None = None,
    number: str = RX,
    citizen_extra: str = "",
) -> list[Message]:
    """A case where the Ledger already answered prescription, agenda and world."""

    rx_reply = rx_reply or f"prescription prescription_number={number} procedure=VIS-FIS priority=D state=issued"
    world = "slots procedure=VIS-FIS page=1/1\n" + "\n".join(slots or DEFAULT_SLOTS)
    return [
        msg(CIT, f"need.request prescription_number={number} lang=it {citizen_extra}".strip(), AGENT),
        msg(AGENT, f"prescription prescription_number={number}", LEDGER),
        msg(AGENT, "agenda", LEDGER),
        msg(AGENT, "rules", LEDGER),
        msg(LEDGER, rx_reply, AGENT),
        msg(LEDGER, f"agenda agenda_now={NOW}", AGENT),
        msg(AGENT, "world procedure=VIS-FIS page=1", LEDGER),
        msg(LEDGER, world, AGENT),
    ]


def offer_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("offer ")]


async def run_advance(city: FakeCity, *, replacing: bool = False) -> None:
    await bc._advance_alone(city, city.inbound, list(city.chat), NEED, fixed=None, replacing=replacing)


class OneOfferMessage(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_memory()

    def test_offers_in_parses_each_line(self) -> None:
        text = "\n".join(
            [
                "offer offer_id=of-S-1 slot_id=S-1 doctor_id=ASST-1-h1",
                "offer offer_id=of-S-2 slot_id=S-2 doctor_id=ASST-1-h2",
                "offer offer_id=of-S-3 slot_id=S-3 doctor_id=ASST-1-h3",
                "Ecco gli appuntamenti disponibili.",
            ]
        )
        self.assertEqual([o["slot_id"] for o in bc._offers_in(text)], ["S-1", "S-2", "S-3"])

    def test_our_offer_finds_third_line(self) -> None:
        text = "\n".join(f"offer offer_id=of-S-{i} slot_id=S-{i} facility={HOSP}" for i in (1, 2, 3))
        chat = [msg(AGENT, text, CIT)]
        self.assertEqual(bc._our_offer(chat, AGENT, "of-S-3")["slot_id"], "S-3")

    def test_pick_spreads_doctors_and_days(self) -> None:
        slots = [bc.fields_of(s) for s in [
            slot("A", "h1", "2026-11-23T14:00:00+01:00", "mon"),
            slot("B", "h1", "2026-11-23T15:00:00+01:00", "mon"),
            slot("C", "h1", "2026-11-23T16:00:00+01:00", "mon"),
            slot("D", "h1", "2026-11-23T17:00:00+01:00", "mon"),
            slot("E", "h3", "2026-11-24T10:00:00+01:00", "tue"),
            slot("F", "h2", "2026-11-25T09:00:00+01:00", "wed"),
        ]]
        picked = bc._pick(slots, set())
        self.assertEqual(len({s["doctor_id"] for s in picked[:3]}), 3)

    def test_pick_prefers_citizen_days(self) -> None:
        slots = [bc.fields_of(s) for s in DEFAULT_SLOTS]
        self.assertEqual(bc._pick(slots, {"wed"})[0]["day"], "wed")

    def test_days_normalised(self) -> None:
        chat = [msg(CIT, "need.request prescription_number=x lang=it days=Mercoledì/FRI", AGENT)]
        self.assertEqual(bc._days(chat), {"wed", "fri"})
        junk = [msg(CIT, "need.request days=xyz,,/", AGENT)]
        self.assertEqual(bc._days(junk), set())

    def test_offer_block_under_2000_chars(self) -> None:
        many = [
            bc.fields_of(slot(f"S-LONG-IDENTIFIER-{i:03d}", "h1", "2026-11-24T09:00:00+01:00", "tue"))
            for i in range(10)
        ]
        text = bc._offer_block(many, RX, "D", "it")
        self.assertLessEqual(len(text), 2000)
        self.assertLessEqual(len(offer_lines(text)), 6)
        self.assertGreater(len(offer_lines(text)), 0)

    async def test_advance_sends_one_message_with_all_slots(self) -> None:
        city = FakeCity(booking_chat())
        await run_advance(city)
        to_citizen = city.sent_to(CIT)
        self.assertEqual(len(to_citizen), 1)
        self.assertEqual(len(offer_lines(to_citizen[0])), 6)

    async def test_confirm_on_third_offer_forwards_right_slot(self) -> None:
        city = FakeCity(booking_chat())
        await run_advance(city)
        third = bc.fields_of(offer_lines(city.sent_to(CIT)[0])[2])
        confirm = msg(CIT, f"confirm offer_id={third['offer_id']} consent_ref=c-1", AGENT)
        city.chat.append(confirm)
        city.inbound = confirm
        await bc._desk(city, confirm, list(city.chat), hold_self=False, fixed=None)
        forwarded = [t for t in city.sent_to(HOSP) if t.startswith("confirm ")]
        self.assertEqual(len(forwarded), 1)
        self.assertIn(f"slot_id={third['slot_id']}", forwarded[0].split())


NEW = "prescription_number-NEW1"


def renewals(texts: list[str]) -> list[str]:
    return [t for t in texts if "data.request" in t.split() and "purpose=prescription_renewal" in t.split()]


def offer_messages(texts: list[str]) -> list[str]:
    return [t for t in texts if offer_lines(t)]


class PrescriptionRenewal(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_memory()

    async def test_rules_sent_with_agenda(self) -> None:
        city = FakeCity([msg(CIT, f"need.request prescription_number={RX} lang=it", AGENT)])
        await run_advance(city)
        to_ledger = city.sent_to(LEDGER)
        self.assertIn("rules", to_ledger)
        self.assertIn("agenda", to_ledger)
        self.assertIn(f"prescription prescription_number={RX}", to_ledger)

    def test_rx_line_tied_to_number(self) -> None:
        chat = booking_chat() + [msg(CIT, f"need.request prescription_number={NEW} lang=it", AGENT)]
        self.assertIsNone(bc._rx_line(chat, AGENT, NEW))
        self.assertIn(f"prescription_number={RX}", bc._rx_line(chat, AGENT, RX).text.split())

    async def test_expired_state_asks_renewal_once_no_offers(self) -> None:
        city = FakeCity(booking_chat(rx_reply=f"prescription prescription_number={RX} procedure=VIS-FIS priority=D state=expired"))
        await run_advance(city)
        await run_advance(city)
        self.assertEqual(len(renewals(city.sent_to(CIT))), 1)
        self.assertEqual(offer_messages(city.sent_to(CIT)), [])

    async def test_expired_token_in_refusal(self) -> None:
        city = FakeCity(booking_chat(rx_reply=f"refused prescription prescription_number={RX} reason=PRESCRIPTION_EXPIRED"))
        await run_advance(city)
        self.assertEqual(len(renewals(city.sent_to(CIT))), 1)
        self.assertEqual(offer_messages(city.sent_to(CIT)), [])

    async def test_p1_rules_and_issue_date(self) -> None:
        chat = booking_chat(rx_reply=f"prescription prescription_number={RX} procedure=VIS-FIS priority=D state=issued issued_at=2026-11-18T09:00:00+01:00")
        chat.insert(-1, msg(LEDGER, "rules rule_id=P1 version=1 effective_agenda=2026-11-18T08:00:00+01:00", AGENT))
        city = FakeCity(chat)
        await run_advance(city)
        self.assertEqual(len(renewals(city.sent_to(CIT))), 1)
        self.assertEqual(offer_messages(city.sent_to(CIT)), [])

    async def test_p1_issue_date_skips_weekend(self) -> None:
        # Issued Friday 09:00, now Monday 08:00: under one agenda day, the agenda has no weekends.
        chat = booking_chat(rx_reply=f"prescription prescription_number={RX} procedure=VIS-FIS priority=D state=issued issued_at=2026-11-20T09:00:00+01:00")
        chat.insert(-1, msg(LEDGER, "rules rule_id=P1 version=1 effective_agenda=2026-11-18T08:00:00+01:00", AGENT))
        city = FakeCity(chat)
        await run_advance(city)
        self.assertEqual(renewals(city.sent_to(CIT)), [])
        self.assertEqual(len(offer_messages(city.sent_to(CIT))), 1)

    async def test_renewed_number_gets_fresh_offer(self) -> None:
        city = FakeCity(booking_chat())
        await run_advance(city)  # six offers for the old number
        city.chat += [
            msg(CIT, f"need.request prescription_number={NEW} lang=it", AGENT),
            msg(AGENT, f"prescription prescription_number={NEW}", LEDGER),
            msg(LEDGER, f"prescription prescription_number={NEW} procedure=VIS-FIS priority=D state=issued", AGENT),
        ]
        city.inbound = city.chat[-1]
        await run_advance(city)
        fresh = offer_messages(city.sent_to(CIT))[-1]
        self.assertEqual(len(offer_lines(fresh)), 6)
        self.assertTrue(all(f"prescription_number={NEW}" in line.split() for line in offer_lines(fresh)))

    async def test_partner_cancel_expired_asks_renewal(self) -> None:
        city = FakeCity(booking_chat())
        await run_advance(city)
        cancel = msg(HOSP, "cancel offer_id=of-S-1 reason=PRESCRIPTION_EXPIRED", AGENT)
        city.chat.append(cancel)
        city.inbound = cancel
        await bc._desk(city, cancel, list(city.chat), hold_self=False, fixed=None)
        to_citizen = city.sent_to(CIT)
        cancels = [t for t in to_citizen if t.startswith("cancel ")]
        self.assertEqual(len(cancels), 1)
        self.assertIn("reason=prescription_expired", cancels[0].split())
        self.assertEqual(len(renewals(to_citizen)), 1)
        self.assertEqual(len(offer_messages(to_citizen)), 1)  # only the original offer


class FacilityDown(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_memory()

    async def confirm_first_hosp_offer(self, city: FakeCity) -> dict[str, str]:
        await run_advance(city)
        offers = [bc.fields_of(line) for line in offer_lines(city.sent_to(CIT)[0])]
        target = next(o for o in offers if o["facility"] == HOSP)
        city.down = {HOSP}
        confirm = msg(CIT, f"confirm offer_id={target['offer_id']} consent_ref=c-1", AGENT)
        city.chat.append(confirm)
        city.inbound = confirm
        await bc._desk(city, confirm, list(city.chat), hold_self=False, fixed=None)
        return target

    async def test_forward_failure_cancels_and_reoffers_other_facility(self) -> None:
        slots = [
            slot("S-1", "h1", "2026-11-23T14:00:00+01:00", "mon"),
            slot("S-3", "h1", "2026-11-24T09:00:00+01:00", "tue"),
            slot("H2-1", "h2", "2026-11-25T09:00:00+01:00", "wed", facility=HOSP2),
            slot("H2-2", "h3", "2026-11-26T11:00:00+01:00", "thu", facility=HOSP2),
        ]
        city = FakeCity(booking_chat(slots=slots))
        target = await self.confirm_first_hosp_offer(city)
        self.assertEqual(sum(1 for to, _ in city.attempts if to == HOSP), 1)
        to_citizen = city.sent_to(CIT)
        cancels = [t for t in to_citizen if t.startswith("cancel ")]
        self.assertEqual(len(cancels), 1)
        self.assertIn(f"offer_id={target['offer_id']}", cancels[0].split())
        self.assertIn("reason=facility_unavailable", cancels[0].split())
        again = offer_messages(to_citizen)[-1]
        self.assertGreater(len(offer_lines(again)), 0)
        self.assertTrue(all(bc.fields_of(line)["facility"] == HOSP2 for line in offer_lines(again)))

    async def test_forward_failure_no_alternative_says_none(self) -> None:
        city = FakeCity(booking_chat())
        await self.confirm_first_hosp_offer(city)
        self.assertEqual(sum(1 for to, _ in city.attempts if to == HOSP), 1)
        to_citizen = city.sent_to(CIT)
        self.assertEqual(len([t for t in to_citizen if t.startswith("cancel ")]), 1)
        self.assertEqual(len(offer_messages(to_citizen)), 1)  # only the original offer
        self.assertTrue(any("non ci sono altri posti" in t for t in to_citizen))


if __name__ == "__main__":
    unittest.main()
