# Cross-ASST booking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** booking_centre books at partner ASSTs when ours has no fitting slot (P2 consent at pick time), and our hospital/specialist answer partners' slot requests.

**Architecture:** patch in place in the kit care-journey brain. Booking side: a partner search when local slots are exhausted, a new `_desk` branch for partner offers, a consent step inside `_on_confirm`, a new `_desk` branch for the consent answer. Facility side: one self-contained `_answer_partner` branch in `_facility`.

**Tech Stack:** Python 3.12, `city_sdk.city`, stdlib `unittest` (IsolatedAsyncioTestCase); run with `~/.local/share/uv/tools/city-cli/bin/python` from the worktree root `/Users/leo/MACH_tartufi-xasst`.

**Spec:** `docs/superpowers/specs/2026-11-20-cross-asst-booking-design.md`

## Global Constraints

- Brain only: `city_sdk.city` + stdlib; no LLM; never retry a failed send; write only to agents returned by `city.find`.
- The partner search carries ONLY `service_type`, `procedure`, `priority`, `request_id`. No citizen id, prescription number, language, gender or district goes to a partner before the cross_asst consent.
- Capabilities: `VIS-FIS → hospital.physiatry`; `VIS-CARD`, `CTRL-CARD → specialist_care.cardiology`. At most 3 partners. `request_id = x-<need key>`.
- Consent ask: `consent purpose=cross_asst offer_id=<id>` + sentence; the partner `confirm` carries the cross_asst `consent_ref`.
- One citizen message ≤ 2000 chars, ≤ 6 offers. Facility offer line: `offer offer_id=p-<slot> slot_id doctor_id facility procedure agenda_date agenda_day expiry priority request_id`; no slot → `cancel request_id=<id> reason=no_slots`.
- Existing behaviour unchanged while local slots fit; all 18 tests in `tests/booking_centre/test_quick_wins.py` stay green.
- Booking edits only in `asst/booking_centre/agent/agent.py`; facility edits in `asst/hospital/agent/agent.py` and the identical block in `asst/specialist/agent/agent.py` (Riccardo's files: separate commit, not pushed to `main` without his OK).

## Review Focus

1. Several partners answer at different times → only the first useful answer reaches the citizen (Task 2 `test_second_partner_offer_not_resent`).
2. The citizen re-sends their original request while the consent question is open → not read as a "no" (Task 3 `test_resend_is_not_a_decline`).
3. Partner offers naming unknown doctor keys, past times or another procedure → dropped, no crash (Task 2 `test_partner_offers_filtered`).
4. Garbage or missing `area.district` → no preference, no crash (Task 1 `test_district_garbage_ignored`).
5. A partner `offer` that fits nothing → no citizen message at all (Task 2 `test_partner_offer_nothing_fits`).

---

### Task 1: Ask partners when no local slot fits

**Files:**
- Modify: `asst/booking_centre/agent/agent.py` (constants block, new helpers near `_ledger`, `_advance_alone` tail)
- Create: `tests/booking_centre/test_cross_asst.py`

**Interfaces:**
- Produces:
  - `_PARTNER_CAPABILITY: dict[str, str]`, `_MAX_PARTNERS = 3`
  - `_own_facilities(chat: list[Message]) -> set[str]`: every `facility=` in Ledger `slots` lines.
  - `_district(chat: list[Message]) -> int | None`: citizen `area.district=`, int or None.
  - `async _partner_peers(city, procedure: str, ours: set[str], district: int | None) -> list[Peer]`
  - `async _ask_partners(city, chat, agent: str, citizen: str, procedure: str, priority: str | None, key: str, avoid: str | None = None) -> bool`: True when a request went out now or earlier in the case.
- Test helpers in the new file: import `msg, FakeCity, slot, booking_chat, AGENT, HOSP, CIT, NEED, RX` from `tests.booking_centre.test_quick_wins`; `PartnerCity(FakeCity)` adds `peers: list[Peer]`, `find_error: bool`, `async find(query) -> list[Peer]` (records queries, raises `CapError("UNAVAILABLE")` when `find_error`). Peers: `Peer(agent_id="agent:px1", district=7, role="facility", capabilities=("hospital.physiatry",))` etc.

- [ ] **Step 1: Failing tests**: `test_no_local_slot_asks_partners` (world with only a past slot → `need.request` to ≤3 peers, never `HOSP`; each text's `key=value` tokens are exactly `service_type, procedure, priority, request_id`), `test_district_first` (citizen `area.district=7`; 4 peers, districts 3/7/9/7 → the two district-7 peers are among the 3 asked), `test_local_slots_no_partner_request` (default slots → zero `need.request`), `test_partners_asked_once` (run `_advance_alone` twice → one request per peer), `test_find_error_no_crash` (→ no request, no exception), `test_district_garbage_ignored` (`area.district=abc` → still asks), `test_citizen_told_once` (one non-offer notice to `CIT` containing "ASST").
- [ ] **Step 2: Run, expect FAIL**: `~/.local/share/uv/tools/city-cli/bin/python -m unittest tests.booking_centre.test_cross_asst -v` → AttributeError / assertion failures.
- [ ] **Step 3: Implement** the Produces list. Trigger in `_advance_alone`: when `chosen` is empty, `not taken`, `fixed is None`, `priority != "P"`, and no further page will be asked (`page >= pages or page >= _MAX_PAGE`) → `_ask_partners(...)`. In the `avoid and not chosen` branch: `_ask_partners(..., avoid=avoid)` first; the existing "no other slot" text only when it returns False. Notice texts `_PARTNER_ASK_IT` = "Nella nostra ASST non ci sono posti adatti: sto chiedendo alle ASST vicine." / `_PARTNER_ASK_EN` = "No suitable slot in our ASST: I am asking the nearby ASSTs."
- [ ] **Step 4: Run new + old suites, expect PASS**; `python3 -I -m py_compile asst/booking_centre/agent/agent.py`.
- [ ] **Step 5: Commit** `feat(booking_centre): ask partner ASSTs when no local slot fits`

### Task 2: Offer partner slots to the citizen

**Files:** Modify `asst/booking_centre/agent/agent.py` (`_desk`, new helpers); Test `tests/booking_centre/test_cross_asst.py`.

**Interfaces:**
- Consumes: Task 1 (`_own_facilities`, `PartnerCity`).
- Produces:
  - `_partner_offers_in(text: str, default_facility: str) -> list[dict[str, str]]`: split on `offer_id=` tokens (one or many per line); each needs `offer_id` and `slot_id`; `facility` defaults to `default_facility`.
  - `async _locked(key: str, work: Callable[[], Awaitable[None]]) -> None`: the per-need gate, extracted from `_advance` (which now uses it).
  - `async _on_partner_offer(city, message, chat) -> None`
- Filtering reuses `fitting()`: each partner offer becomes a line `slot_id=… doctor_id=… facility=… procedure=… at=<agenda_date|at> day=<agenda_day|day, normalised via _DAY_KEYS>`; the partner's `offer_id` is kept for our offer line (map by `slot_id`). Message = `_PARTNER_LEAD_IT|EN` ("Questi posti sono in un'altra ASST della rete." / "These appointments are at a partner ASST.") + up to 6 `offer` lines built like `_offer_line` but with the partner `offer_id`. Sent once per prescription number (`_sent(f"{key}|{number}")` mark `partner-offered`).

- [ ] **Step 1: Failing tests**: `test_partner_offer_forwarded` (agent `agent:px1` sends 3 offers on 3 lines → one citizen message, 3 `offer ` lines, `facility=agent:px1`, partner offer_ids kept, `prescription_number=RX`), `test_partner_offers_filtered` (past time, `procedure=VIS-CARD`, unknown doctor key `ASST-9-zz` → all dropped; one valid kept), `test_partner_offers_one_line` (2 offers on one line → 2 offer lines), `test_second_partner_offer_not_resent`, `test_partner_offer_nothing_fits` (→ no citizen message).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**; `_desk` branch: `"offer" in kind and message.sender.startswith("agent:")` → `_locked(key, lambda: _on_partner_offer(...))`, placed after the `cancel` branch.
- [ ] **Step 4: Run all suites, expect PASS**; py_compile.
- [ ] **Step 5: Commit** `feat(booking_centre): offer partner ASST slots in one message`

### Task 3: Cross_asst consent and the partner confirm

**Files:** Modify `asst/booking_centre/agent/agent.py` (`_on_confirm`, `_desk`, new helpers); Test `tests/booking_centre/test_cross_asst.py`.

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces:
  - `_cross_consent_ref(chat: list[Message], citizen: str) -> str`: latest citizen message with `purpose=cross_asst` and `consent_ref=`, else "".
  - `_open_cross_ask(chat: list[Message], agent: str) -> str`: `offer_id` of our latest `consent purpose=cross_asst` ask with no later `confirm` to a facility or `cancel` for that offer_id, else "".
  - `async _forward_confirm(city, message, chat, *, offer_id: str, slot: str, citizen: str, consent: str, facility: str, number: str | None) -> None`: the existing forward + failure path of `_on_confirm`, extracted unchanged.
  - `async _on_cross_consent(city, message, chat) -> None`
- In `_on_confirm`, when not `book_here` and `facility not in _own_facilities(chat)`: `ref = _cross_consent_ref(...)`; ref → `_forward_confirm(..., consent=ref)`; no ref → send once `consent purpose=cross_asst offer_id=<id>` + `_CROSS_ASK_IT|EN` ("Per prenotare in un'altra ASST mi serve il suo consenso a condividere con quella struttura i dati necessari." / "To book at another ASST I need your consent to share the necessary data with that facility.") and return.
- `_desk` branch (before the `_advance` fallback): citizen message, `_open_cross_ask(...)` non-empty, text has no `prescription_number=` → `_on_cross_consent`: `consent_ref=` present → forward the open offer (slot/facility/number from our offer line, citizen = sender); otherwise `cancel offer_id=<id> reason=consent_declined` + "Va bene, non condivido i suoi dati con l'altra ASST." / "All right, I won't share your data with the other ASST."
- Forward failure: the existing path calls `_advance(..., avoid=facility)`, which (Task 1) now asks the remaining partners.

- [ ] **Step 1: Failing tests**: `test_partner_pick_asks_consent` (citizen confirms a partner offer → `consent purpose=cross_asst offer_id=…` to CIT; nothing sent to `agent:px1`), `test_consent_forwards_confirm` (then citizen `consent consent_ref=cx-1 purpose=cross_asst` → `confirm` to `agent:px1` with `consent_ref=cx-1`, `citizen=CIT`, `prescription_number=RX`), `test_plain_no_cancels` (citizen "no grazie" → `cancel … reason=consent_declined`, nothing to the partner), `test_resend_is_not_a_decline` (citizen re-sends `need.request prescription_number=RX` → no cancel), `test_local_pick_unchanged` (confirm of a local offer → forwarded to `HOSP` with no consent question), `test_partner_down_asks_others` (`down={"agent:px1"}` after consent → cancel `reason=facility_unavailable`, a `need.request` to another peer, none to `agent:px1` again).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run all suites, expect PASS**; py_compile.
- [ ] **Step 5: Commit** `feat(booking_centre): cross_asst consent before the partner confirm`

### Task 4: Facilities answer partner requests (Riccardo's files)

**Files:**
- Modify: `asst/hospital/agent/agent.py` (`_facility` branch + new block), `asst/specialist/agent/agent.py` (identical block)
- Create: `tests/facility/__init__.py`, `tests/facility/test_answer_partner.py` (loads the hospital brain; one test loads the specialist brain to prove the block is identical in behaviour)

**Interfaces:**
- Produces: `async _answer_partner(city, message, chat) -> None`; helper `_partner_slots(lines: list[str], *, now: datetime, priority: str | None, facility: str, procedure: str) -> list[dict[str, str]]` (own facility, `free` > 0, after `now + _LEAD`, inside `_DEADLINE_DAYS`/U-today/P-any, no `urgent_reserve` unless U, spread over doctor+day, max 6).
- Branch in `_facility`, before `confirm`: sender `agent:`, first word `need.request`, has `procedure` and `request_id` → `_answer_partner`. Reads `world procedure=<p> page=1` and `agenda` (send if not yet asked in this case, read back with `_until`). Once per `request_id`.

- [ ] **Step 1: Failing tests**: `test_partner_request_gets_one_offer` (request priority B → one message to the requester, every line `offer offer_id=p-… … facility=agent:hosp … request_id=x-1`, times within 2 agenda days, none urgent_reserve), `test_no_slot_cancel` (→ `cancel request_id=x-1 reason=no_slots`), `test_answered_once`, `test_specialist_block_same`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** in hospital, copy the block verbatim into specialist.
- [ ] **Step 4: Run facility + booking suites, expect PASS**; py_compile both files.
- [ ] **Step 5: Commit separately** `feat(facility): answer partner ASST slot requests (for Riccardo)`.

### Task 5: Ship check and hand-off

- [ ] **Step 1:** All suites green: `~/.local/share/uv/tools/city-cli/bin/python -m unittest discover -s tests -t . -v` → OK.
- [ ] **Step 2:** Smoke: `cd asst && printf 'citizen\nbook prescription_number=prescription_number-LOCAL lang=it\n/quit\n' | perl -e 'alarm 60; exec @ARGV' city run booking_centre` → no traceback (same for `hospital`).
- [ ] **Step 3:** Push branch `cross-asst` (with the user's OK); Huddle: release lock, note on task #16; message for the deploy tab (merge Tasks 1–3, deploy booking_centre) and for Riccardo (Task 4 commit to review/apply).
