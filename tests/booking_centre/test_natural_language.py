"""Booking centre: plain-language citizens, no silence, other ASSTs and our own agents.

Run from the repo root with the city-cli Python (it ships city_sdk):
    ~/.local/share/uv/tools/city-cli/bin/python -m unittest tests.booking_centre.test_natural_language -v
"""

from __future__ import annotations

import unittest

from city_sdk.city import LEDGER, Message, Peer, understood

from tests.booking_centre.test_cross_asst import PAST_ONLY, PartnerCity
from tests.booking_centre.test_quick_wins import (
    AGENT,
    CIT,
    DEFAULT_SLOTS,
    HOSP,
    NOW,
    RX,
    bc,
    booking_chat,
    msg,
    offer_lines,
    reset_memory,
)

PARTNER = "agent:otherbc"


class _Fresh(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        self.setUp()  # the brain keeps module globals: leave nothing for other test files

    def setUp(self) -> None:
        reset_memory()


def plain_chat(first: str, **kwargs) -> list[Message]:
    """booking_chat with the citizen's first message replaced by a plain sentence."""

    chat = booking_chat(**kwargs)
    chat[0] = msg(CIT, first, AGENT)
    return chat


async def desk(city: PartnerCity, message: Message) -> None:
    city.chat.append(message) if message not in city.chat else None
    city.inbound = message
    await bc._desk(city, message, city.chat, hold_self=False, fixed=None)


def to_citizen(city: PartnerCity) -> list[str]:
    return city.sent_to(CIT)


class ReadsPlainSentences(unittest.TestCase):
    def test_everything_in_one_sentence(self) -> None:
        said = bc._citizen_fields(
            "Buongiorno, ho la ricetta numero AB12-CD34, parlo inglese, "
            "preferisco il mercoledì, meglio una dottoressa. Abito nel distretto 7."
        )
        self.assertEqual(said["prescription_number"], "prescription_number-AB12-CD34")
        self.assertEqual(said["lang"], "en")
        self.assertEqual(said["days"], "wed")
        self.assertEqual(said["gender"], "f")
        self.assertEqual(said["area.district"], "7")

    def test_full_number_in_text(self) -> None:
        said = bc._citizen_fields("ciao ecco prescription_number-ABCD grazie")
        self.assertEqual(said["prescription_number"], RX)

    def test_tokens_win(self) -> None:
        said = bc._citizen_fields(f"need.request prescription_number={RX} lang=it days=mon")
        self.assertEqual(said["prescription_number"], RX)
        self.assertEqual(said["lang"], "it")

    def test_bare_token_value_gets_prefix(self) -> None:
        said = bc._citizen_fields("prescription_number=abcd")
        self.assertEqual(said["prescription_number"], RX)

    def test_word_without_digits_is_not_a_number(self) -> None:
        self.assertNotIn("prescription_number", bc._citizen_fields("ho la ricetta per il ginocchio"))

    def test_excluded_day(self) -> None:
        self.assertEqual(bc._citizen_fields("va bene tutto tranne il lunedì")["days"],
                         "fri/thu/tue/wed")

    def test_english_sentence(self) -> None:
        self.assertEqual(bc._guess_lang("Hello, I have my prescription and I would like a visit"),
                         "en")
        self.assertEqual(bc._guess_lang("Buongiorno, vorrei prenotare una visita"), "it")

    def test_lang_from_sentence(self) -> None:
        chat = [msg(CIT, "Hi, I would like to book with my prescription please", AGENT)]
        self.assertEqual(bc._lang(chat), "en")

    def test_visita_medica_is_not_a_gender(self) -> None:
        self.assertNotIn("gender", bc._citizen_fields("devo fare una visita medica"))

    def test_choice(self) -> None:
        offers = [
            {"offer_id": "of-S-1", "slot_id": "S-1", "agenda_day": "mon",
             "agenda_date": "2026-11-23T14:00:00+01:00"},
            {"offer_id": "of-S-5", "slot_id": "S-5", "agenda_day": "wed",
             "agenda_date": "2026-11-25T09:00:00+01:00"},
        ]
        self.assertEqual(bc._choice("la seconda va benissimo", offers)["slot_id"], "S-5")
        self.assertEqual(bc._choice("I'll take the first one", offers)["slot_id"], "S-1")
        self.assertEqual(bc._choice("ok mercoledì", offers)["slot_id"], "S-5")
        self.assertEqual(bc._choice("of-S-5", offers)["slot_id"], "S-5")
        self.assertEqual(bc._choice("2", offers)["slot_id"], "S-5")
        self.assertIsNone(bc._choice("nessuno va bene", offers))
        self.assertIsNone(bc._choice("quando sono aperti?", offers))


class NoActWordInSentences(unittest.TestCase):
    """understood() reads the LAST act word: our sentences must not hold one."""

    def test_phrases(self) -> None:
        texts = [text for pair in bc._PHRASES.values() for text in pair]
        texts += [bc._RENEW_IT, bc._RENEW_EN, bc._NO_PARTNER_IT, bc._NO_PARTNER_EN,
                  bc._EMERGENCY_IT, bc._EMERGENCY_EN, bc._OFFER_IT, bc._OFFER_EN]
        for text in texts:
            found = {token for token in text.replace(",", " ").split() if token in bc._ACT_WORDS}
            self.assertEqual(found, set(), text)

    def test_renewal_reads_as_data_request(self) -> None:
        text = f"data.request purpose=prescription_renewal fields=prescription_number {bc._RENEW_EN}"
        self.assertEqual(understood(msg(AGENT, text, CIT)).type, "data.request")


class PlainCitizen(_Fresh):
    async def test_plain_first_message_gets_offers(self) -> None:
        chat = plain_chat("Buongiorno! Ho la ricetta prescription_number-ABCD, "
                          "vorrei andare di giovedì se possibile")
        city = PartnerCity(chat)
        await desk(city, chat[0])
        sent = to_citizen(city)
        self.assertEqual(len(sent), 1)
        lines = offer_lines(sent[0])
        self.assertTrue(lines)
        self.assertIn("agenda_day=thu", lines[0])

    async def test_hello_without_number_is_asked_for_it(self) -> None:
        city = PartnerCity([])
        await desk(city, msg(CIT, "Buongiorno, vorrei prenotare una visita", AGENT))
        sent = to_citizen(city)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("data.request purpose=booking fields=prescription_number"))
        self.assertEqual(understood(msg(AGENT, sent[0], CIT)).type, "data.request")

    async def test_english_hello_answered_in_english(self) -> None:
        city = PartnerCity([])
        await desk(city, msg(CIT, "Hi, I would like to book a visit please", AGENT))
        self.assertIn("Could you send me the number", to_citizen(city)[0])

    async def test_unknown_number_asked_again(self) -> None:
        chat = plain_chat("ho la ricetta 12345",
                          rx_reply="refused prescription code=PRESCRIPTION_NOT_FOUND",
                          number="prescription_number-12345")
        city = PartnerCity(chat)
        await desk(city, chat[0])
        sent = to_citizen(city)
        self.assertEqual(len(sent), 1)
        self.assertIn("fields=prescription_number", sent[0])
        self.assertIn("Non trovo questa ricetta", sent[0])

    async def test_medical_question_never_answered(self) -> None:
        city = PartnerCity([])
        await desk(city, msg(CIT, "Che farmaco devo prendere per il mal di schiena?", AGENT))
        sent = " ".join(to_citizen(city))
        self.assertIn("non posso dare diagnosi", sent)
        self.assertNotIn("prescribe", sent)
        self.assertEqual([to for to, _ in city.attempts if to == LEDGER], [])

    async def test_red_flag_escalates(self) -> None:
        city = PartnerCity([])
        await desk(city, msg(CIT, "ho una stretta fortissima al petto, aiuto", AGENT))
        sent = to_citizen(city)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("escalate.emergency reason=red_flag"))


class PlainChoice(_Fresh):
    async def _offered(self) -> PartnerCity:
        # Six slots or fewer: every free slot is in the first offer message.
        chat = booking_chat(slots=DEFAULT_SLOTS[:5])
        city = PartnerCity(chat)
        await desk(city, chat[0])
        self.assertTrue(offer_lines(to_citizen(city)[0]))
        return city

    async def test_ordinal_with_consent_forwards(self) -> None:
        city = await self._offered()
        second = bc._offers_in(to_citizen(city)[0])[1]
        await desk(city, msg(CIT, "prendo la seconda, consent_ref=cons-1", AGENT))
        forwarded = city.sent_to(HOSP)
        self.assertEqual(len(forwarded), 1)
        self.assertIn(f"slot_id={second['slot_id']}", forwarded[0].split())
        self.assertIn("consent_ref=cons-1", forwarded[0].split())
        # The citizen hears that the choice went through, not silence.
        self.assertIn("Ho inoltrato", to_citizen(city)[-1])

    async def test_choice_without_consent_asks_it_then_books(self) -> None:
        city = await self._offered()
        first = bc._offers_in(to_citizen(city)[0])[0]
        await desk(city, msg(CIT, "la prima va bene", AGENT))
        ask = to_citizen(city)[-1]
        self.assertTrue(ask.startswith(f"consent purpose=booking offer_id={first['offer_id']}"))
        self.assertEqual(city.sent_to(HOSP), [])
        await desk(city, msg(CIT, "consent consent_ref=cons-7 purpose=booking", AGENT))
        forwarded = city.sent_to(HOSP)
        self.assertEqual(len(forwarded), 1)
        self.assertIn(f"slot_id={first['slot_id']}", forwarded[0].split())
        self.assertIn("consent_ref=cons-7", forwarded[0].split())

    async def test_plain_no_to_consent_is_respected(self) -> None:
        city = await self._offered()
        await desk(city, msg(CIT, "la prima", AGENT))
        await desk(city, msg(CIT, "no grazie, preferisco di no", AGENT))
        self.assertEqual(city.sent_to(HOSP), [])
        self.assertIn("non prenoto nulla", to_citizen(city)[-1])

    async def test_new_day_preference_reoffers(self) -> None:
        city = await self._offered()
        await desk(city, msg(CIT, "scusi, potrei solo di mercoledì?", AGENT))
        lines = offer_lines(to_citizen(city)[-1])
        self.assertTrue(lines)
        self.assertIn("agenda_day=wed", lines[0])

    async def test_unclear_message_after_offers_gets_a_reply(self) -> None:
        city = await self._offered()
        before = len(to_citizen(city))
        await desk(city, msg(CIT, "mmm non so, ci penso", AGENT))
        self.assertEqual(len(to_citizen(city)), before + 1)
        self.assertIn("ancora valide", to_citizen(city)[-1])

    async def test_unknown_offer_lists_open_ones(self) -> None:
        city = await self._offered()
        await desk(city, msg(CIT, "confirm offer_id=of-NOPE consent_ref=cons-1", AGENT))
        self.assertIn("Non trovo questa proposta", to_citizen(city)[-1])
        self.assertIn("offer_id=of-S-", to_citizen(city)[-1])

    async def test_receipt_has_next_step_and_thanks_answered(self) -> None:
        city = await self._offered()
        await desk(city, msg(CIT, "la prima consent_ref=cons-1", AGENT))
        await desk(city, msg(HOSP, "booking.receipt booking_id=B-1 ledger_ref=L-1", AGENT))
        receipt = to_citizen(city)[-1]
        self.assertTrue(receipt.startswith("booking.receipt booking_id=B-1 ledger_ref=L-1"))
        self.assertEqual(understood(msg(AGENT, receipt, CIT)).type, "booking.receipt")
        await desk(city, msg(CIT, "grazie mille!", AGENT))
        self.assertIn("prenotazione è registrata", to_citizen(city)[-1])


class NoPartnerToAsk(_Fresh):
    async def test_empty_index_closes_search_at_once(self) -> None:
        chat = booking_chat(slots=PAST_ONLY)
        city = PartnerCity(chat, peers=[])
        await desk(city, chat[0])
        self.assertIn("Mi dispiace", to_citizen(city)[-1])

    async def test_our_own_agents_are_never_partners(self) -> None:
        phys = ("hospital.physiatry",)
        ours = [Peer(agent_id=agent_id, role="facility", capabilities=phys)
                for agent_id in bc._TEAM_AGENTS]
        city = PartnerCity(booking_chat(slots=PAST_ONLY), peers=ours)
        await desk(city, city.chat[0])
        asked = [to for to, text in city.attempts if text.startswith("need.request")]
        self.assertEqual(asked, [])


def partner_case() -> list[Message]:
    """The partner's need in our mailbox: we already read world and agenda for it."""

    world = booking_chat()[7]
    return [msg(AGENT, "agenda", LEDGER), msg(LEDGER, f"agenda agenda_now={NOW}", AGENT),
            msg(AGENT, "world procedure=VIS-FIS page=1", LEDGER), world]


class OtherAsstsAskUs(_Fresh):
    async def test_need_request_gets_one_offer_message(self) -> None:
        city = PartnerCity(partner_case())
        ask = msg(PARTNER, "need.request service_type=hospital.physiatry procedure=VIS-FIS "
                           "priority=D request_id=rq-9", AGENT)
        await desk(city, ask)
        sent = city.sent_to(PARTNER)
        self.assertEqual(len(sent), 1)
        lines = offer_lines(sent[0])
        self.assertTrue(1 <= len(lines) <= 6)
        for line in lines:
            self.assertIn(f"facility={HOSP}", line.split())
            self.assertIn("request_id=rq-9", line.split())
            self.assertNotIn("citizen=", line)

    async def test_plain_request_understood(self) -> None:
        city = PartnerCity(partner_case())
        ask = msg(PARTNER, "Ciao, avete posti liberi per una visita fisiatrica, priorità D?", AGENT)
        await desk(city, ask)
        lines = offer_lines(city.sent_to(PARTNER)[0])
        self.assertTrue(lines)
        self.assertIn("procedure=VIS-FIS", lines[0].split())
        self.assertIn(f"request_id=rq-{ask.msg_id}", lines[0].split())

    async def test_no_slot_is_a_cancel(self) -> None:
        case = partner_case()
        case[-1] = msg(LEDGER, "slots procedure=VIS-FIS page=1/1\n" + PAST_ONLY[0], AGENT)
        city = PartnerCity(case)
        await desk(city, msg(PARTNER, "need.request procedure=VIS-FIS priority=B request_id=r1",
                             AGENT))
        self.assertEqual(city.sent_to(PARTNER),
                         ["cancel request_id=r1 reason=no_availability"])

    async def test_partner_confirm_forwarded_not_held(self) -> None:
        city = PartnerCity(partner_case())
        await desk(city, msg(PARTNER, "need.request procedure=VIS-FIS priority=D request_id=r2",
                             AGENT))
        offer = bc._offers_in(city.sent_to(PARTNER)[0])[0]
        await desk(city, msg(PARTNER, f"confirm offer_id={offer['offer_id']} "
                                      f"slot_id={offer['slot_id']} facility={HOSP} "
                                      "citizen=citizen:far consent_ref=cx-1", AGENT))
        self.assertEqual([t for t in city.sent_to(LEDGER) if t.startswith("hold")], [])
        self.assertEqual(len(city.sent_to(HOSP)), 1)
        await desk(city, msg(HOSP, "booking.receipt booking_id=B-9 ledger_ref=L-9", AGENT))
        self.assertEqual(city.sent_to(PARTNER)[-1], "booking.receipt booking_id=B-9 ledger_ref=L-9")

    async def test_unclear_agent_told_format_once(self) -> None:
        city = PartnerCity([])
        await desk(city, msg(PARTNER, "hey what's up", AGENT))
        await desk(city, msg(PARTNER, "hello??", AGENT))
        self.assertEqual(len(city.sent_to(PARTNER)), 1)

    async def test_our_own_agents_get_no_format_note(self) -> None:
        city = PartnerCity([])
        await desk(city, msg(next(iter(bc._TEAM_AGENTS)), "ciao", AGENT))
        self.assertEqual(city.attempts, [])


class PartnersAnswerInPlainWords(_Fresh):
    async def test_plain_no_from_every_partner_closes_search(self) -> None:
        phys = ("hospital.physiatry",)
        partners = [Peer(agent_id=PARTNER, role="facility", capabilities=phys)]
        city = PartnerCity(booking_chat(slots=PAST_ONLY), peers=partners)
        await desk(city, city.chat[0])
        self.assertEqual(len(city.sent_to(PARTNER)), 1)
        await desk(city, msg(PARTNER, "Mi spiace, non abbiamo posti liberi", AGENT))
        self.assertIn("Mi dispiace", to_citizen(city)[-1])
        # We never answer the partner's plain "no" with our own offers.
        self.assertEqual(len(city.sent_to(PARTNER)), 1)

    async def test_refused_forward_of_partner_confirm_goes_back_to_partner(self) -> None:
        city = PartnerCity(partner_case(), down={HOSP})
        await desk(city, msg(PARTNER, "need.request procedure=VIS-FIS priority=D request_id=r3",
                             AGENT))
        offer = bc._offers_in(city.sent_to(PARTNER)[0])[0]
        await desk(city, msg(PARTNER, f"confirm offer_id={offer['offer_id']} "
                                      f"slot_id={offer['slot_id']} facility={HOSP} "
                                      "citizen=citizen:far consent_ref=cx-1", AGENT))
        self.assertTrue(city.sent_to(PARTNER)[-1].startswith(
            f"cancel offer_id={offer['offer_id']} reason=facility_unavailable"))
        self.assertEqual(city.sent_to("citizen:far"), [])


class RingFormat(_Fresh):
    """The shared cross-ASST format (Giambellino proposal): confirm without facility=."""

    async def test_partner_confirm_without_facility_or_history(self) -> None:
        city = PartnerCity(partner_case())
        await desk(city, msg(PARTNER, "need.request service_type=hospital.physiatry "
                                      "procedure=VIS-FIS priority=D request_id=r4", AGENT))
        offer = bc._offers_in(city.sent_to(PARTNER)[0])[0]
        self.assertIn("doctor_lang=", city.sent_to(PARTNER)[0])
        # Our own answer is missing from history: the offer id alone must be enough.
        city.chat[:] = [row for row in city.chat if row.sender != AGENT or row.recipient == LEDGER]
        await desk(city, msg(PARTNER, f"confirm offer_id={offer['offer_id']} "
                                      f"slot_id={offer['slot_id']} citizen=citizen:far "
                                      "consent_ref=cx-2 prescription_number=prescription_number-ZZ99",
                             AGENT))
        self.assertEqual(len(city.sent_to(HOSP)), 1)


class LorenteggioFormat(_Fresh):
    """Shared ring format, Lorenteggio flavour: lang/gender/days criteria, cross_asst ref."""

    async def test_criteria_filter_our_answer(self) -> None:
        city = PartnerCity(partner_case())
        await desk(city, msg(PARTNER, "need.request service_type=hospital.physiatry "
                                      "procedure=VIS-FIS priority=D request_id=r5 lang=en "
                                      "gender=f days=tue", AGENT))
        lines = offer_lines(city.sent_to(PARTNER)[0])
        self.assertTrue(lines)
        for line in lines:
            self.assertIn("doctor_gender=f", line.split())
            self.assertIn("en", line.split("doctor_lang=")[1].split()[0].split("/"))
        self.assertIn("agenda_day=tue", lines[0].split())

    async def test_forward_to_partner_carries_cross_ref(self) -> None:
        chat = booking_chat()
        offer = ("offer offer_id=px-1 slot_id=PS-1 doctor_id=ASST-9-h1 facility=agent:pfac "
                 "procedure=VIS-FIS agenda_date=2026-11-24T10:00:00+01:00 agenda_day=tue")
        chat += [msg("agent:pfac", offer, AGENT),
                 msg(CIT, "consent consent_ref=cx-77 purpose=cross_asst", AGENT)]
        city = PartnerCity(chat)
        await bc._forward_confirm(city, chat[-1], chat, offer_id="px-1", slot="PS-1",
                                  citizen=CIT, consent="cx-77", facility="agent:pfac",
                                  number=RX, cross="cx-77")
        sent = city.sent_to("agent:pfac")[0].split()
        self.assertIn("consent_ref=cx-77", sent)
        self.assertIn("cross_asst_consent_ref=cx-77", sent)


class LogReport2343(_Fresh):
    """Fixes from the 23:43 log report (journeys 104, 113, 116 and the LLM judge)."""

    async def test_offer_sentence_is_proactive(self) -> None:
        chat = booking_chat(citizen_extra="days=wed")
        city = PartnerCity(chat)
        await desk(city, chat[0])
        text = to_citizen(city)[0]
        self.assertIn("mercoledì", text)
        self.assertIn("accessibilità", text)
        self.assertIn("mezzi pubblici", text)
        self.assertEqual(understood(msg(AGENT, text, CIT)).type, "offer")

    async def test_cancel_with_offer_id_none_never_reaches_citizen(self) -> None:
        chat = booking_chat(slots=PAST_ONLY)
        city = PartnerCity(chat, peers=[Peer(agent_id=PARTNER, role="facility",
                                             capabilities=("hospital.physiatry",))])
        await desk(city, chat[0])
        await desk(city, msg(PARTNER, "cancel offer_id=none reason=facility_cancelled", AGENT))
        self.assertFalse(any("offer_id=none" in text for text in to_citizen(city)))

    async def test_refused_doctor_not_offered_again_that_day(self) -> None:
        chat = booking_chat(slots=DEFAULT_SLOTS[:5])
        city = PartnerCity(chat)
        await desk(city, chat[0])
        await desk(city, msg(CIT, "confirm offer_id=of-S-1 consent_ref=cons-1", AGENT))
        await desk(city, msg(HOSP, "cancel offer_id=of-S-1 reason=facility_cancelled", AGENT))
        cancel = [t for t in to_citizen(city) if t.startswith("cancel offer_id=of-S-1")]
        self.assertEqual(len(cancel), 1)
        self.assertIn("altre date", cancel[0])
        again = bc._offers_in(to_citizen(city)[-1])
        self.assertTrue(again)
        # S-1 and S-2 are both h1 on Monday 23: neither comes back.
        self.assertFalse({o["slot_id"] for o in again} & {"S-1", "S-2"})

    async def test_same_cancel_twice_told_once(self) -> None:
        chat = booking_chat(slots=DEFAULT_SLOTS[:5])
        city = PartnerCity(chat)
        await desk(city, chat[0])
        await desk(city, msg(CIT, "confirm offer_id=of-S-3 consent_ref=cons-1", AGENT))
        for _ in range(2):
            await desk(city, msg(HOSP, "cancel offer_id=of-S-3 reason=facility_cancelled", AGENT))
        self.assertEqual(len([t for t in to_citizen(city) if t.startswith("cancel ")]), 1)

    async def test_no_offer_after_confirm(self) -> None:
        chat = booking_chat(slots=DEFAULT_SLOTS[:5])
        city = PartnerCity(chat)
        await desk(city, chat[0])
        await desk(city, msg(CIT, "confirm offer_id=of-S-3 consent_ref=cons-1", AGENT))
        before = len(to_citizen(city))
        await desk(city, msg(LEDGER, "agenda agenda_now=" + NOW, AGENT))
        self.assertEqual(len(to_citizen(city)), before)

    async def test_english_doctor_missing_gets_relaxed_alternative(self) -> None:
        italian_only = [line for line in DEFAULT_SLOTS if "-h2 " in line]
        chat = plain_chat(f"need.request prescription_number={RX} lang=en", slots=italian_only)
        partners = [Peer(agent_id=PARTNER, role="facility", capabilities=("hospital.physiatry",))]
        city = PartnerCity(chat, peers=partners)
        await desk(city, chat[0])
        last = to_citizen(city)[-1]
        self.assertTrue(bc._offers_in(last), to_citizen(city))
        self.assertIn("Italian-speaking", last)

    async def test_confirm_to_facility_explains_and_flags_access(self) -> None:
        chat = booking_chat(slots=DEFAULT_SLOTS[:5],
                            citizen_extra="vengo in carrozzina")
        city = PartnerCity(chat)
        await desk(city, chat[0])
        await desk(city, msg(CIT, "confirm offer_id=of-S-3 consent_ref=cons-1", AGENT))
        sent = city.sent_to(HOSP)[0]
        self.assertTrue(sent.startswith("confirm "))
        self.assertIn("vi inoltro la scelta del cittadino", sent)
        self.assertIn("accessibilità", sent)
        self.assertEqual(understood(msg(AGENT, sent, HOSP)).type, "confirm")

    async def test_receipt_says_when_and_how_to_move_it(self) -> None:
        chat = booking_chat(slots=DEFAULT_SLOTS[:5])
        city = PartnerCity(chat)
        await desk(city, chat[0])
        await desk(city, msg(CIT, "confirm offer_id=of-S-3 consent_ref=cons-1", AGENT))
        await desk(city, msg(HOSP, "booking.receipt booking_id=B-9 ledger_ref=L-9", AGENT))
        receipt = [t for t in to_citizen(city) if t.startswith("booking.receipt")][-1]
        self.assertIn("martedì 24/11 alle 09:00", receipt)
        self.assertIn("08:00-13:00", receipt)
        self.assertEqual(understood(msg(AGENT, receipt, CIT)).type, "booking.receipt")


if __name__ == "__main__":
    unittest.main()
