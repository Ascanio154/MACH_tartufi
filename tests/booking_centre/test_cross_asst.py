"""Booking centre cross-ASST booking: partner search, partner offers, cross_asst consent.

Run from the repo root with the city-cli Python (it ships city_sdk):
    ~/.local/share/uv/tools/city-cli/bin/python -m unittest tests.booking_centre.test_cross_asst -v
"""

from __future__ import annotations

import unittest

from city_sdk.city import CapError, Message, Peer

from tests.booking_centre.test_quick_wins import (
    AGENT,
    CIT,
    HOSP,
    NEED,
    RX,
    FakeCity,
    bc,
    booking_chat,
    msg,
    reset_memory,
    slot,
)

PX1, PX2, PX3, PX4 = "agent:px1", "agent:px2", "agent:px3", "agent:px4"
PAST_ONLY = [slot("S-OLD", "h1", "2026-11-20T09:00:00+01:00", "fri")]


def peers() -> list[Peer]:
    phys = ("hospital.physiatry",)
    return [
        Peer(agent_id=HOSP, district=7, role="facility", capabilities=phys),
        Peer(agent_id=PX1, district=3, role="facility", capabilities=phys),
        Peer(agent_id=PX2, district=7, role="facility", capabilities=phys),
        Peer(agent_id=PX3, district=9, role="facility", capabilities=phys),
        Peer(agent_id=PX4, district=7, role="facility", capabilities=phys),
        Peer(agent_id="agent:card", district=7, role="facility",
             capabilities=("specialist_care.cardiology",)),
    ]


class PartnerCity(FakeCity):
    """FakeCity plus an Index: ``find`` returns ``peers`` or raises like the platform."""

    def __init__(
        self,
        chat: list[Message],
        *,
        peers: list[Peer] | None = None,
        find_error: bool = False,
        down: set[str] | None = None,
    ) -> None:
        super().__init__(chat, down=down)
        self.peers = peers if peers is not None else []
        self.find_error = find_error
        self.queries: list[str] = []

    async def find(self, query: str) -> list[Peer]:
        self.queries.append(query)
        if self.find_error:
            raise CapError("UNAVAILABLE", "index")
        return list(self.peers)


def requests(city: PartnerCity) -> list[tuple[str, str]]:
    return [(to, text) for to, text in city.attempts if "need.request" in text.split()]


def keys(text: str) -> set[str]:
    return {token.partition("=")[0] for token in text.split() if "=" in token}


async def advance(city: PartnerCity, message: Message | None = None) -> None:
    inbound = message or city.inbound
    await bc._advance_alone(city, inbound, city.chat, NEED, fixed=None, replacing=False)


class _Fresh(unittest.IsolatedAsyncioTestCase):
    """The brain remembers sent offers per need in module globals; every test starts clean."""

    def setUp(self) -> None:
        reset_memory()


class AskPartners(_Fresh):
    async def test_no_local_slot_asks_partners(self) -> None:
        city = PartnerCity(booking_chat(slots=PAST_ONLY), peers=peers())
        await advance(city)
        sent = requests(city)
        self.assertEqual(len(sent), 3)
        targets = {to for to, _ in sent}
        self.assertNotIn(HOSP, targets)
        self.assertTrue(targets <= {PX1, PX2, PX3, PX4})
        for _, text in sent:
            self.assertEqual(keys(text), {"service_type", "procedure", "priority", "request_id"})
            self.assertIn("service_type=hospital.physiatry", text.split())
            self.assertIn("procedure=VIS-FIS", text.split())
            self.assertIn("priority=D", text.split())
            self.assertIn(f"request_id=x-{NEED}", text.split())

    async def test_district_first(self) -> None:
        chat = booking_chat(slots=PAST_ONLY, citizen_extra="area.district=7")
        city = PartnerCity(chat, peers=peers())
        await advance(city)
        targets = {to for to, _ in requests(city)}
        self.assertIn(PX2, targets)
        self.assertIn(PX4, targets)

    async def test_local_slots_no_partner_request(self) -> None:
        city = PartnerCity(booking_chat(), peers=peers())
        await advance(city)
        self.assertEqual(requests(city), [])
        self.assertEqual(city.queries, [])

    async def test_partners_asked_once(self) -> None:
        city = PartnerCity(booking_chat(slots=PAST_ONLY), peers=peers())
        await advance(city)
        await advance(city)
        self.assertEqual(len(requests(city)), 3)

    async def test_find_error_no_crash(self) -> None:
        city = PartnerCity(booking_chat(slots=PAST_ONLY), peers=peers(), find_error=True)
        await advance(city)
        self.assertEqual(requests(city), [])

    async def test_district_garbage_ignored(self) -> None:
        chat = booking_chat(slots=PAST_ONLY, citizen_extra="area.district=abc")
        city = PartnerCity(chat, peers=peers())
        await advance(city)
        self.assertEqual(len(requests(city)), 3)

    async def test_peers_without_capabilities_still_asked(self) -> None:
        # find(capability) already filters by capability; a peer card may come back without them.
        bare = [Peer(agent_id=PX1, district=3, role="facility"),
                Peer(agent_id=HOSP, district=7, role="facility")]
        city = PartnerCity(booking_chat(slots=PAST_ONLY), peers=bare)
        await advance(city)
        self.assertEqual([to for to, _ in requests(city)], [PX1])

    async def test_citizen_told_once(self) -> None:
        city = PartnerCity(booking_chat(slots=PAST_ONLY), peers=peers())
        await advance(city)
        await advance(city)
        notices = [text for text in city.sent_to(CIT) if text == bc._PARTNER_ASK_IT]
        self.assertEqual(len(notices), 1)


def poffer(oid: str, sid: str, doc: str, at: str, day: str, *, proc: str = "VIS-FIS",
           facility: str = PX1) -> str:
    return (
        f"offer offer_id={oid} slot_id={sid} doctor_id=ASST-2-{doc} facility={facility} "
        f"procedure={proc} agenda_date={at} agenda_day={day} request_id=x-{NEED}"
    )


VALID_A = poffer("p-A", "A", "h1", "2026-11-24T09:00:00+01:00", "tue")
VALID_B = poffer("p-B", "B", "h2", "2026-11-25T10:30:00+01:00", "wed")
VALID_C = poffer("p-C", "C", "h1", "2026-11-24T14:30:00+01:00", "tue")


def offer_messages(city: PartnerCity) -> list[str]:
    return [text for text in city.sent_to(CIT)
            if "offer offer_id=" in text and "waiting_list=true" not in text.split()]


def offer_rows(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("offer ")]


async def asked_city(peer_list: list[Peer] | None = None) -> PartnerCity:
    """A case where our ASST had nothing and the partners were asked."""

    city = PartnerCity(booking_chat(slots=PAST_ONLY), peers=peer_list or peers())
    await advance(city)
    return city


async def partner_says(city: PartnerCity, sender: str, text: str) -> Message:
    message = msg(sender, text, AGENT)
    city.chat.append(message)
    city.inbound = message
    await bc._desk(city, message, city.chat, hold_self=False, fixed=None)
    return message


class PartnerOffers(_Fresh):
    async def test_partner_offer_forwarded(self) -> None:
        city = await asked_city()
        await partner_says(city, PX1, "\n".join([VALID_A, VALID_B, VALID_C]))
        sent = offer_messages(city)
        self.assertEqual(len(sent), 1)
        rows = offer_rows(sent[0])
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertIn(f"facility={PX1}", row.split())
            self.assertIn(f"prescription_number={RX}", row.split())
        ids = {token for row in rows for token in row.split() if token.startswith("offer_id=")}
        self.assertEqual(ids, {"offer_id=p-A", "offer_id=p-B", "offer_id=p-C"})

    async def test_partner_offers_filtered(self) -> None:
        city = await asked_city()
        past = poffer("p-P", "P", "h1", "2026-11-20T09:00:00+01:00", "fri")
        other = poffer("p-K", "K", "h1", "2026-11-24T10:00:00+01:00", "tue", proc="VIS-CARD")
        unknown = poffer("p-Z", "Z", "zz", "2026-11-24T11:00:00+01:00", "tue")
        await partner_says(city, PX1, "\n".join([past, other, unknown, VALID_A]))
        sent = offer_messages(city)
        self.assertEqual(len(sent), 1)
        rows = offer_rows(sent[0])
        self.assertEqual(len(rows), 1)
        self.assertIn("slot_id=A", rows[0].split())

    async def test_partner_offers_one_line(self) -> None:
        city = await asked_city()
        await partner_says(city, PX1, VALID_A + " " + VALID_B.removeprefix("offer "))
        rows = offer_rows(offer_messages(city)[0])
        self.assertEqual(len(rows), 2)

    async def test_second_partner_offer_not_resent(self) -> None:
        city = await asked_city()
        await partner_says(city, PX1, VALID_A)
        await partner_says(city, PX2, poffer("q-D", "D", "h1", "2026-11-24T10:30:00+01:00",
                                             "tue", facility=PX2))
        self.assertEqual(len(offer_messages(city)), 1)

    async def test_partner_offer_nothing_fits(self) -> None:
        city = await asked_city()
        await partner_says(city, PX1, poffer("p-P", "P", "h1", "2026-11-20T09:00:00+01:00", "fri"))
        self.assertEqual(offer_messages(city), [])


async def offered_city() -> PartnerCity:
    """Partners asked, PX1 answered, the citizen holds partner offers p-A and p-B."""

    city = await asked_city()
    await partner_says(city, PX1, "\n".join([VALID_A, VALID_B]))
    return city


async def citizen_says(city: PartnerCity, text: str) -> None:
    message = msg(CIT, text, AGENT)
    city.chat.append(message)
    city.inbound = message
    await bc._desk(city, message, city.chat, hold_self=False, fixed=None)


def confirms_to(city: PartnerCity, target: str) -> list[str]:
    return [text for to, text in city.attempts if to == target and text.startswith("confirm")]


class CrossConsent(_Fresh):
    async def test_partner_pick_asks_consent(self) -> None:
        city = await offered_city()
        await citizen_says(city, "confirm offer_id=p-A consent_ref=cons-booking")
        asks = [t for t in city.sent_to(CIT) if t.startswith("consent")]
        self.assertEqual(len(asks), 1)
        self.assertIn("purpose=cross_asst", asks[0].split())
        self.assertIn("offer_id=p-A", asks[0].split())
        self.assertEqual(confirms_to(city, PX1), [])

    async def test_consent_forwards_confirm(self) -> None:
        city = await offered_city()
        await citizen_says(city, "confirm offer_id=p-A consent_ref=cons-booking")
        await citizen_says(city, "consent consent_ref=cx-1 purpose=cross_asst")
        sent = confirms_to(city, PX1)
        self.assertEqual(len(sent), 1)
        tokens = sent[0].split()
        for token in ("consent_ref=cx-1", f"citizen={CIT}", f"prescription_number={RX}",
                      "slot_id=A", "offer_id=p-A"):
            self.assertIn(token, tokens)

    async def test_plain_no_cancels(self) -> None:
        city = await offered_city()
        await citizen_says(city, "confirm offer_id=p-A consent_ref=cons-booking")
        await citizen_says(city, "no grazie, preferisco di no")
        cancels = [t for t in city.sent_to(CIT) if t.startswith("cancel")]
        self.assertEqual(len(cancels), 1)
        self.assertIn("reason=consent_declined", cancels[0].split())
        self.assertIn("offer_id=p-A", cancels[0].split())
        self.assertEqual(confirms_to(city, PX1), [])

    async def test_resend_is_not_a_decline(self) -> None:
        city = await offered_city()
        await citizen_says(city, "confirm offer_id=p-A consent_ref=cons-booking")
        await citizen_says(city, f"need.request prescription_number={RX} lang=it")
        cancels = [t for t in city.sent_to(CIT) if "reason=consent_declined" in t.split()]
        self.assertEqual(cancels, [])

    async def test_local_pick_unchanged(self) -> None:
        city = PartnerCity(booking_chat(), peers=peers())
        await advance(city)
        first = next(t for t in city.sent_to(CIT) if "offer offer_id=" in t)
        offer_id = next(tok for tok in first.split() if tok.startswith("offer_id=")).partition("=")[2]
        await citizen_says(city, f"confirm offer_id={offer_id} consent_ref=cons-booking")
        self.assertEqual(len(confirms_to(city, HOSP)), 1)
        self.assertEqual([t for t in city.sent_to(CIT) if t.startswith("consent")], [])

    async def test_partner_down_asks_others(self) -> None:
        city = await offered_city()
        await citizen_says(city, "confirm offer_id=p-A consent_ref=cons-booking")
        city.down = {PX1}
        await citizen_says(city, "consent consent_ref=cx-1 purpose=cross_asst")
        cancels = [t for t in city.sent_to(CIT) if t.startswith("cancel")]
        self.assertTrue(any("reason=facility_unavailable" in t.split() for t in cancels))
        self.assertTrue(any(to == PX4 for to, _ in requests(city)))
        self.assertEqual(len(confirms_to(city, PX1)), 1)
        self.assertEqual(sum(1 for to, _ in requests(city) if to == PX1), 1)


def closing(city: PartnerCity) -> list[str]:
    return [t for t in city.sent_to(CIT) if "vicine" in t
            and (not t.startswith("offer") or "waiting_list=true" in t.split())
            and "sto chiedendo" not in t]


def lang_chat(lang: str, slots: list[str] | None = None) -> list[Message]:
    chat = booking_chat(slots=slots)
    chat[0] = msg(CIT, f"need.request prescription_number={RX} lang={lang}", AGENT)
    return chat


class NextSteps(_Fresh):
    async def test_all_partners_no_availability_closes(self) -> None:
        city = await asked_city()
        asked = [to for to, _ in requests(city)]
        # Partners that stay silent in the ask turn: the citizen hears the outcome at once
        # (journey 113), and later "no" answers never repeat it.
        self.assertEqual(len(closing(city)), 1)
        for peer in asked:
            await partner_says(city, peer, f"cancel request_id=x-{NEED} reason=no_availability")
        self.assertEqual(len(closing(city)), 1)

    async def test_citizen_resend_after_silent_search_closes(self) -> None:
        city = await asked_city()
        await citizen_says(city, f"need.request prescription_number={RX} lang=it ci siete?")
        self.assertEqual(len(closing(city)), 1)

    async def test_our_rare_language_only_si(self) -> None:
        self.assertTrue(bc._speaks("ASST-1-h3", "si"))
        self.assertFalse(bc._speaks("ASST-1-h3", "ka"))

    async def test_rare_language_citizen_asks_more_partners(self) -> None:
        many = peers() + [Peer(agent_id=f"agent:pz{i}", role="facility",
                               capabilities=("hospital.physiatry",)) for i in range(4)]
        city = PartnerCity(lang_chat("ka", PAST_ONLY), peers=many)
        await advance(city)
        # Level 4: one ASST's h3 speaks each rare language, so every partner is asked.
        self.assertEqual(len(requests(city)), 8)

    async def test_partner_doctor_lang_used(self) -> None:
        city = PartnerCity(lang_chat("ka", PAST_ONLY), peers=peers())
        await advance(city)
        ka = VALID_A.replace("ASST-2-h1", "ASST-2-h3") + " doctor_lang=it/ka doctor_gender=m"
        si = (poffer("p-S", "S", "h3", "2026-11-24T10:30:00+01:00", "tue")
              + " doctor_lang=it/si doctor_gender=m")
        bare = poffer("p-U", "U", "h3", "2026-11-26T10:30:00+01:00", "thu")
        await partner_says(city, PX1, "\n".join([ka, si, bare]))
        rows = offer_rows(offer_messages(city)[0])
        self.assertEqual([r for r in rows if "slot_id=A" in r.split()] != [], True)
        self.assertEqual(len(rows), 1)


def without_our_rows(city: PartnerCity) -> None:
    """Journey 000045: our own sent messages are not found in the history."""

    city.chat[:] = [row for row in city.chat if row.sender != AGENT]


class ConfirmWithoutOwnHistory(_Fresh):
    async def test_local_confirm_rebuilt_from_offer_id(self) -> None:
        city = PartnerCity(booking_chat(), peers=peers())
        await advance(city)
        without_our_rows(city)
        await citizen_says(city, "confirm consent_ref=cons-booking offer_id=of-S-3")
        sent = confirms_to(city, HOSP)
        self.assertEqual(len(sent), 1)
        for token in ("slot_id=S-3", f"citizen={CIT}", "consent_ref=cons-booking",
                      f"prescription_number={RX}"):
            self.assertIn(token, sent[0].split())

    async def test_partner_confirm_found_in_partner_message(self) -> None:
        city = await offered_city()
        without_our_rows(city)
        await citizen_says(city, "confirm offer_id=p-A consent_ref=cons-booking")
        asks = [t for t in city.sent_to(CIT) if t.startswith("consent")]
        self.assertEqual(len(asks), 1)
        without_our_rows(city)
        await citizen_says(city, "consent consent_ref=cx-1 purpose=cross_asst")
        sent = confirms_to(city, PX1)
        self.assertEqual(len(sent), 1)
        self.assertIn("consent_ref=cx-1", sent[0].split())
        self.assertIn("slot_id=A", sent[0].split())


class ReceiptOnce(_Fresh):
    async def test_receipt_forwarded_once_without_own_history(self) -> None:
        # Journey 000098: the citizen got the same booking.receipt twice in the same second.
        city = PartnerCity(booking_chat(), peers=peers())
        receipt = "booking.receipt booking_id=bk-1 ledger_ref=lr-1"
        await partner_says(city, HOSP, receipt)
        without_our_rows(city)
        await partner_says(city, HOSP, receipt)
        sent = [t for t in city.sent_to(CIT) if t.startswith("booking.receipt")]
        self.assertEqual(len(sent), 1)


class Level4Live(_Fresh):
    """Fixes from the level 4 live logs (journeys 106, 113, 118, 141)."""

    async def test_nothing_anywhere_gets_waiting_list(self) -> None:
        city = await asked_city()
        waits = [t for t in city.sent_to(CIT) if "waiting_list=true" in t.split()]
        self.assertEqual(len(waits), 1)
        self.assertIn("lista d'attesa", waits[0])
        self.assertEqual(bc.understood(msg(AGENT, waits[0], CIT)).type, "offer")

    async def test_waiting_list_pick_is_acknowledged(self) -> None:
        city = await asked_city()
        await citizen_says(city, f"confirm offer_id=wl-{NEED} consent_ref=cons-booking")
        self.assertIn("lista d'attesa", city.sent_to(CIT)[-1])
        self.assertEqual(city.sent_to(HOSP), [])

    async def test_partner_refusal_asks_new_partners(self) -> None:
        city = await asked_city()
        first = {to for to, _ in requests(city)}
        await partner_says(city, PX1, VALID_A)
        await citizen_says(city, "confirm offer_id=p-A consent_ref=cons-booking")
        await citizen_says(city, "consent consent_ref=cx-1 purpose=cross_asst")
        await partner_says(city, PX1, "cancel offer_id=p-A reason=facility_cancelled")
        later = {to for to, _ in requests(city)} - first
        self.assertTrue(later, requests(city))
        self.assertNotIn(PX1, later)

    async def test_partners_without_the_language_close_kindly(self) -> None:
        city = PartnerCity(lang_chat("el", PAST_ONLY), peers=peers())
        await advance(city)
        before = len([t for t in city.sent_to(CIT) if "waiting_list=true" in t.split()])
        for peer in {to for to, _ in requests(city)}:
            await partner_says(city, peer, poffer("p-X", "X", "h1", "2026-11-24T09:00:00+01:00",
                                                  "tue", facility=peer)
                               + " doctor_lang=it/en doctor_gender=f")
        waits = [t for t in city.sent_to(CIT) if "waiting_list=true" in t.split()]
        self.assertEqual(len(waits), 1)
        self.assertLessEqual(before, 1)

    async def test_receipt_from_unknown_agent_not_forwarded(self) -> None:
        city = await asked_city()
        await partner_says(city, "agent:impostor", "booking.receipt booking_id=bk-x ledger_ref=lr-x")
        self.assertEqual([t for t in city.sent_to(CIT) if t.startswith("booking.receipt")], [])

    async def test_partner_false_doctor_language_dropped(self) -> None:
        city = PartnerCity(lang_chat("en", PAST_ONLY), peers=peers())
        await advance(city)
        lie = VALID_B + " doctor_lang=it/en doctor_gender=m"  # h2 speaks only Italian
        await partner_says(city, PX1, lie)
        self.assertEqual(offer_messages(city), [])

    async def test_confirm_from_unknown_agent_not_forwarded(self) -> None:
        city = PartnerCity(booking_chat(), peers=peers())
        await advance(city)
        await partner_says(city, "agent:impostor",
                           f"confirm offer_id=of-S-3 slot_id=S-3 citizen={CIT} consent_ref=c-1")
        self.assertEqual(confirms_to(city, HOSP), [])
        self.assertTrue(any("reason=hold_refused" in t for t in city.sent_to("agent:impostor")))

    async def test_rare_language_asks_its_asst_first(self) -> None:
        georgian = "agent:h6r1v45esk77"  # Giambellino's hospital (MACH-1 table)
        many = peers() + [Peer(agent_id=georgian, role="facility",
                               capabilities=("hospital.physiatry",))]
        city = PartnerCity(lang_chat("ka", PAST_ONLY), peers=many)
        await advance(city)
        self.assertEqual(requests(city)[0][0], georgian)

    async def test_ring_reasons_only(self) -> None:
        self.assertEqual(bc._ring_reason("facility_cancelled"), "hold_refused")
        self.assertEqual(bc._ring_reason("CONSENT_MISSING"), "rule_p2_no_consent")
        self.assertEqual(bc._ring_reason("no_availability"), "no_availability")


if __name__ == "__main__":
    unittest.main()
