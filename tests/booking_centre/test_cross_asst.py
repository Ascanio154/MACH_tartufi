"""Booking centre cross-ASST path: partner search, P2 consent, partner offers.

When our ASST has nothing that fits, the booking centre asks partner facilities found with
``city.find`` (search criteria only, no citizen data), asks the citizen for consent with
``data.request purpose=cross_asst``, and offers partner slots only after that consent.

Run from the repo root with the city-cli Python (it ships city_sdk):
    ~/.local/share/uv/tools/city-cli/bin/python -m unittest tests.booking_centre.test_cross_asst -v
"""

from __future__ import annotations

import unittest

from city_sdk.city import LEDGER, Peer

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
    offer_lines,
    slot,
)

P1 = "agent:partner1"
P2 = "agent:partner2"
OWN_HOSPITAL = "agent:xtj1dk9x3qej"  # our live hospital: never a partner
CROSS_REF = f"cons-{NEED}-cross_asst"

# Our world answered, but every slot is already in the past: nothing here fits.
PAST_SLOTS = [
    slot("S-1", "h1", "2026-11-20T09:00:00+01:00", "fri"),
    slot("S-2", "h2", "2026-11-20T10:00:00+01:00", "fri"),
]


def physiatry(agent_id: str) -> Peer:
    return Peer(agent_id=agent_id, role="facility", capabilities=("hospital.physiatry",))


class CrossCity(FakeCity):
    """FakeCity plus ``find``: returns the peers that list the queried capability."""

    def __init__(self, chat, peers=(), down=None) -> None:
        super().__init__(chat, down=down)
        self.peers = list(peers)
        self.queries: list[str] = []

    async def find(self, query: str) -> list[Peer]:
        self.queries.append(query)
        return [p for p in self.peers if query in p.capabilities or not p.capabilities]


def partner_offer(sender: str = P1, *, sep: str = "\n") -> object:
    lines = [
        f"offer offer_id=po-1 slot_id=PS-1 doctor_id=ASST-2-h1 facility={sender} procedure=VIS-FIS "
        "agenda_date=2026-11-24T09:00:00+01:00 agenda_day=tue",
        f"offer offer_id=po-2 slot_id=PS-2 doctor_id=ASST-2-h2 facility={sender} procedure=VIS-FIS "
        "agenda_date=2026-11-25T11:00:00+01:00 agenda_day=wed",
        # Outside the D window (5 agenda days): never offered to the citizen.
        f"offer offer_id=po-3 slot_id=PS-3 doctor_id=ASST-2-h1 facility={sender} procedure=VIS-FIS "
        "agenda_date=2026-12-07T09:00:00+01:00 agenda_day=mon",
    ]
    return msg(sender, sep.join(lines), AGENT)


def exhausted_chat() -> list:
    return booking_chat(slots=PAST_SLOTS)


async def advance(city: CrossCity, *, replacing: bool = False) -> None:
    await bc._advance_alone(
        city, city.inbound, list(city.chat), NEED, fixed=None, replacing=replacing
    )


def asked_partners_chat() -> list:
    """Our world exhausted, partners asked, citizen asked for cross_asst consent."""

    chat = exhausted_chat()
    request = bc._request_id(NEED)
    for partner in (P1, P2):
        chat.append(
            msg(
                AGENT,
                f"need.request service_type=hospital.physiatry procedure=VIS-FIS priority=D "
                f"request_id={request}",
                partner,
            )
        )
    chat.append(msg(AGENT, "data.request purpose=cross_asst fields=consent_ref", CIT))
    return chat


class PartnerSearch(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bc._SENT.clear()

    async def test_exhausted_world_asks_partners_with_search_criteria_only(self) -> None:
        city = CrossCity(exhausted_chat(), peers=[physiatry(P1), physiatry(P2)])
        await advance(city)
        for partner in (P1, P2):
            sent = city.sent_to(partner)
            self.assertEqual(len(sent), 1, partner)
            fields = bc.fields_of(sent[0])
            self.assertTrue(sent[0].startswith("need.request "))
            self.assertEqual(fields["service_type"], "hospital.physiatry")
            self.assertEqual(fields["procedure"], "VIS-FIS")
            self.assertEqual(fields["priority"], "D")
            self.assertTrue(fields["request_id"].startswith("rq-"))
            # No citizen data before consent: no citizen id, prescription, need id or lang.
            for secret in (CIT, RX, NEED, "lang="):
                self.assertNotIn(secret, sent[0])
        self.assertEqual(city.queries, ["hospital.physiatry"])

    async def test_exhausted_world_asks_citizen_cross_asst_consent(self) -> None:
        city = CrossCity(exhausted_chat(), peers=[physiatry(P1)])
        await advance(city)
        to_citizen = city.sent_to(CIT)
        self.assertEqual(len(to_citizen), 1)
        self.assertTrue(to_citizen[0].startswith("data.request purpose=cross_asst"))
        self.assertEqual(offer_lines(to_citizen[0]), [])

    async def test_own_facilities_are_never_partners(self) -> None:
        city = CrossCity(
            exhausted_chat(), peers=[physiatry(HOSP), physiatry(OWN_HOSPITAL), physiatry(P1)]
        )
        await advance(city)
        self.assertEqual(city.sent_to(HOSP), [])
        self.assertEqual(city.sent_to(OWN_HOSPITAL), [])
        self.assertEqual(len(city.sent_to(P1)), 1)

    async def test_partners_asked_once_across_turns(self) -> None:
        city = CrossCity(exhausted_chat(), peers=[physiatry(P1)])
        await advance(city)
        city.inbound = msg(LEDGER, "agenda agenda_now=2026-11-23T08:05:00+01:00", AGENT)
        city.chat.append(city.inbound)
        await advance(city)
        self.assertEqual(len(city.sent_to(P1)), 1)
        self.assertEqual(len([t for t in city.sent_to(CIT) if "cross_asst" in t]), 1)
        self.assertEqual(len(city.queries), 1)

    async def test_unreachable_partner_skipped_never_retried(self) -> None:
        city = CrossCity(exhausted_chat(), peers=[physiatry(P1), physiatry(P2)], down={P1})
        await advance(city)
        await advance(city)
        self.assertEqual(len([to for to, _ in city.attempts if to == P1]), 1)
        self.assertEqual(len(city.sent_to(P2)), 1)

    async def test_no_partner_found_tells_citizen_instead_of_silence(self) -> None:
        city = CrossCity(exhausted_chat(), peers=[physiatry(OWN_HOSPITAL)])
        await advance(city)
        to_citizen = city.sent_to(CIT)
        self.assertEqual(len(to_citizen), 1)
        self.assertNotIn("cross_asst", to_citizen[0])
        self.assertEqual(offer_lines(to_citizen[0]), [])

    async def test_fitting_own_slots_never_go_cross(self) -> None:
        city = CrossCity(booking_chat(), peers=[physiatry(P1)])
        await advance(city)
        self.assertEqual(city.queries, [])
        self.assertEqual(city.sent_to(P1), [])


class PartnerOffers(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bc._SENT.clear()

    async def test_partner_offer_waits_for_consent(self) -> None:
        chat = asked_partners_chat() + [partner_offer()]
        city = CrossCity(chat, peers=[physiatry(P1), physiatry(P2)])
        await advance(city)
        self.assertEqual([t for t in city.sent_to(CIT) if offer_lines(t)], [])

    async def test_consent_then_partner_slots_in_one_offer_message(self) -> None:
        chat = asked_partners_chat() + [
            partner_offer(),
            msg(CIT, f"consent consent_ref={CROSS_REF} purpose=cross_asst", AGENT),
        ]
        city = CrossCity(chat, peers=[physiatry(P1), physiatry(P2)])
        await advance(city)
        offers = [t for t in city.sent_to(CIT) if offer_lines(t)]
        self.assertEqual(len(offers), 1)
        parsed = bc._offers_in(offers[0])
        self.assertEqual({o["slot_id"] for o in parsed}, {"PS-1", "PS-2"})
        for offer in parsed:
            self.assertEqual(offer["facility"], P1)
            self.assertEqual(offer["prescription_number"], RX)
            self.assertEqual(offer["priority"], "D")

    async def test_semicolon_separated_partner_offers_are_read(self) -> None:
        chat = asked_partners_chat() + [
            partner_offer(sep=" ; "),
            msg(CIT, f"consent consent_ref={CROSS_REF} purpose=cross_asst", AGENT),
        ]
        city = CrossCity(chat, peers=[physiatry(P1)])
        await advance(city)
        offers = [t for t in city.sent_to(CIT) if offer_lines(t)]
        self.assertEqual(len(offers), 1)
        self.assertEqual({o["slot_id"] for o in bc._offers_in(offers[0])}, {"PS-1", "PS-2"})

    async def test_partner_offers_sent_once(self) -> None:
        chat = asked_partners_chat() + [
            partner_offer(),
            msg(CIT, f"consent consent_ref={CROSS_REF} purpose=cross_asst", AGENT),
        ]
        city = CrossCity(chat, peers=[physiatry(P1)])
        await advance(city)
        city.inbound = partner_offer(P2)
        city.chat.append(city.inbound)
        await advance(city)
        self.assertEqual(len([t for t in city.sent_to(CIT) if offer_lines(t)]), 1)

    async def test_plain_refusal_stops_cross_path(self) -> None:
        chat = asked_partners_chat() + [
            partner_offer(),
            msg(CIT, "No, preferisco restare nella mia ASST.", AGENT),
        ]
        city = CrossCity(chat, peers=[physiatry(P1)])
        await advance(city)
        to_citizen = city.sent_to(CIT)
        self.assertEqual([t for t in to_citizen if offer_lines(t)], [])
        self.assertEqual(len(to_citizen), 1)  # one polite note, no new data request
        self.assertNotIn("data.request", to_citizen[0])
        self.assertEqual(city.sent_to(P1), [])

    async def test_desk_routes_partner_offer(self) -> None:
        chat = asked_partners_chat() + [
            msg(CIT, f"consent consent_ref={CROSS_REF} purpose=cross_asst", AGENT),
            partner_offer(),
        ]
        city = CrossCity(chat, peers=[physiatry(P1)])
        await bc._desk(city, city.inbound, list(city.chat), hold_self=False, fixed=None)
        self.assertEqual(len([t for t in city.sent_to(CIT) if offer_lines(t)]), 1)


def offered_chat(*, consent: bool = True) -> list:
    chat = asked_partners_chat() + [partner_offer()]
    if consent:
        chat.append(msg(CIT, f"consent consent_ref={CROSS_REF} purpose=cross_asst", AGENT))
    chat.append(
        msg(
            AGENT,
            f"offer offer_id=of-PS-1 slot_id=PS-1 doctor_id=ASST-2-h1 facility={P1} "
            f"procedure=VIS-FIS agenda_date=2026-11-24T09:00:00+01:00 agenda_day=tue "
            f"priority=D prescription_number={RX}",
            CIT,
        )
    )
    return chat


class PartnerConfirm(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bc._SENT.clear()

    async def test_confirm_forwards_with_cross_asst_consent(self) -> None:
        chat = offered_chat() + [
            msg(CIT, f"confirm offer_id=of-PS-1 consent_ref=cons-{NEED}-booking", AGENT)
        ]
        city = CrossCity(chat, peers=[physiatry(P1)])
        await bc._desk(city, city.inbound, list(city.chat), hold_self=False, fixed=None)
        sent = city.sent_to(P1)
        self.assertEqual(len(sent), 1)
        fields = bc.fields_of(sent[0])
        self.assertTrue(sent[0].startswith("confirm "))
        self.assertEqual(fields["slot_id"], "PS-1")
        self.assertEqual(fields["consent_ref"], CROSS_REF)
        self.assertEqual(fields["offer_id"], "po-1")  # the partner's own offer id
        self.assertEqual(fields["citizen"], CIT)
        self.assertEqual(fields["prescription_number"], RX)
        self.assertEqual(city.sent_to(LEDGER), [])  # the booking centre never holds

    async def test_confirm_without_cross_consent_shares_nothing(self) -> None:
        chat = offered_chat(consent=False)
        chat = [row for row in chat if not ("data.request" in row.text and "cross_asst" in row.text)]
        chat.append(msg(CIT, f"confirm offer_id=of-PS-1 consent_ref=cons-{NEED}-booking", AGENT))
        city = CrossCity(chat, peers=[physiatry(P1)])
        await bc._desk(city, city.inbound, list(city.chat), hold_self=False, fixed=None)
        self.assertEqual(city.sent_to(P1), [])
        to_citizen = city.sent_to(CIT)
        self.assertEqual(len(to_citizen), 1)
        self.assertTrue(to_citizen[0].startswith("data.request purpose=cross_asst"))

    async def test_pending_confirm_forwarded_once_consent_arrives(self) -> None:
        chat = offered_chat(consent=False) + [
            msg(CIT, f"confirm offer_id=of-PS-1 consent_ref=cons-{NEED}-booking", AGENT),
            msg(CIT, f"consent consent_ref={CROSS_REF} purpose=cross_asst", AGENT),
        ]
        city = CrossCity(chat, peers=[physiatry(P1)])
        await bc._desk(city, city.inbound, list(city.chat), hold_self=False, fixed=None)
        sent = city.sent_to(P1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(bc.fields_of(sent[0])["consent_ref"], CROSS_REF)

    async def test_partner_cancel_uses_our_offer_id(self) -> None:
        chat = offered_chat() + [
            msg(CIT, f"confirm offer_id=of-PS-1 consent_ref=cons-{NEED}-booking", AGENT),
            msg(
                AGENT,
                f"confirm offer_id=po-1 slot_id=PS-1 citizen={CIT} consent_ref={CROSS_REF} "
                f"facility={P1} prescription_number={RX}",
                P1,
            ),
            msg(P1, "cancel offer_id=po-1 reason=NO_CAPACITY", AGENT),
        ]
        city = CrossCity(chat, peers=[physiatry(P1)])
        await bc._desk(city, city.inbound, list(city.chat), hold_self=False, fixed=None)
        cancels = [t for t in city.sent_to(CIT) if t.startswith("cancel ")]
        self.assertEqual(len(cancels), 1)
        self.assertEqual(bc.fields_of(cancels[0])["offer_id"], "of-PS-1")

    async def test_partner_no_slots_cancel_keeps_citizen_offers(self) -> None:
        chat = offered_chat() + [msg(P2, f"cancel request_id={bc._request_id(NEED)} reason=no_slots", AGENT)]
        city = CrossCity(chat, peers=[physiatry(P1), physiatry(P2)])
        await bc._desk(city, city.inbound, list(city.chat), hold_self=False, fixed=None)
        self.assertEqual(city.sent_to(CIT), [])  # no cancel, no duplicate offers

    async def test_every_partner_without_slots_tells_citizen_once(self) -> None:
        request = bc._request_id(NEED)
        chat = asked_partners_chat() + [
            msg(CIT, f"consent consent_ref={CROSS_REF} purpose=cross_asst", AGENT),
            msg(P1, f"cancel request_id={request} reason=no_slots", AGENT),
            msg(P2, f"cancel request_id={request} reason=no_slots", AGENT),
        ]
        city = CrossCity(chat, peers=[physiatry(P1), physiatry(P2)])
        await bc._desk(city, city.inbound, list(city.chat), hold_self=False, fixed=None)
        await bc._desk(city, city.inbound, list(city.chat), hold_self=False, fixed=None)
        to_citizen = city.sent_to(CIT)
        self.assertEqual(len(to_citizen), 1)
        self.assertEqual(offer_lines(to_citizen[0]), [])
        self.assertNotIn("cancel", to_citizen[0])

    async def test_booking_centre_never_holds_on_agent_confirm(self) -> None:
        chat = booking_chat() + [
            msg(P1, f"confirm offer_id=x slot_id=S-1 consent_ref=c-1 citizen={CIT}", AGENT)
        ]
        city = CrossCity(chat, peers=[])
        await bc._desk(city, city.inbound, list(city.chat), hold_self=False, fixed=None)
        self.assertEqual([t for t in city.sent_to(LEDGER) if t.startswith("hold")], [])


if __name__ == "__main__":
    unittest.main()
