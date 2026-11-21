"""Rule P2 (live since Sat 21/11 12:47): a hold for another ASST's citizen needs the cross_asst
consent. MACH-1.3 partners send the booking consent as consent_ref and the P2 one as
cross_asst_consent_ref (that one wins): our hold and commit must carry it.

    ~/.local/share/uv/tools/city-cli/bin/python -m unittest tests.facility.test_cross_consent -v
"""

from __future__ import annotations

import unittest

from city_sdk.city import LEDGER

from tests.facility.test_slot_taken import SLOT, GatedCity, _load, msg

PARTNER = "agent:partner-bc"


class _CrossConsent:
    module = None
    agent = ""

    def setUp(self) -> None:
        self.module._SLOT_LOCKS.clear()
        self.module._SLOT_OWNER.clear()

    async def test_partner_confirm_holds_and_commits_with_the_cross_asst_consent(self) -> None:
        city = GatedCity(self.agent)
        city._need = "N-P"
        confirm = msg(PARTNER, f"confirm mach=1.3 offer_id=p-{SLOT} slot_id={SLOT} citizen=citizen:far "
                               f"consent_ref=cons-far-booking cross_asst_consent_ref=cx-far-1 "
                               f"facility={self.agent} request_id=ring-1", "N-P", self.agent)
        city.chats["N-P"] = [confirm]
        await self.module._on_confirm(city, confirm, [confirm], hold_self=False)
        holds = city.holds("N-P")
        self.assertEqual(len(holds), 1)
        self.assertIn("consent_ref=cx-far-1", holds[0].split())
        held = msg(LEDGER, f"held hold_id=bk-p1 slot_id={SLOT} fence=1", "N-P", self.agent)
        city.chats["N-P"].append(held)
        await self.module._facility(city, held, list(city.chats["N-P"]), reports=False, hold_self=False)
        commits = [t for n, to, t in city.sent if n == "N-P" and to == LEDGER and t.startswith("commit ")]
        self.assertEqual(len(commits), 1)
        self.assertIn("consent_ref=cx-far-1", commits[0].split())

    async def test_plain_consent_still_works_for_our_own_booking_centre(self) -> None:
        city = GatedCity(self.agent)
        city._need = "N-O"
        confirm = msg("agent:h3n6kfd18r3g", f"confirm offer_id=of-{SLOT} slot_id={SLOT} citizen=citizen:a "
                                            f"consent_ref=cons-a-booking facility={self.agent}", "N-O", self.agent)
        city.chats["N-O"] = [confirm]
        await self.module._on_confirm(city, confirm, [confirm], hold_self=False)
        self.assertIn("consent_ref=cons-a-booking", city.holds("N-O")[0].split())


class HospitalCrossConsent(_CrossConsent, unittest.IsolatedAsyncioTestCase):
    module = _load("hospital")
    agent = "agent:xtj1dk9x3qej"


class SpecialistCrossConsent(_CrossConsent, unittest.IsolatedAsyncioTestCase):
    module = _load("specialist")
    agent = "agent:q18es2d82f8r"


if __name__ == "__main__":
    unittest.main()
