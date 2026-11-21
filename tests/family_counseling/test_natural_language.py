"""Consultorio, level 3: plain-language choice, preferences and arrival (check-in).

Run from the repo root with the city-cli Python (it ships city_sdk):
    ~/.local/share/uv/tools/city-cli/bin/python -m unittest tests.family_counseling.test_natural_language -v
"""

from __future__ import annotations

import importlib.util
import itertools
import unittest
from pathlib import Path

from city_sdk.city import LEDGER, Message

_BRAIN = Path(__file__).resolve().parents[2] / "asst" / "family_counseling" / "agent" / "agent.py"
_spec = importlib.util.spec_from_file_location("family_brain", _BRAIN)
fc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fc)
fc._PULL_PAUSE = 0

AGENT = "agent:fc"
CIT = "citizen:c9"
NEED = "N-2026-11-23-0042"
NOW = "2026-11-23T08:00:00+01:00"  # Monday
_ids = itertools.count(1)
SLOTS = [
    ("G-1", "c1", "2026-11-23T14:00:00+01:00", "mon"),
    ("G-2", "c1", "2026-11-24T10:00:00+01:00", "tue"),
    ("G-3", "c1", "2026-11-25T11:00:00+01:00", "wed"),
]


def msg(sender: str, text: str, recipient: str = "") -> Message:
    return Message(type="chat", sender=sender, msg_id=f"f-{next(_ids)}", need_id=NEED,
                   body={"text": text}, recipient=recipient)


class LedgerCity:
    """A mailbox whose Ledger answers at once, so every write's reply is in history."""

    def __init__(self) -> None:
        self.agent_id = AGENT
        self.address = ""
        self.inbound: Message | None = None
        self.chat: list[Message] = []
        self.holds = 0

    async def say(self, to: str, text: str, *, status: str | None = None) -> str:
        sent = msg(AGENT, text, to)
        self.chat.append(sent)
        if to == LEDGER:
            self.chat.append(msg(LEDGER, self._answer(text), AGENT))
        return sent.msg_id

    def _answer(self, text: str) -> str:
        head = text.split()[0]
        fields = fc.fields_of(text)
        if head == "agenda":
            return f"agenda agenda_now={NOW}"
        if head == "rules":
            return "rules none"
        if head == "world":
            lines = [f"slot_id={sid} doctor_id=ASST-1-{doc} facility={AGENT} procedure=CONS-GIN "
                     f"at={at} day={day} free=1" for sid, doc, at, day in SLOTS]
            return "slots procedure=CONS-GIN page=1/1\n" + "\n".join(lines)
        if head == "hold":
            self.holds += 1
            return f"held hold_id=H-{self.holds} slot_id={fields['slot_id']} fence=1"
        if head == "commit":
            return f"committed booking_id=B-77 ledger_ref=L-77 hold_id={fields['hold_id']}"
        if head == "checkin":
            return f"checked_in booking_id={fields['booking_id']}"
        return "ok"

    async def history(self, peer: str | None = None, *, need_id: str | None = None) -> list[Message]:
        return list(self.chat)

    async def find(self, query: str) -> list:
        return []

    def to_citizen(self) -> list[str]:
        return [row.text for row in self.chat if row.sender == AGENT and row.recipient == CIT]

    def to_ledger(self, head: str) -> list[str]:
        return [row.text for row in self.chat
                if row.sender == AGENT and row.recipient == LEDGER and row.text.startswith(head)]


async def citizen_says(city: LedgerCity, text: str) -> None:
    message = msg(CIT, text, AGENT)
    city.chat.append(message)
    city.inbound = message
    await fc._family(city, message, list(city.chat))


class PlainConsultorio(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        fc._FAMILY_CASES.clear()

    async def _offered(self, first: str = "Buongiorno, vorrei un appuntamento al consultorio") -> LedgerCity:
        city = LedgerCity()
        await citizen_says(city, first)
        self.assertTrue(any(fc._offer_rows(text) for text in city.to_citizen()), city.to_citizen())
        return city

    def test_reads_plain_preferences(self) -> None:
        rows = [msg(CIT, "Hi, I would like a visit, only on Wednesday, with a female doctor please")]
        prefs = fc._family_preferences(rows, CIT)
        self.assertEqual(prefs["lang"], "en")
        self.assertEqual(prefs["days"], "wed")
        self.assertEqual(prefs["gender"], "f")

    def test_plain_arrival_reads_as_checkin(self) -> None:
        self.assertEqual(fc._family_head(msg(CIT, "Sono arrivata, prenotazione B-77")), "checkin")
        self.assertNotEqual(fc._family_head(msg(CIT, "vorrei prenotare una visita")), "checkin")

    async def test_plain_day_preference_goes_first(self) -> None:
        city = await self._offered("Buongiorno, vorrei una visita, meglio mercoledì")
        offers = [o for text in city.to_citizen() for o in fc._offer_rows(text)]
        self.assertEqual(offers[0]["slot_id"], "G-3")

    async def test_plain_choice_then_consent_books_then_plain_arrival_checks_in(self) -> None:
        city = await self._offered()
        await citizen_says(city, "va bene la seconda")
        self.assertEqual(city.to_ledger("hold"), [])
        self.assertTrue(city.to_citizen()[-1].startswith("consent purpose=booking"))
        await citizen_says(city, "consent consent_ref=cons-9 purpose=booking")
        holds = city.to_ledger("hold")
        self.assertEqual(len(holds), 1)
        self.assertIn("slot_id=G-2", holds[0].split())
        self.assertTrue(any(t.startswith("booking.receipt booking_id=B-77") for t in city.to_citizen()))
        await citizen_says(city, "Buongiorno, sono arrivata in accettazione")
        self.assertEqual(city.to_ledger("checkin"), [f"checkin booking_id=B-77 citizen={CIT}"])
        self.assertTrue(city.to_citizen()[-1].startswith("checkin booking_id=B-77"))

    async def test_choice_with_consent_in_one_sentence(self) -> None:
        city = await self._offered()
        await citizen_says(city, "prendo la prima, consent_ref=cons-1")
        holds = city.to_ledger("hold")
        self.assertEqual(len(holds), 1)
        self.assertIn("slot_id=G-1", holds[0].split())


class MoreHumanAndRing(unittest.IsolatedAsyncioTestCase):
    """Proactive check-in, MACH-1 answers to other ASSTs, privacy of the request."""

    def setUp(self) -> None:
        fc._FAMILY_CASES.clear()
        fc._SOFT.clear()

    async def test_checkin_welcomes_with_next_steps(self) -> None:
        city = LedgerCity()
        await citizen_says(city, "Buongiorno, vorrei un appuntamento al consultorio")
        await citizen_says(city, "prendo la prima, consent_ref=cons-1")
        await citizen_says(city, "Buongiorno, sono arrivata in accettazione")
        last = city.to_citizen()[-1]
        self.assertTrue(last.startswith("checkin booking_id=B-77"))
        self.assertIn("sala d'attesa", last)
        self.assertIn("non serve la ricetta", last)
        self.assertEqual(fc.understood(msg(AGENT, last, CIT)).type, "checkin")

    async def test_partner_request_gets_one_offer_message(self) -> None:
        city = LedgerCity()
        ask = Message(type="chat", sender="agent:other-bc", msg_id="r-1", need_id="N-RING-1",
                      recipient=AGENT, body={"text": "need.request service_type=family_counseling"
                                             ".gynecology procedure=CONS-GIN request_id=rq-7"})
        city.chat.append(ask)
        await fc._family(city, ask, list(city.chat))
        sent = [row.text for row in city.chat if row.recipient == "agent:other-bc"]
        self.assertEqual(len(sent), 1)
        rows = [line for line in sent[0].splitlines() if line.startswith("offer ")]
        self.assertTrue(rows)
        for line in rows:
            self.assertIn("request_id=rq-7", line.split())
            self.assertNotIn("citizen", line)

    async def test_partner_hold_uses_cross_asst_consent(self) -> None:
        city = LedgerCity()
        confirm = Message(type="chat", sender="agent:other-bc", msg_id="r-2", need_id="N-RING-2",
                          recipient=AGENT, body={"text": "confirm offer_id=p-G-2 slot_id=G-2 "
                                                 "citizen=citizen:x1 consent_ref=c-book "
                                                 "cross_asst_consent_ref=cx-9"})
        city.chat.append(confirm)
        await fc._family(city, confirm, list(city.chat))
        holds = city.to_ledger("hold")
        self.assertEqual(len(holds), 1)
        self.assertIn("consent_ref=cx-9", holds[0].split())

    async def _ring(self, city: LedgerCity, text: str, msg_id: str = "r-9") -> list[str]:
        ask = Message(type="chat", sender="agent:other-bc", msg_id=msg_id, need_id="N-RING-9",
                      recipient=AGENT, body={"text": "need.request service_type=family_counseling"
                                             ".gynecology procedure=CONS-GIN " + text})
        city.chat.append(ask)
        await fc._family(city, ask, list(city.chat))
        return [row.text for row in city.chat if row.recipient == "agent:other-bc"]

    async def test_ring_days_respected_or_relaxed(self) -> None:
        city = LedgerCity()
        sent = await self._ring(city, "request_id=rq-d days=tue")
        offers = [line for line in sent[0].splitlines() if line.startswith("offer ")]
        self.assertEqual([fc.fields_of(line)["slot_id"] for line in offers], ["G-2"])
        city = LedgerCity()
        sent = await self._ring(city, "request_id=rq-t days=thu/fri")
        lines = sent[0].splitlines()
        self.assertTrue(lines[0].startswith("cancel request_id=rq-t reason=no_availability"))
        offers = [line for line in lines if line.startswith("offer ")]
        self.assertTrue(1 <= len(offers) <= 3)
        for line in offers:
            self.assertIn("relaxed=days", line.split())

    async def test_ring_gender_and_window(self) -> None:
        city = LedgerCity()
        sent = await self._ring(city, "request_id=rq-g gender_preference=m "
                                      "after=2026-11-24T00:00:00+01:00")
        lines = sent[0].splitlines()
        self.assertTrue(lines[0].startswith("cancel "))
        relaxed = {fc.fields_of(line)["slot_id"]: fc.fields_of(line)["relaxed"]
                   for line in lines if line.startswith("offer ")}
        self.assertEqual(relaxed.get("G-2"), "gender_preference")
        self.assertNotIn("G-1", relaxed)  # breaks two criteria: never offered

    async def test_ring_same_request_same_answer(self) -> None:
        city = LedgerCity()
        await self._ring(city, "request_id=rq-r", msg_id="r-10")
        sent = await self._ring(city, "request_id=rq-r", msg_id="r-11")
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0], sent[1])
        sent = await self._ring(city, "request_id=rq-r", msg_id="r-11")
        self.assertEqual(len(sent), 2)  # the same message again: no third answer

    def test_partner_cancel_with_relaxed_offers_is_read_line_by_line(self) -> None:
        state = fc._FamilyCase()
        state.rows = [
            msg(AGENT, "need.request service_type=family_counseling.gynecology procedure=CONS-GIN "
                       "request_id=rq-x", "agent:p1"),
            Message(type="chat", sender="agent:p1", msg_id="p-1", need_id=NEED, recipient=AGENT,
                    body={"text": "cancel request_id=rq-x reason=no_availability\n"
                                  "offer offer_id=of-S31 slot_id=S31 doctor_id=asst-x-h1 facility=agent:p1 "
                                  "procedure=CONS-GIN agenda_date=2026-11-26T11:00:00+01:00 "
                                  "request_id=rq-x relaxed=days\nGiovedì invece di martedì"}),
        ]
        catalog = list(fc._family_cross_catalog(state, AGENT).values())
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0]["slot_id"], "S31")
        self.assertEqual(catalog[0]["relaxed"], "days")

    async def test_citizen_gives_up_slot_booking_centre_told(self) -> None:
        city = LedgerCity()
        await citizen_says(city, "Buongiorno, vorrei un appuntamento al consultorio")
        offer = next(o for row in city.to_citizen() for o in fc._offer_rows(row))
        await citizen_says(city, f"cancel offer_id={offer['offer_id']} reason=other")
        told = [row.text for row in city.chat if row.recipient == fc._OUR_BOOKING_CENTRE]
        self.assertEqual(len(told), 1)
        self.assertIn(f"slot_id={offer['slot_id']}", told[0].split())
        self.assertNotIn("citizen", told[0])
        self.assertEqual(fc.first_word(told[0]), "notice")
        await citizen_says(city, f"cancel offer_id={offer['offer_id']} reason=other")
        told = [row.text for row in city.chat if row.recipient == fc._OUR_BOOKING_CENTRE]
        self.assertEqual(len(told), 1)  # once per slot


class VisibleCollaboration(unittest.IsolatedAsyncioTestCase):
    """The review reads only the citizen's messages: say what we did for them, only if done."""

    def setUp(self) -> None:
        fc._FAMILY_CASES.clear()

    async def test_receipt_tells_desk_and_citizen(self) -> None:
        city = LedgerCity()
        await citizen_says(city, "Buongiorno, vorrei un appuntamento al consultorio")
        await citizen_says(city, "prendo la prima, consent_ref=cons-1")
        receipt = next(t for t in city.to_citizen() if t.startswith("booking.receipt"))
        self.assertIn("tessera sanitaria", receipt)
        self.assertIn("Ho già avvisato anche il centro prenotazioni", receipt)
        told = [row.text for row in city.chat if row.recipient == fc._OUR_BOOKING_CENTRE]
        self.assertEqual(len(told), 1)
        self.assertTrue(told[0].startswith("notice slot_booked procedure=CONS-GIN slot_id=G-1"))
        self.assertNotIn("citizen", told[0])
        self.assertEqual(fc.understood(msg(AGENT, receipt, CIT)).type, "booking.receipt")

    def test_no_claim_when_the_notice_failed(self) -> None:
        self.assertNotIn("avvisato", fc._family_receipt_extras(False, False))
        self.assertIn("health card", fc._family_receipt_extras(False, True))
class Level7Contention(unittest.IsolatedAsyncioTestCase):
    """MACH-1.2 level 7: duplicates, soft lock, refusals with alternatives."""

    def setUp(self) -> None:
        fc._FAMILY_CASES.clear()
        fc._SOFT.clear()
        fc._RECEIPT_RESENT.clear()

    async def _ask(self, city: LedgerCity, sender: str, rid: str) -> str:
        ask = Message(type="chat", sender=sender, msg_id=f"q-{rid}", need_id=f"N-{rid}",
                      recipient=AGENT, body={"text": "need.request service_type=family_counseling"
                                             f".gynecology procedure=CONS-GIN request_id={rid}"})
        city.chat.append(ask)
        await fc._family(city, ask, list(city.chat))
        return [row.text for row in city.chat if row.recipient == sender][-1]

    async def test_soft_lock_keeps_offered_slots_for_their_request(self) -> None:
        city = LedgerCity()
        first = await self._ask(city, "agent:bc-a", "rq-a")
        second = await self._ask(city, "agent:bc-b", "rq-b")
        ids = lambda text: {fc.fields_of(l)["slot_id"] for l in text.splitlines() if l.startswith("offer ")}
        self.assertEqual(ids(first), {"G-1", "G-2", "G-3"})
        self.assertFalse(ids(first) & ids(second))  # nothing left: an honest no_availability
        self.assertTrue(second.startswith("cancel request_id=rq-b reason=no_availability"))

    async def test_duplicate_confirm_same_receipt_one_hold(self) -> None:
        city = LedgerCity()
        bc_a = "agent:bc-a"
        confirm = "confirm offer_id=p-G-1 slot_id=G-1 citizen=citizen:far consent_ref=c-1 " \
                  "cross_asst_consent_ref=cx-1"
        city.chat = [msg(AGENT, "offer offer_id=p-G-1 slot_id=G-1 request_id=rq-c", bc_a),
                     msg(bc_a, confirm, AGENT),
                     msg(AGENT, "hold slot_id=G-1 citizen=citizen:far consent_ref=cx-1", LEDGER),
                     msg(LEDGER, "held hold_id=H-1 slot_id=G-1 fence=1", AGENT),
                     msg(AGENT, "commit hold_id=H-1 consent_ref=cx-1 fence=1", LEDGER),
                     msg(LEDGER, "committed booking_id=B-77 ledger_ref=L-77 hold_id=H-1", AGENT),
                     msg(AGENT, "booking.receipt booking_id=B-77 ledger_ref=L-77", bc_a)]
        again = msg(bc_a, confirm, AGENT)
        city.chat.append(again)
        await fc._on_confirm(city, again, list(city.chat), hold_self=False)
        self.assertEqual(len(city.to_ledger("hold")), 1)  # never a second hold
        receipts = [row.text for row in city.chat if row.recipient == bc_a
                    and row.text.startswith("booking.receipt")]
        self.assertEqual(receipts, ["booking.receipt booking_id=B-77 ledger_ref=L-77"] * 2)
        await fc._on_confirm(city, again, list(city.chat), hold_self=False)
        self.assertEqual(len([row for row in city.chat if row.recipient == bc_a]), 3)

    def test_refused_partner_hold_is_slot_taken_with_alternatives(self) -> None:
        chat = [msg(AGENT, "agenda", LEDGER), msg(LEDGER, f"agenda agenda_now={NOW}", AGENT),
                msg(LEDGER, "slots procedure=CONS-GIN page=1/1\n" + "\n".join(
                    f"slot_id={sid} doctor_id=ASST-1-{doc} facility={AGENT} procedure=CONS-GIN "
                    f"at={at} day={day} free=1" for sid, doc, at, day in SLOTS), AGENT),
                msg(AGENT, "offer offer_id=p-G-1 slot_id=G-1 request_id=rq-x", "agent:bc-a")]
        text = fc._ring_refusal(chat, AGENT, "agent:bc-a", "p-G-1", "G-1", "SLOT_HELD")
        lines = text.splitlines()
        self.assertEqual(lines[0], "cancel offer_id=p-G-1 slot_id=G-1 reason=slot_taken request_id=rq-x")
        self.assertEqual([fc.fields_of(l)["slot_id"] for l in lines[1:]], ["G-2", "G-3"])
        consent = fc._ring_refusal(chat, AGENT, "agent:bc-a", "p-G-1", "G-1", "CONSENT_MISSING")
        self.assertEqual(consent.splitlines()[0].split()[3], "reason=consent_missing")


if __name__ == "__main__":
    unittest.main()
