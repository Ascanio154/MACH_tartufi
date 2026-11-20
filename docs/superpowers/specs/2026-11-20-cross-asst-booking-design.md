# Cross-ASST booking: design

Date: 2026-11-20 · Owner: Leo (Huddle task #16) · Branch: `cross-asst` (worktree `../MACH_tartufi-xasst`)
Files: `asst/booking_centre/agent/agent.py` (booking side), `asst/hospital/agent/agent.py` and `asst/specialist/agent/agent.py` (facility side, Riccardo's files: delivered as a patch, applied with his OK).

## Intent

The ASSTs form a ring and each one lacks something another has (kit rules). The 22:29 "Booking Center" dispatch says the booking centre should look at its neighbours when it's full ("Se da me è pieno, guardo dai vicini") and ask for consents. Today nothing cross-ASST exists in any of our brains or in the kit example: when our slots run out, the citizen gets nothing (−1 when the answer set isn't empty, plus "passive" −1).

Two parts:
1. **Booking side**: when our ASST has no fitting slot, get slots from partner facilities in other ASSTs and book them, respecting rule P2 and the data rules.
2. **Facility side**: our hospital and specialist answer other ASSTs' booking centres, so partner citizens can book with us (each honoured check-in +2 for us, collaboration +1).

## Goals

- Booking: our slots exhausted (or the facility unreachable) → partner slots offered in ONE offer message → `consent purpose=cross_asst` only when the citizen picks a partner slot → `confirm` forwarded → receipt forwarded.
- Facility: an incoming `need.request` from another agent → ONE `offer` message with our fitting slots (or a `cancel` with a reason).
- No citizen data leaves our ASST before the cross_asst consent.

## Non-goals

- Asking partners when we still have fitting slots (approach B, rejected).
- The consultorio: it is direct access, partner booking centres never book it.
- Relaxed alternatives when nobody anywhere has a slot (separate improvement).

## Anti-goals (failure even if it "works")

- Citizen data in the search request (−2 audit). The `need.request` carries ONLY `service_type`, `procedure`, `priority`, `request_id`.
- A cross-ASST hold without consent (−1, `CONSENT_MISSING`).
- Slower normal bookings: nothing changes while our own slots fit.
- Retrying a 4xx or writing to an agent the Index didn't return (kit rule).
- Breaking today's flow: local offers, P1 renewal, hospital-down cancel, receipts.

## Constraints

- Brain only: `city_sdk.city` + stdlib. `city.find(query)` returns `list[Peer]` (`agent_id`, `district`, `role`, `capabilities`, …), unordered, without the caller; it may raise `CapError`.
- Messages from agents open a turn for us; Ledger replies don't (read them back from history, as the code already does).
- One citizen message ≤ 2000 characters; up to 6 offers.
- Every ASST has the same doctor keys (h1–h3, s1–s2, c1); only the rare language (`*` in `_DOCTORS`) differs per ASST.
- Deploys are done by the deploy tab only; this branch is pushed and handed over.

## Approaches

- **A. Partners only when we have nothing; consent when the citizen picks a partner slot (chosen).** Normal bookings stay as fast as today; data rules hold by construction. Cost: when we have some slots but the citizen's ideal one is at a partner, we offer ours only (+1 instead of +2).
- B. Ask partners on every booking and merge: best S_pref, but every booking waits for partner replies; most partners run the kit brain and never answer. Rejected.
- C. Ask consent before searching: an extra citizen round trip before any offer, even when no partner has a slot; not required since the search carries no citizen data. Rejected.

## Design: booking side (`asst/booking_centre/agent/agent.py`)

### When it triggers
In `_advance_alone`, after our own `fitting` slots come back empty:
- **No more local pages** (last page or `_MAX_PAGE` reached), `fixed is None` (not consultorio), priority not `P` (waiting list stays as is) → ask partners.
- **Facility unreachable** (`avoid` set) and nothing else fits locally → ask partners (excluding `avoid`), and send the existing "no other slot" message only if no partner can be asked.

### Finding partners: `_partner_peers(city, procedure, ours, district) -> list[Peer]`
- Capability per procedure: `VIS-FIS → hospital.physiatry`, `VIS-CARD`/`CTRL-CARD → specialist_care.cardiology`. Unknown procedure → no partners.
- `city.find(<capability>)`; `CapError` → `[]`.
- Keep peers whose `capabilities` contain the capability and whose `agent_id` is not ours (`ours` = every `facility=` in our world slot lines, plus `avoid`).
- Prefer the citizen's district (`area.district` from the citizen's request; our own choice, nothing is sent), then the rest. At most **3** peers (`_MAX_PARTNERS`).

### Asking: `_ask_partners(...)`
- Once per case: `request_id = x-<need_id>`; skip if we already sent a `need.request` with that `request_id`.
- To each peer: `need.request service_type=<capability> procedure=<procedure> priority=<priority> request_id=<id>`, nothing else. Send with `_send`; a failure skips that peer, never retried.
- If at least one request went out: tell the citizen once (IT/EN by `lang`): "Nella nostra ASST non ci sono posti adatti: sto chiedendo alle ASST vicine." (*my call*: avoids silence, which reads as passive).
- No partner at all: today's behaviour (nothing / "no other slot" when the facility is down).

### Partner offers arrive: new `_desk` branch → `_on_partner_offer`
- Trigger: sender `agent:` and act `offer`; under the same per-need gate as `_advance`.
- Parse every offer in the message, robust to one-per-line or several on one line (split on `offer_id=` tokens). Each needs `offer_id`, `slot_id`; `facility` defaults to the sender.
- Keep a slot only if: its time (`agenda_date` or `at`) is after `now + _LEAD` and inside the priority window; the doctor speaks the citizen's language and matches a gender preference (via `_DOCTORS` keys; unknown key or a partner's rare language → drop); not already offered for this prescription number.
- Rank with `_pick` (citizen days first, spread over doctors/days), max 6, ONE message via the same line format as local offers (mapping `agenda_date`/`agenda_day`), with `prescription_number` and `priority`. Lead sentence: "Questi posti sono in un'altra ASST della rete." / "These appointments are at a partner ASST." No other attribute is added (no false offer attribute).
- Nothing fits → do nothing (another partner may still answer).

### The citizen picks a partner slot: `_on_confirm`
- A slot is cross-ASST when its offer's `facility` is not in `ours`.
- If the citizen already gave a cross_asst consent in this case (`consent … purpose=cross_asst consent_ref=…`) → forward `confirm` with **that** `consent_ref`.
- Otherwise: send `consent purpose=cross_asst offer_id=<id>` + "Per prenotare in un'altra ASST mi serve il suo consenso a condividere i dati necessari con quella struttura." / EN; remember the pick (it's in our own message); return. Nothing is sent to the partner yet.

### The citizen answers: new `_desk` branch → `_on_cross_consent`
- Trigger: a citizen message after our open `consent purpose=cross_asst` ask.
- Has `consent_ref` → forward `confirm offer_id slot_id citizen consent_ref facility prescription_number` to the partner facility (the existing forward code, failure path included).
- No `consent_ref` (plain-language no) → `cancel offer_id=<id> reason=consent_declined` + short sentence; no data sent.

### Afterwards (existing code, unchanged)
- `booking.receipt` from the partner → `_forward_receipt` to the citizen.
- `cancel` from the partner → `_partner_cancel` (P1 renewal if `EXPIR`, else re-offer).
- Partner unreachable on forward (`partner_down`, `TARGET_NOT_VISIBLE`) → existing cancel + `_advance(avoid=…)`, which now asks the remaining partners.

## Design: facility side (hospital, specialist)

`_answer_partner(city, message, chat)`, added to `_facility` as a branch before `confirm`:
- Trigger: sender `agent:`, act `need.request`, has `procedure` and `request_id`.
- Read our world for that procedure (`world procedure=<p> page=1`) and `agenda`, then read them back from history (`_until`), exactly like the booking side does.
- Slots: our own facility (`facility == city.agent_id`), free, after `now + _LEAD`, inside the priority window (U today, B 2 days, D 5 days; P → the earliest), never `urgent_reserve` for non-U. No language or gender filter (the request carries no citizen data; the partner's booking centre filters).
- Spread over doctors/days, max 6. ONE message to the sender: `offer offer_id=p-<slot> slot_id=… doctor_id=… facility=<us> procedure=… agenda_date=… agenda_day=… priority=… request_id=<echo>` per line.
- No slot → `cancel request_id=<id> reason=no_slots`.
- Answer once per `request_id`.
- The partner's later `confirm` goes through the existing `_on_confirm` (sender is an agent → we hold, commit with its `consent_ref`, send the receipt back).

Self-contained: its own small helpers, no change to shared functions, so it can be pasted into both facility files unchanged.

## Testing

- `tests/booking_centre/test_cross_asst.py` with the existing fake `City`, extended with `find()`:
  - no local slot → `need.request` to ≤3 non-own peers, district first, with ONLY the 4 search tokens;
  - local slots fit → no partner request;
  - partner offer → one citizen message, filtered (deadline, language), max 6;
  - confirm of a partner slot → `consent purpose=cross_asst`, nothing to the partner;
  - consent with ref → `confirm` to the partner with that ref; plain "no" → `cancel reason=consent_declined`;
  - `find` raising `CapError` → no crash, today's behaviour;
  - forward fails → cancel + the remaining partners asked.
- `tests/facility/test_answer_partner.py` against the hospital brain: request → one offer message from our facility's slots inside the deadline, no U-reserve for B; no slot → `cancel reason=no_slots`; answered once.
- All existing booking tests keep passing; local `city run` smoke for both agents.

## Coordination

- Huddle task #16; lock on `asst/booking_centre/**`, `tests/booking_centre/**`.
- Facility patch to Riccardo (hospital + specialist owner): he applies it, or tells me to commit it.
- Pushed branch → the deploy tab merges and deploys, booking centre first.

## Settled during the builder read

- **Consent act word:** `consent purpose=cross_asst …`. Kit rules line 124 allow `data.request purpose=…` or `consent purpose=…`; P2 says "un `consent` con scopo `cross_asst`"; the specialist already asks `consent purpose=report_sharing` and the citizen answers `consent … consent_ref=…`.
- **Which consent ref goes to the partner:** the cross_asst one (P2: a cross-ASST hold without that reference is refused `CONSENT_MISSING`).
- **Ask consent even when P2 is not in force** *(my call)*: the citizen data goes to another ASST either way; it costs one citizen turn and matches the Token hint ("chiedo i consensi").
- **No in-turn waiting for partner replies** *(my call)*: a partner's `offer` opens its own turn; the asking turn ends after sending.
- **Partner offers go to the citizen once per prescription number** *(my call)*: the citizen chooses in the turn of the first offer message, later messages don't enter that choice. A partner `cancel` re-opens it through the existing re-offer path.
- **`area.district`** is compared with `Peer.district` as integers; missing or unparsable → no preference.
- **Facility offer lines** also carry `expiry=<agenda_date>`, like local offers.

## Left to the builder

- Exact sentence wording (short, polite, IT/EN).
- Names of small private helpers.
