# Booking centre quick wins Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** booking_centre sends all offers in one message, handles P1 (expired prescription → renewal), reads `rules`, and tells the citizen when the hospital is unreachable.

**Architecture:** patch in place in `asst/booking_centre/agent/agent.py` (kit care-journey brain). New small pure helpers plus minimal changes to `_advance_alone`, `_on_confirm`, `_partner_cancel`. Tests drive the brain through a fake `City` that records `say` and serves `history`.

**Tech Stack:** Python 3.12, `city_sdk.city`, stdlib `unittest` (IsolatedAsyncioTestCase). Run tests with `~/.local/share/uv/tools/city-cli/bin/python` (has `city_sdk`, no pytest).

**Spec:** `docs/superpowers/specs/2026-11-20-booking-centre-quick-wins-design.md`

## Global Constraints

- Edit only `asst/booking_centre/agent/agent.py` (plus tests under `tests/booking_centre/`). Never touch Dockerfile, city.yaml, card.yaml.
- Stdlib + `city_sdk.city` only. No `city.llm`. No new Ledger round trip in the normal path.
- One citizen message ≤ 2000 characters. Up to 6 offers per message.
- Never retry a failed send. Never send a receipt without a commit.
- Offer line format stays exactly `_offer_line(...)` (act word `offer` first, `offer_id=of-<slot_id>`, `slot_id=`).

## Review Focus

1. Citizen confirms an offer that isn't the first line → must resolve that line's slot (Task 1 test `test_confirm_on_third_offer_forwards_right_slot`).
2. Old prescription reply still in the chat when a renewed number arrives → must not reuse it (Task 2 test `test_rx_line_tied_to_number`).
3. Same expired number seen on several turns → renewal asked once (Task 2 test `test_expired_state_asks_renewal_once_no_offers`).
4. Facility unreachable → exactly one send attempt to it, citizen always gets a reply (Task 3 tests).
5. Citizen `days=` with unknown spellings (Italian names, mixed case, `/` or `,`) → normalised, never crashes (Task 1 test `test_days_normalised`).

---

### Task 1: One offer message

**Files:**
- Modify: `asst/booking_centre/agent/agent.py` (`_offered`, `_withdrawn`, `_our_offer`, `_advance_alone` offer loop at ~807-813)
- Create: `tests/booking_centre/test_quick_wins.py` (loader, fake City, fixtures, tests)

**Interfaces:**
- Produces:
  - `_offers_in(text: str) -> list[dict[str, str]]`: one `fields_of(line)` per line whose first word is `offer` and that has `offer_id=`.
  - `_offered(chat, agent, number: str | None = None) -> set[str]`: slot ids from our offers; when `number` is given, only offers with that `prescription_number` (or none).
  - `_withdrawn(chat, agent) -> set[str]` and `_our_offer(chat, agent, offer_id) -> dict[str, str]`: same contracts as today, per line.
  - `_days(chat) -> set[str]`: citizen `days=` as `{"mon".."fri"}`; accepts `/` or `,`, any case, English or Italian (`lun mar mer gio ven`, full names too).
  - `_pick(slots: list[dict[str, str]], days: set[str], limit: int = 6) -> list[dict[str, str]]`
  - `_offer_block(slots, number: str | None, priority: str | None, lang: str) -> str`
- Test helpers (in the test file): `FakeCity(agent_id, chat, down=set())` with `async say(to, text)` (appends our message to `chat`, raises `CapError("TARGET_NOT_VISIBLE")` when `to in down`), `async history(need_id=None)`, `inbound`, `address=""`; `msg(sender, text, recipient="")`; `booking_chat(rx=..., slots=...)` building: citizen need → our `prescription` + `agenda` + `rules` queries → Ledger rx, agenda (`agenda_now=2026-11-23T08:00:00+01:00`), our `world` query → Ledger `slots` (page=1/1). Set `module._PULL_PAUSE = 0`.

`_pick` algorithm (not determined by tests alone):
```python
order = sorted(slots, key=lambda s: (s.get("day") not in days if days else False, s["at"], s["slot_id"]))
first, rest, seen = [], [], set()
for s in order:
    k = (s["doctor_id"], s.get("day", ""))
    (rest if k in seen else first).append(s); seen.add(k)
return (first + rest)[:limit]
```
`_offer_block`: add `_offer_line` lines while `len("\n".join(lines + [sentence])) <= 2000`; sentence `it`: "Ecco gli appuntamenti disponibili: mi indichi l'offer_id che preferisce." otherwise: "Here are the available appointments: tell me the offer_id you prefer."

- [ ] **Step 1: Write failing tests**: `test_offers_in_parses_each_line` (3 lines + sentence → slot ids `S-1,S-2,S-3`), `test_our_offer_finds_third_line`, `test_pick_spreads_doctors_and_days` (4×h1-mon + h2-wed + h3-tue → first three picks have 3 distinct doctors), `test_pick_prefers_citizen_days` (`days={"wed"}` → first pick day `wed`), `test_days_normalised` (`days=Mercoledì/FRI` → `{"wed","fri"}`; `days=` garbage → `set()`), `test_offer_block_under_2000_chars` (10 slots → `len <= 2000`, ≤ 6 `offer ` lines), `test_advance_sends_one_message_with_all_slots` (exactly 1 `say` to citizen, offer lines == min(6, fitting slots)), `test_confirm_on_third_offer_forwards_right_slot` (citizen `confirm offer_id=<3rd> consent_ref=c-1` → `confirm` to facility with that `slot_id=`).
- [ ] **Step 2: Run, expect FAIL** `cd /Users/leo/MACH_tartufi && ~/.local/share/uv/tools/city-cli/bin/python -m unittest tests.booking_centre.test_quick_wins -v` → AttributeError `_offers_in`.
- [ ] **Step 3: Implement** the Produces list; in `_advance_alone` replace the `for slot in chosen[:3]` loop with one `_say` of `_offer_block(_pick(chosen, _days(chat)), number, priority, _lang(chat))`; on success add every picked slot id to `sent.offers` (and `sent.again` when `replacing`).
- [ ] **Step 4: Run, expect all PASS.** Also `python3 -I -m py_compile asst/booking_centre/agent/agent.py`.
- [ ] **Step 5: Commit** `feat(booking_centre): all offers in one message`

### Task 2: P1 renewal + rules

**Files:** Modify `asst/booking_centre/agent/agent.py` (`_advance_alone` verification loop ~725-761, `_partner_cancel`); Test: same file.

**Interfaces:**
- Consumes: Task 1 helpers, `FakeCity`, `booking_chat`.
- Produces:
  - `_rx_line(chat, agent, number: str) -> Message | None`: first Ledger row after our latest `prescription prescription_number=<number>` query whose first word is `prescription`, or `refused` with `prescription` in its words; skip rows naming a different `prescription_number=`. No query → a Ledger `prescription` row naming `number`, else None.
  - `_p1_active(chat, now: datetime | None) -> bool`: latest Ledger `rules` row mentions `P1` (word or `rule_id=P1`); if it has `effective_agenda=` later than `now`, False.
  - `_expired(text: str, chat) -> bool`: True if `"expir" in state.lower()`, `valid=false`, `"EXPIRED" in text.upper()`, or (`_p1_active`) and an issue time (`issued_at`/`issued`/`issued_agenda`) ≤ now − 2 days. Other non-`issued` states keep today's behaviour (stop, no renewal).
  - `async _ask_renewal(city, citizen: str, number: str, lang: str, chat, agent) -> None`: once per number (dedupe on our `data.request` to citizen containing `purpose=prescription_renewal` and `prescription_number=<number>`). Text: `data.request purpose=prescription_renewal fields=prescription_number prescription_number=<number>` + `it` "La ricetta risulta scaduta: chieda il rinnovo al suo medico di base e poi mi mandi il nuovo prescription_number." / en "Your prescription has expired: please ask your GP to renew it, then send me the new prescription_number."
- Wiring in `_advance_alone`: `number = _number(chat)` first; use `_rx_line` instead of `_ledger(chat, "prescription")`; query condition = no `_rx_line` yet (dedupe only on our rows **to LEDGER** naming the number); also send `rules` once (`_exact(chat, agent, "rules")`) next to `agenda`, never waited on; wait condition `_rx_line(rows, agent, number) is not None and _has_agenda(rows)`; right after the rx row exists and before the P branch: `if fixed is None and _expired(row.text, chat): await _ask_renewal(...); return`. `_sent` key becomes `f"{key}|{number}"`, `taken` uses `_offered(chat, agent, number)`. Log `care.ledger` with the first 300 chars of the rx row and the rules row each turn they are present.
- `_partner_cancel`: when `reason` contains `EXPIR` (any case): cancel to citizen with `reason=prescription_expired`, `_ask_renewal`, no re-offer.

- [ ] **Step 1: Write failing tests**: `test_rules_sent_with_agenda` (fresh need → our Ledger messages include `rules`, `agenda`, `prescription prescription_number=…`), `test_rx_line_tied_to_number`, `test_expired_state_asks_renewal_once_no_offers` (`state=expired`, run `_advance_alone` twice → 1 renewal, 0 offers), `test_expired_token_in_refusal` (`refused prescription prescription_number=… reason=PRESCRIPTION_EXPIRED`), `test_p1_rules_and_issue_date` (rules `rule_id=P1 effective_agenda=2026-11-20T08:00:00+01:00`, rx `state=issued issued_at=2026-11-20T09:00:00+01:00` → renewal), `test_renewed_number_gets_fresh_offer` (new number + its issued reply → one offer message with `prescription_number=<new>`), `test_partner_cancel_expired_asks_renewal`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** the Produces list and wiring.
- [ ] **Step 4: Run all tests, expect PASS** (Task 1 tests too). py_compile.
- [ ] **Step 5: Commit** `feat(booking_centre): P1 renewal and rules`

### Task 3: Hospital down

**Files:** Modify `asst/booking_centre/agent/agent.py` (`_say` neighbour, `_on_confirm` forward branch ~850-864, `_advance`/`_advance_alone` signature); Test: same file.

**Interfaces:**
- Produces:
  - `async _send(city, to: str, text: str) -> str | None`: `None` on success, `CapError.code` on failure. `_say` unchanged.
  - `_advance(..., avoid: str | None = None)` and `_advance_alone(..., avoid: str | None = None)`: when set, drop slots whose `facility == avoid` and do not page; if nothing is left to offer, send once: it "Al momento non ci sono altri posti disponibili: la struttura è temporaneamente non raggiungibile." / en "No other slot is available right now: the facility is temporarily unreachable."
- `_on_confirm` forward: `code = await _send(city, facility, text)`; on any code: once, `cancel offer_id=<id> reason=facility_unavailable` + it "La struttura non è raggiungibile in questo momento." / en "The facility can't be reached right now." to the citizen, then `await _advance(city, message, chat, fixed=None, replacing=True, avoid=facility)`. Never resend to the facility.

- [ ] **Step 1: Write failing tests**: `test_forward_failure_cancels_and_reoffers_other_facility` (slots at `agent:hosp` and `agent:hosp2`; `down={"agent:hosp"}`; confirm a hosp offer → citizen gets `cancel … reason=facility_unavailable`, then one offer message whose offers are all `facility=agent:hosp2`; exactly 1 send attempt to `agent:hosp`), `test_forward_failure_no_alternative_says_none` (only hosp slots → cancel + the "no other slot" text, no `offer ` lines).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run all tests, expect PASS.** py_compile.
- [ ] **Step 5: Commit** `feat(booking_centre): cancel and re-offer when a facility is unreachable`

### Task 4: Ship check

- [ ] **Step 1:** Full suite + `python3 -I -m py_compile asst/booking_centre/agent/agent.py` → all PASS.
- [ ] **Step 2:** Smoke: `cd asst && printf 'citizen\nbook prescription_number=prescription_number-LOCAL lang=it\n/quit\n' | city run booking_centre` → brain asks the local Ledger (`prescription …`, `agenda`, `rules`), no traceback.
- [ ] **Step 3:** Huddle: `release_paths`, note on task #7, messages to Asca (GP renewal), Riccardo (pass real refusal code), Mattia (port fix #1). Deploy only on Leo's go.
