"""Consultorio: delayed Ledger replies must respect the citizen's latest choice."""

from __future__ import annotations

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


class FewHistoryReads(unittest.IsolatedAsyncioTestCase):
    """Leo: many history reads in one turn let the lease expire (renew/ack 422)."""

    async def test_waiting_for_a_silent_ledger_reads_history_at_most_three_times(self):
        city = SilentLedger()
        found = await fc._await_line(city, "N-1", [], "held", "slot_id=S-1", op="hold")
        self.assertIsNone(found)
        self.assertLessEqual(city.reads, 3)
        city.reads = 0
        await fc._until(city, "N-1", [], lambda rows: False)
        self.assertLessEqual(city.reads, 3)


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
