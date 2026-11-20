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
        bc._SENT.clear()
        bc._GATES.clear()


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

    async def test_citizen_told_once(self) -> None:
        city = PartnerCity(booking_chat(slots=PAST_ONLY), peers=peers())
        await advance(city)
        await advance(city)
        notices = [
            text for text in city.sent_to(CIT)
            if not text.startswith("offer") and "ASST" in text
        ]
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
    return [text for text in city.sent_to(CIT) if "offer offer_id=" in text]


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


if __name__ == "__main__":
    unittest.main()
