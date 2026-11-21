"""Consultorio: delayed Ledger replies must respect the citizen's latest choice."""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from city_sdk.city import LEDGER, CapError

from tests.family_counseling.test_natural_language import (
    AGENT, CIT, LedgerCity, citizen_says, fc, msg,
)


class InterruptedHold(LedgerCity):
    def __init__(self, interruption: str) -> None:
        super().__init__()
        self.interruption = interruption
        self.interrupted = False

    async def say(self, to, text, *, status=None):
        if to == LEDGER and text.startswith("hold "):
            sent = msg(AGENT, text, to)
            self.chat.append(sent)
            return sent.msg_id
        return await super().say(to, text, status=status)

    async def history(self, peer=None, *, need_id=None):
        if self.to_ledger("hold") and not self.interrupted:
            self.interrupted = True
            held = fc.fields_of(self.to_ledger("hold")[-1])
            self.chat += [msg(CIT, self.interruption, AGENT),
                          msg(LEDGER, f"held hold_id=H-late slot_id={held['slot_id']} fence=1", AGENT)]
        return list(self.chat)


class BookingSafety(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fc._FAMILY_CASES.clear()

    async def offered(self, city=None):
        city = city or LedgerCity()
        await citizen_says(city, "Buongiorno, vorrei un appuntamento al consultorio")
        offers = [o for text in city.to_citizen() for o in fc._offer_rows(text)]
        self.assertTrue(offers)
        return city, offers

    async def test_cancel_during_hold_never_commits_even_after_restart(self):
        city, offers = await self.offered(InterruptedHold("cancel reason=changed_mind"))
        await citizen_says(city, f"confirm offer_id={offers[0]['offer_id']} consent_ref=c-1")
        self.assertTrue(city.interrupted)
        self.assertEqual(city.to_ledger("commit"), [])
        self.assertFalse(any(t.startswith("booking.receipt") for t in city.to_citizen()))
        fc._FAMILY_CASES.clear()
        late = city.chat[-1]
        await fc._family(city, late, list(city.chat))
        self.assertEqual(city.to_ledger("commit"), [])

    async def test_plain_withdrawal_during_hold_never_commits(self):
        city, offers = await self.offered(InterruptedHold("Ho cambiato idea, non prenotare"))
        await citizen_says(city, f"confirm offer_id={offers[0]['offer_id']} consent_ref=c-1")
        self.assertEqual(city.to_ledger("commit"), [])
        self.assertTrue(any("reason=choice_withdrawn" in t for t in city.to_citizen()))

    async def test_changed_selection_during_hold_does_not_commit_old_slot(self):
        city, offers = await self.offered()
        interrupted = InterruptedHold(f"confirm offer_id={offers[1]['offer_id']} consent_ref=c-2")
        interrupted.chat = list(city.chat)
        await citizen_says(interrupted, f"confirm offer_id={offers[0]['offer_id']} consent_ref=c-1")
        self.assertEqual(interrupted.to_ledger("commit"), [])
        self.assertEqual(len(interrupted.to_ledger("hold")), 1)

    async def test_cancel_does_not_reask_consent_or_publish_slot_as_free(self):
        city, offers = await self.offered()
        await citizen_says(city, f"confirm offer_id={offers[0]['offer_id']}")
        before = len(city.to_citizen())
        await citizen_says(city, f"cancel offer_id={offers[0]['offer_id']} reason=changed_mind")
        after = city.to_citizen()[before:]
        self.assertFalse(any(t.startswith(("consent ", "offer ")) for t in after), after)
        self.assertFalse(any("slot_free" in row.text for row in city.chat
                             if row.recipient == fc._OUR_BOOKING_CENTRE))
        self.assertEqual(city.to_ledger("hold"), [])

    async def test_report_consent_cannot_authorize_booking(self):
        city, offers = await self.offered()
        await citizen_says(city, f"confirm offer_id={offers[0]['offer_id']} "
                                 "purpose=report_sharing consent_ref=report-only")
        self.assertEqual(city.to_ledger("hold"), [])

    async def test_placeholder_consent_cannot_authorize_booking(self):
        for ref in ("false", "none", "null"):
            with self.subTest(ref=ref):
                fc._FAMILY_CASES.clear()
                city, offers = await self.offered()
                await citizen_says(city, f"confirm offer_id={offers[0]['offer_id']} consent_ref={ref}")
                self.assertEqual(city.to_ledger("hold"), [])

    async def test_expired_local_offer_is_not_held(self):
        class LaterClock(LedgerCity):
            late = False
            def _answer(self, text):
                if text == "agenda" and self.late:
                    return "agenda agenda_now=2026-11-26T18:00:00+01:00"
                return super()._answer(text)
        city, offers = await self.offered(LaterClock())
        city.late = True
        await citizen_says(city, f"confirm offer_id={offers[0]['offer_id']} consent_ref=c-1")
        self.assertEqual(city.to_ledger("hold"), [])
        self.assertTrue(any("reason=slot_expired" in t for t in city.to_citizen()))

    async def test_completed_booking_is_not_claimed_cancelled_or_free(self):
        city, offers = await self.offered()
        await citizen_says(city, f"confirm offer_id={offers[0]['offer_id']} consent_ref=c-1")
        before = len(city.to_citizen())
        await citizen_says(city, f"cancel offer_id={offers[0]['offer_id']} reason=changed_mind")
        self.assertTrue(any("reason=already_booked" in t for t in city.to_citizen()[before:]))
        self.assertFalse(any("slot_free" in row.text for row in city.chat
                             if row.recipient == fc._OUR_BOOKING_CENTRE))
        self.assertEqual(len(city.to_ledger("hold")), 1)

    async def test_late_emergency_during_hold_stops_booking(self):
        city, offers = await self.offered(InterruptedHold("Ho un forte dolore al petto"))
        await citizen_says(city, f"confirm offer_id={offers[0]['offer_id']} consent_ref=c-1")
        self.assertEqual(city.to_ledger("commit"), [])
        self.assertTrue(any(t.startswith("escalate.emergency") for t in city.to_citizen()))

    async def test_accepted_commit_with_uncertain_result_is_not_repeated(self):
        class UncertainCommit(LedgerCity):
            async def say(self, to, text, *, status=None):
                if to == LEDGER and text.startswith("commit "):
                    sent = msg(AGENT, text, to)
                    self.chat.append(sent)
                    return sent.msg_id
                return await super().say(to, text, status=status)
        city, offers = await self.offered(UncertainCommit())
        await citizen_says(city, f"confirm offer_id={offers[0]['offer_id']} consent_ref=c-1")
        await citizen_says(city, "cancel reason=changed_mind")
        self.assertTrue(any("reason=commit_pending" in t for t in city.to_citizen()))
        fc._FAMILY_CASES.clear()
        await citizen_says(city, "cancel reason=changed_mind")
        self.assertEqual(len(city.to_ledger("commit")), 1)
        self.assertEqual(len(city.to_ledger("hold")), 1)
        self.assertFalse(any(t.startswith("booking.receipt") for t in city.to_citizen()))

    async def test_uncertain_write_error_is_not_retried_or_claimed_cancelled(self):
        class CommitTimeout(LedgerCity):
            attempts = 0
            async def say(self, to, text, *, status=None):
                if to == LEDGER and text.startswith("commit "):
                    self.attempts += 1
                    raise CapError("UPSTREAM_TIMEOUT", "uncertain")
                return await super().say(to, text, status=status)
        city, offers = await self.offered(CommitTimeout())
        await citizen_says(city, f"confirm offer_id={offers[0]['offer_id']} consent_ref=c-1")
        await citizen_says(city, "cancel reason=changed_mind")
        self.assertEqual(city.attempts, 1)
        self.assertTrue(any("reason=commit_pending" in t for t in city.to_citizen()))
        self.assertFalse(any(t.startswith("booking.receipt") for t in city.to_citizen()))
        fc._FAMILY_CASES.clear()
        await citizen_says(city, f"confirm offer_id={offers[0]['offer_id']} consent_ref=c-1")
        self.assertEqual(city.attempts, 1)

    async def test_external_confirmation_pending_and_late_receipt_after_withdrawal(self):
        city = LedgerCity()
        partner = "agent:other-consultorio"
        city.chat = [
            msg(CIT, "need.request lang=it", AGENT),
            msg(AGENT, "need.request service_type=family_counseling.gynecology "
                "procedure=CONS-GIN request_id=rq-test", partner),
            msg(AGENT, "confirm offer_id=p-X slot_id=X facility=" + partner
                + f" citizen={CIT} purpose=cross_asst consent_ref=c-x", partner),
        ]
        await citizen_says(city, "cancel reason=changed_mind")
        self.assertTrue(any("reason=commit_pending" in t for t in city.to_citizen()))
        receipt = msg(partner, "booking.receipt offer_id=p-X slot_id=X facility=" + partner
                      + " booking_id=B-X ledger_ref=L-X", AGENT)
        city.chat.append(receipt)
        city.inbound = receipt
        await fc._family(city, receipt, list(city.chat))
        self.assertTrue(any("booking.receipt booking_id=B-X" in t for t in city.to_citizen()))
        self.assertTrue(any("reason=already_booked" in t for t in city.to_citizen()))
        self.assertEqual(city.to_ledger("hold"), [])

    async def test_partner_search_includes_english_and_no_citizen_data(self):
        city = LedgerCity()
        inbound = msg(CIT, "need.request lang=en", AGENT)
        city.chat = [inbound]
        city.inbound = inbound
        state = fc._FamilyCase()
        state.rows = [inbound]
        await fc._family_send(city, state, "agent:p", "need.request procedure=CONS-GIN request_id=rq-1")
        request = next(row.text for row in city.chat if row.recipient == "agent:p")
        self.assertIn("lang=en", request.split())
        self.assertNotIn(CIT, request)

    async def test_rejected_hold_is_not_retried_after_restart(self):
        class RejectedHold(LedgerCity):
            attempts = 0
            async def say(self, to, text, *, status=None):
                if to == LEDGER and text.startswith("hold "):
                    self.attempts += 1
                    raise CapError("NO_CAPACITY", "rejected")
                return await super().say(to, text, status=status)
        city, offers = await self.offered(RejectedHold())
        command = f"confirm offer_id={offers[0]['offer_id']} consent_ref=c-1"
        await citizen_says(city, command)
        fc._FAMILY_CASES.clear()
        await citizen_says(city, command)
        self.assertEqual(city.attempts, 1)
        self.assertEqual(city.to_ledger("commit"), [])

    async def test_rejected_time_gets_new_options_without_booking(self):
        city, offers = await self.offered()
        before = len(city.to_citizen())
        await citizen_says(city, f"cancel offer_id={offers[0]['offer_id']} reason=deadline")
        batches = [t for t in city.to_citizen()[before:] if fc._offer_rows(t)]
        self.assertEqual(len(batches), 1)
        self.assertNotIn(offers[0]['slot_id'], {o['slot_id'] for o in fc._offer_rows(batches[0])})
        self.assertEqual(city.to_ledger("hold"), [])

    async def test_citizen_can_restart_after_withdrawing(self):
        city, _ = await self.offered()
        await citizen_says(city, "Ho cambiato idea, non prenotare")
        await citizen_says(city, "Vorrei cercare un nuovo appuntamento")
        latest = next(t for t in reversed(city.to_citizen()) if fc._offer_rows(t))
        offer = fc._offer_rows(latest)[0]
        await citizen_says(city, f"confirm offer_id={offer['offer_id']} consent_ref=c-new")
        self.assertEqual(len(city.to_ledger("commit")), 1)


if __name__ == "__main__":
    unittest.main()


class SilentLedger(LedgerCity):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    async def history(self, peer=None, *, need_id=None):
        self.reads += 1
        return list(self.chat)


class PacedLedgerWaits(unittest.IsolatedAsyncioTestCase):
    """The live Ledger answers hold/commit in 15-20 s (Sat morning): 3 reads gave up after 3 s
    ("family.write.stopped reason=ledger_timeout"). Wait ~20 s, one read per second (the
    hospital waits 40 s this way live with renew/ack 200, no 422)."""

    async def test_silent_ledger_is_read_at_most_twenty_times(self):
        city = SilentLedger()
        found = await fc._await_line(city, "N-1", [], "held", "slot_id=S-1", op="hold")
        self.assertIsNone(found)
        self.assertLessEqual(city.reads, 20)
        self.assertGreaterEqual(fc._PULL_TRIES * max(fc._LIVE_PULL_PAUSE, 0), 15)

    async def test_slow_ledger_hold_still_books(self):
        class SlowLedger(LedgerCity):
            """hold/commit answers appear only on the 10th history read after the write."""
            def __init__(self):
                super().__init__()
                self.pending = []
                self.reads = 0
            async def say(self, to, text, *, status=None):
                if to == LEDGER and text.split()[0] in {"hold", "commit"}:
                    sent = msg(AGENT, text, to)
                    self.chat.append(sent)
                    self.pending.append((self.reads + 10, msg(LEDGER, self._answer(text), AGENT)))
                    return sent.msg_id
                return await super().say(to, text, status=status)
            async def history(self, peer=None, *, need_id=None):
                self.reads += 1
                for due, row in list(self.pending):
                    if self.reads >= due:
                        self.chat.append(row)
                        self.pending.remove((due, row))
                return list(self.chat)
        fc._FAMILY_CASES.clear()
        city = SlowLedger()
        await citizen_says(city, "Buongiorno, vorrei un appuntamento al consultorio")
        offers = [o for text in city.to_citizen() for o in fc._offer_rows(text)]
        await citizen_says(city, f"confirm offer_id={offers[0]['offer_id']} consent_ref=c-1")
        self.assertEqual(len(city.to_ledger("commit")), 1)
        self.assertTrue(any(t.startswith("booking.receipt") for t in city.to_citizen()))


class OfferMargin(unittest.IsolatedAsyncioTestCase):
    """Every message takes ~3.5 real min = 3.5 agenda hours: a slot a few hours ahead is past
    by the time the confirm arrives. Direct offers keep _LEAD (8 agenda hours) of margin."""

    async def test_slot_three_hours_ahead_is_not_offered(self):
        class NearSlot(LedgerCity):
            def _answer(self, text):
                answer = super()._answer(text)
                if text.startswith("world"):
                    answer += (f"\nslot_id=G-0 doctor_id=ASST-1-c1 facility={AGENT} procedure=CONS-GIN "
                               "at=2026-11-23T11:00:00+01:00 day=mon free=1")
                return answer
        fc._FAMILY_CASES.clear()
        city = NearSlot()
        await citizen_says(city, "Buongiorno, vorrei un appuntamento al consultorio")
        offered = {o["slot_id"] for text in city.to_citizen() for o in fc._offer_rows(text)}
        self.assertTrue(offered)
        self.assertNotIn("G-0", offered)

    async def test_expired_choice_gets_new_offers_in_the_same_turn(self):
        class LaterClock(LedgerCity):
            late = False
            def _answer(self, text):
                if text == "agenda" and self.late:
                    return "agenda agenda_now=2026-11-26T08:00:00+01:00"
                answer = super()._answer(text)
                if text.startswith("world"):
                    answer += (f"\nslot_id=G-9 doctor_id=ASST-1-c1 facility={AGENT} procedure=CONS-GIN "
                               "at=2026-11-27T11:00:00+01:00 day=fri free=1")
                return answer
        fc._FAMILY_CASES.clear()
        city = LaterClock()
        await citizen_says(city, "Buongiorno, vorrei un appuntamento al consultorio")
        offers = [o for text in city.to_citizen() for o in fc._offer_rows(text)]
        first = offers[0]["offer_id"]
        city.late = True
        before = len(city.to_citizen())
        await citizen_says(city, f"confirm offer_id={first} consent_ref=c-1")
        after = city.to_citizen()[before:]
        self.assertEqual(city.to_ledger("hold"), [])
        self.assertTrue(any("reason=slot_expired" in t for t in after), after)
        fresh = {o["slot_id"] for t in after for o in fc._offer_rows(t)}
        self.assertEqual(fresh, {"G-9"}, after)


class Level8RedFlags(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fc._FAMILY_CASES.clear()

    async def test_free_text_alarm_escalates_with_112_and_never_books(self):
        for text, english in (("Ho una stretta al petto da stamattina, vorrei un appuntamento", False),
                              ("Non riesco a respirare bene e vorrei parlare con qualcuno", False),
                              ("I fainted yesterday and I would like an appointment please", True)):
            fc._FAMILY_CASES.clear()
            city = LedgerCity()
            message = msg(CIT, text, AGENT)
            city.chat.append(message)
            city.inbound = message
            with mock.patch.object(fc, "role_of", return_value="family_counseling"):
                await fc.main(SimpleNamespace(is_emergency=False, summary=text), city)
            said = city.to_citizen()
            self.assertTrue(said and said[-1].startswith("escalate.emergency reason=red_flag"), text)
            self.assertIn("112", said[-1])
            self.assertEqual(english, "call 112" in said[-1])
            self.assertEqual(city.to_ledger("hold"), [])

    async def test_ordinary_request_is_not_an_emergency(self):
        city = LedgerCity()
        await citizen_says(city, "Buongiorno, vorrei un appuntamento al consultorio per un colloquio")
        self.assertFalse(any(t.startswith("escalate.emergency") for t in city.to_citizen()))



class ModelCity(LedgerCity):
    def __init__(self, answer):
        super().__init__()
        self.answer = answer
        self.asked = 0

    async def llm(self, messages, **kwargs):
        self.asked += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return {"content": self.answer}


class Level8EverydayWords(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fc._FAMILY_CASES.clear()
        fc._ASKED_MODEL.clear()

    async def run_main(self, city, text):
        message = msg(CIT, text, AGENT)
        city.chat.append(message)
        city.inbound = message
        with mock.patch.object(fc, "role_of", return_value="family_counseling"):
            await fc.main(SimpleNamespace(is_emergency=False, summary=text), city)
        return [t for t in city.to_citizen() if t.startswith("escalate.emergency")]

    async def test_everyday_words_escalate_without_the_model(self):
        for text in ("Mi manca l\u2019aria e ho il fiato corto, vorrei un colloquio",
                     "Ieri ho perso i sensi in cucina",
                     "Le gambe non mi reggono più da stamattina",
                     "Ho la bocca storta e non riesco a parlare bene",
                     "Non ce la faccio più, voglio farla finita",
                     "I'm short of breath and I collapsed this morning"):
            fc._FAMILY_CASES.clear()
            city = ModelCity("NO")
            self.assertTrue(await self.run_main(city, text), text)
            self.assertEqual(city.asked, 0, text)
            self.assertEqual(city.to_ledger("hold"), [])

    async def test_model_catches_what_the_patterns_miss(self):
        city = ModelCity("SI")
        said = await self.run_main(city, "Sono incinta e perdo tantissimo sangue con un dolore fortissimo")
        self.assertTrue(said)
        self.assertEqual(city.asked, 1)

    async def test_ordinary_worries_never_escalate_nor_ask_the_model(self):
        for text in ("Ho molta ansia e litighiamo spesso in famiglia, vorrei un colloquio",
                     "Vorrei informazioni sulla contraccezione",
                     "Mio figlio adolescente non mi parla più"):
            fc._FAMILY_CASES.clear()
            city = ModelCity("SI")
            self.assertEqual(await self.run_main(city, text), [], text)
            self.assertEqual(city.asked, 0, text)

    async def test_model_no_or_failure_keeps_the_normal_path(self):
        for answer in ("NO", TimeoutError()):
            fc._FAMILY_CASES.clear()
            fc._ASKED_MODEL.clear()
            city = ModelCity(answer)
            said = await self.run_main(city, "Ho un po' di mal di testa, vorrei un appuntamento")
            self.assertEqual(said, [])
            self.assertEqual(city.asked, 1)


class SharedLedgerReads(unittest.IsolatedAsyncioTestCase):
    """Sat 21/11: every consultorio case waited on the Ledger for its own agenda and slot list
    (15-20+ s each); turns ran long and other citizens' messages were "held while running".
    A second case right after the first reuses those answers instead of waiting again."""

    def setUp(self):
        self._old = (fc._SHARED_AGENDA_FOR, fc._SHARED_WORLD_FOR)
        fc._SHARED_AGENDA_FOR, fc._SHARED_WORLD_FOR = 120.0, 60.0

    def tearDown(self):
        fc._SHARED_AGENDA_FOR, fc._SHARED_WORLD_FOR = self._old
        fc._SHARED_READS.clear()

    async def test_second_case_offers_without_asking_the_ledger_again(self):
        from city_sdk.city import Message as _M

        class TwoNeeds(LedgerCity):
            def __init__(self):
                super().__init__()
                self.need = "N-1"
            async def say(self, to, text, *, status=None):
                sent = _M(type="chat", sender=AGENT, msg_id=f"s-{len(self.chat)}", need_id=self.need,
                          body={"text": text}, recipient=to)
                self.chat.append(sent)
                if to == LEDGER:
                    self.chat.append(_M(type="chat", sender=LEDGER, msg_id=f"l-{len(self.chat)}",
                                        need_id=self.need, body={"text": self._answer(text)},
                                        recipient=AGENT))
                return sent.msg_id
            async def history(self, peer=None, *, need_id=None):
                return [row for row in self.chat if row.need_id == (need_id or self.need)]

        async def ask(city, need, citizen):
            city.need = need
            row = _M(type="chat", sender=citizen, msg_id=f"c-{need}", need_id=need,
                     body={"text": "Buongiorno, vorrei un appuntamento al consultorio"}, recipient=AGENT)
            city.chat.append(row)
            city.inbound = row
            await fc._family(city, row, await city.history(need_id=need))

        fc._FAMILY_CASES.clear()
        fc._SHARED_READS.clear()
        city = TwoNeeds()
        await ask(city, "N-1", CIT)
        await ask(city, "N-2", "citizen:c10")
        second = [row.text for row in city.chat if row.need_id == "N-2" and row.sender == AGENT]
        asked = [t for t in second if t.split()[0] in {"agenda", "world"}]
        self.assertEqual(asked, [], "the second case reuses the first case's agenda and slots")
        offers = [o for t in second for o in fc._offer_rows(t)]
        self.assertTrue(offers, second)

    async def test_old_reads_are_not_reused(self):
        fc._SHARED_READS.clear()
        fc._SHARED_READS["agenda"] = (time.monotonic() - 3600, None)
        self.assertIsNone(fc._shared_read("agenda"))


class ParallelLedgerAsks(unittest.IsolatedAsyncioTestCase):
    """Ledger answers are deliveries (~5 min on Sat 21/11): the first turn asks agenda and the
    first slot page together, so one late delivery round brings both."""

    async def test_silent_ledger_gets_agenda_and_world_in_the_same_turn(self):
        class Silent(LedgerCity):
            async def say(self, to, text, *, status=None):
                sent = msg(AGENT, text, to)
                self.chat.append(sent)
                return sent.msg_id

        fc._FAMILY_CASES.clear()
        fc._SHARED_READS.clear()
        city = Silent()
        await citizen_says(city, "Buongiorno, vorrei un appuntamento al consultorio")
        asked = [t.split()[0] for t in (row.text for row in city.chat
                                        if row.sender == AGENT and row.recipient == LEDGER)]
        self.assertIn("agenda", asked)
        self.assertIn("world", asked)
        self.assertLess(asked.index("world"), len(asked))


class ArrivalWordsWithoutBooking(unittest.IsolatedAsyncioTestCase):
    """Sat 21/11 (000376): "eccomi" / "sono qui" opened a booking request, the consultorio read
    it as a check-in with no booking, answered checkin_pending and never offered a slot."""

    async def test_eccomi_without_a_booking_gets_offers(self):
        fc._FAMILY_CASES.clear()
        city = LedgerCity()
        await citizen_says(city, "Sigh, eccomi di nuovo qui. Vorrei prenotare una visita al consultorio.")
        said = city.to_citizen()
        self.assertFalse(any("checkin_pending" in t for t in said), said)
        self.assertTrue([o for t in said for o in fc._offer_rows(t)], said)

    async def test_arrival_after_a_receipt_is_still_a_checkin(self):
        fc._FAMILY_CASES.clear()
        city = LedgerCity()
        await citizen_says(city, "Buongiorno, vorrei un appuntamento al consultorio")
        offers = [o for t in city.to_citizen() for o in fc._offer_rows(t)]
        await citizen_says(city, f"confirm offer_id={offers[0]['offer_id']} consent_ref=c-1")
        await citizen_says(city, "Eccomi, sono arrivata!")
        self.assertEqual(len(city.to_ledger("checkin")), 1)
