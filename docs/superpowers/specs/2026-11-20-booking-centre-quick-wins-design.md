# Booking centre quick wins: design

Date: 2026-11-20 · Owner: Leo (booking_centre, Huddle task #7) · File: `asst/booking_centre/agent/agent.py` only

## Intent

Make our booking centre score more on the rubric, starting with the fixes that pay off on almost every booking. Cross-ASST booking (and with it P2 and `partner_down`) is deliberately left for the next round.

## Goals

1. **One offer message.** Send every offered slot in a single message, so the citizen chooses among all of them (the rules say a later message doesn't enter a choice already made).
2. **P1 expired prescription.** Ask the citizen to renew (`data.request purpose=prescription_renewal`) instead of going silent, then book the new prescription.
3. **Read the rules.** Ask the Ledger for `rules` and log what comes back.
4. **Hospital down.** When forwarding a `confirm` to a facility fails with `TARGET_NOT_VISIBLE`, tell the citizen `cancel ... reason=facility_unavailable` and offer what's left at other facilities.

## Non-goals

- Cross-ASST booking, P2 consent, `partner_down`: next round.
- Asking the citizen for constraints (`data.request purpose=booking`).
- Touching other agents' copies of the brain (gp, hospital, specialist, family_counseling).

## Anti-goals (a failure even if it "works")

- Any extra Ledger round trip in the normal path. The chain has to answer fast (+1 within ~10 s; first offer within 16 real minutes).
- Booking an expired prescription (−1), a receipt without a commit (−2), or retrying a 4xx.
- Leaving the citizen without a reply ("passive", −1).
- Breaking the flow that works today: prescription check → offers → confirm → facility → receipt.

## Constraints

- Only `asst/booking_centre/agent/agent.py` changes. Stdlib + `city_sdk.city` only. No LLM.
- The Ledger's exact replies for an expired prescription and for `rules` aren't documented (the SDK has no schema for the `prescription` reply). Detection must tolerate several shapes.
- Deploys take about 8 minutes, so ship all four fixes in one deploy.

## Approaches

- **Patch in place (chosen).** Small changes to `_advance_alone`, `_on_confirm`, `_partner_cancel` and the helpers that read our offers back. Keeps the working flow intact.
- Rewrite the booking desk: cleaner, but hours of rebuilding and retesting before Level 2. Rejected.
- LLM-driven offers/rules: slower (loses fast-reply), costs budget, and the judge reads only `key=value` tokens. Rejected.

## Design

### 1. One offer message

- `_advance_alone` builds one message: one line per slot, each line exactly today's `_offer_line(...)` (starts with `offer`, has `offer_id=` and `slot_id=`). Lines are joined with `\n`, followed by one short bilingual sentence.
- **Up to 6 slots**, trimmed so the message stays under the 2000-character limit.
- **Spread, not just earliest** *(my call)*: pick across different doctors and days first, then fill by time. The citizen's hidden preferences (days, gender) are more likely to be met by a varied set.
- **Citizen `days`** *(my call)*: if the citizen's request carries `days=` (the doc says lists use `/`), rank slots on those days first. Never exclude the others.
- Readers become per-line. A new `_offers_in(text)` returns one dict per `offer` line. `_offered`, `_withdrawn` and `_our_offer` use it, so a `confirm offer_id=of-S-14` finds the right slot even when it's the third line.
- The waiting list for priority P is unchanged (`offer offer_id=wl-... waiting_list=true`).

### 2. P1 expired prescription

- **Prescription reply tied to the number.** Today the brain uses the latest `prescription` reply from the Ledger, whatever number it was for. New: `_rx_reply(chat, number)` uses the latest Ledger `prescription` line that came after our latest `prescription prescription_number=<number>` query. That way a renewed number gets its own check.
- **Expired is any of:** reply `state=` present and not `issued`; reply has `valid=false`; any token containing `EXPIRED` in a Ledger line about this number; or a facility `cancel` whose reason contains `EXPIRED`.
- **On expired:** send the citizen `data.request purpose=prescription_renewal fields=prescription_number` plus a short sentence (go to your GP for a renewal, then send me the new number). Ask **once per expired number**. Send no offers for that number.
- **When the citizen comes back** with a new `prescription_number`, the normal flow runs for it: verify, then offer. Offers and "already offered" tracking are per prescription number, so the new number gets a full fresh offer.
- **Depends on others:** the GP must handle a renewal request that carries only `prescription_number` (Asca, task #8). The hospital currently turns every Ledger refusal into `cancel reason=facility_cancelled`, which loses the `PRESCRIPTION_EXPIRED` code. Ask Riccardo to pass the real code through. Until then, our proactive check on the prescription reply is the main signal.

### 3. Read the rules

- On the first pass, alongside the existing `agenda` query, also send `rules` to the Ledger. Never wait for it: it doesn't join the "ready" condition, so no extra round trip.
- Log the raw `rules` and `prescription` replies (`_LOG.info`, first 300 characters), so we learn the real formats from the console logs.
- **P3:** already handled. Slots with `urgent_reserve=true` are skipped for non-U priorities; the Ledger only sets that field while P3 is in force.

### 4. Hospital down

- **New `_send(city, to, text) -> str | None`:** returns the `CapError.code` on failure, `None` on success. `_say` keeps its current behaviour for every other call.
- **In `_on_confirm`, forwarding to the facility:** if the code is `TARGET_NOT_VISIBLE` (or any other 4xx code), never resend. Instead:
  - Send the citizen `cancel offer_id=<id> reason=facility_unavailable` plus a short sentence.
  - Re-offer, as one message, the still-free fitting slots at **other** facilities.
  - If there are none: one message saying no other slot is available right now. Real alternatives come with cross-ASST.
- **No proactive `city.find()` check before offering** *(my call)*: if `find()` ever misses our hospital while it's up, we would stop offering physiatry entirely. React only to the real error.

## Testing

- **Local unit tests** *(my call: `tests/booking_centre/` at the repo root, outside `asst/` so the deploy never sees them)*, run with the city-cli tool's Python against a fake `City` that records `say` calls:
  - one message with N `offer` lines;
  - `confirm` on the 3rd offer resolves the right slot;
  - expired reply → one renewal request and no offers;
  - renewed number → a fresh offer;
  - `TARGET_NOT_VISIBLE` on forward → cancel + re-offer, with no resend.
- **Then:** `city run booking_centre` smoke test, then one deploy (Leo's go).

## Coordination

- Huddle: `lock_paths asst/booking_centre/**` while editing.
- Message Asca (GP renewal dependency) and Riccardo (hospital should pass the real refusal code).
- Tell Mattia afterwards: family_counseling shares the same 3-offers code path (`_advance_alone`) and can port fix #1.

## Left to the builder

- Exact sentence wording (short, polite, IT/EN).
- How strictly to spread slots across doctors and days.
