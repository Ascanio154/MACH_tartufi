"""Specialist after check-in: the report reaches the Ledger even when the citizen stays silent.

    ~/.local/share/uv/tools/city-cli/bin/python -m unittest tests.facility.test_specialist_report -v
"""

from __future__ import annotations

import unittest

from city_sdk.city import LEDGER

from tests.facility.test_answer_partner import SPEC, FacilityCity, msg, specialist

CITIZEN = "citizen:W-1"
BK = "bk-1"


class SilentCitizen(unittest.IsolatedAsyncioTestCase):
    async def test_report_is_filed_unshared_and_without_follow_up(self) -> None:
        specialist._CONSENT_WAIT = 0
        specialist._FILING.discard(BK)
        chat = [
            msg(CITIZEN, f"checkin booking_id={BK}", recipient=SPEC),
            msg(SPEC, f"checkin booking_id={BK} citizen={CITIZEN}", recipient=LEDGER),
            msg(LEDGER, f"checked_in booking_id={BK}", recipient=SPEC),
        ]
        city = FacilityCity(SPEC, chat)
        await specialist._on_checked_in(city, chat[-1], chat, reports=True)
        to_citizen = city.sent_to(CITIZEN)
        self.assertTrue(to_citizen[0].startswith(f"checkin booking_id={BK}"))
        self.assertTrue(to_citizen[1].startswith("consent purpose=report_sharing"))
        self.assertEqual(city.sent_to(LEDGER), [f"report booking_id={BK} outcome_code=VISIT"])


if __name__ == "__main__":
    unittest.main()
