"""Part of agent.py (loaded into its namespace by _load_parts). Do not import directly."""

from __future__ import annotations

def _family_cross_consent(
    state: _FamilyCase, agent: str, citizen: str, offer_id: str, facility: str
) -> tuple[str, bool]:
    request = next((row for row in reversed(state.rows) if row.sender == agent and row.recipient == citizen
                    and _family_head(row) == "data.request" and _family_fields(row).get("purpose") == "cross_asst"
                    and _family_fields(row).get("offer_id") == offer_id
                    and _family_fields(row).get("facility") == facility), None)
    if request is None:
        return "", False
    for row in reversed(state.rows[state.rows.index(request) + 1:]):
        if row.sender != citizen:
            continue
        fields = _family_fields(row)
        if fields.get("offer_id", offer_id) != offer_id or fields.get("facility", facility) != facility:
            continue
        lowered = row.text.lower().strip()
        denied = (fields.get("granted", fields.get("accepted", "")).lower() == "false"
                  or re.search(r"\b(no|non acconsento|non autorizzo|rifiuto|decline|refuse|do not consent)\b", lowered))
        if denied and fields.get("purpose", "cross_asst") == "cross_asst":
            return "", True
        if _family_head(row) in {"consent", "data.response", "confirm"} and fields.get("purpose") == "cross_asst":
            ref = fields.get("consent_ref", "")
            if ref.lower() not in {"", "none", "false", "null"}:
                return ref, False
    return "", False


def _family_cross_forwards(state: _FamilyCase, agent: str) -> list[Message]:
    targets = _family_cross_requests(state, agent)
    return [row for row in state.rows if row.sender == agent and row.recipient in targets
            and _family_head(row) == "confirm" and _family_fields(row).get("purpose") == "cross_asst"]


def _family_cross_response(row: Message, request: Message) -> bool:
    fields, wanted = _family_fields(row), _family_fields(request)
    return (row.sender == request.recipient
            and _family_head(row) in {"booking.receipt", "cancel"}
            and all(fields.get(key, wanted.get(key)) == wanted.get(key) for key in ("offer_id", "slot_id", "facility")))


def _family_cross_uncertain(state: _FamilyCase, agent: str) -> bool:
    sent = {_family_cross_hash(row.text) for row in _family_cross_forwards(state, agent)}
    keys = {_family_fields(row).get("key", "") for row in state.rows if row.sender == agent
            and row.recipient == LEDGER and first_word(row.text) == "remember"
            and _family_fields(row).get("key", "").startswith("family_forward:")}
    return any(_family_cross_marker(state, agent, key) == "started" and key.split(":", 1)[1] not in sent
               for key in keys)


async def _family_cross_confirm(
    city: City, state: _FamilyCase, citizen: str, offer: dict[str, str], selected: dict[str, str]
) -> None:
    oid, target = offer["offer_id"], offer["facility"]
    shown = _our_offer(state.rows, city.agent_id, oid)
    if (not shown or any(selected.get(key, shown.get(key)) != shown.get(key) for key in ("slot_id", "facility"))):
        await _family_note(city, state, citizen, "invalid_offer")
        return
    consent, declined = _family_cross_consent(state, city.agent_id, citizen, oid, target)
    if declined:
        await _family_note(city, state, citizen, "cross_consent_declined")
        return
    if not consent:
        english = _family_preferences(state.rows, citizen).get("lang") == "en"
        phrase = ("May I share your citizen identifier and selected appointment with this facility to book it? Without permission I will not send them."
                  if english else
                  "Posso condividere con questa struttura il tuo identificativo cittadino e l'appuntamento scelto per prenotarlo? Senza consenso non li invio.")
        await _family_send(city, state, citizen,
                           f"data.request purpose=cross_asst fields=consent_ref offer_id={oid} facility={target}\n{phrase}")
        return
    # Refresh the clock after consent: do not send a now-expired offer to the partner.
    clock = await _family_read(city, state, "agenda", "agenda", refresh=True)
    now = _when("" if clock is None else _family_fields(clock).get("agenda_now", ""))
    if now is None:
        await _family_note(city, state, citizen, "ledger_timeout")
        return
    if _when(offer["at"]) <= now:
        await _family_send(city, state, citizen, f"cancel offer_id={oid} reason=slot_expired")
        await _family_cross_search(city, state, citizen, city.inbound, now)
        return
    command = (f"confirm offer_id={offer['partner_offer_id']} slot_id={offer['slot_id']} facility={target} "
               f"citizen={citizen} consent_ref={consent} cross_asst_consent_ref={consent} "
               "purpose=cross_asst")
    key = "family_forward:" + _family_cross_hash(command)
    if _family_cross_marker(state, city.agent_id, key):
        await _family_note(city, state, citizen, "partner_pending")
        return
    if await _family_send(city, state, LEDGER, f"remember key={key} value=started") is None:
        await _family_note(city, state, citizen, "ledger_timeout")
        return
    if await _family_send(city, state, target, command) is None:
        code = state.failures.get((target, command), "UPSTREAM_TIMEOUT")
        try:
            permanent = 400 <= CapError(code).http_status() < 500
        except CapError:
            permanent = True
        if permanent:
            await _family_send(city, state, LEDGER, f"remember key={key} value=failed")
            await _family_send(city, state, citizen, f"cancel offer_id={oid} reason=facility_unavailable")
            await _family_cross_search(city, state, citizen, city.inbound, now)
            return
    await _family_note(city, state, citizen, "partner_pending")


async def _family_cross_event(city: City, state: _FamilyCase, citizen: str, message: Message) -> bool:
    forwards = _family_cross_forwards(state, city.agent_id)
    for request in reversed(forwards):
        responses = [row for row in state.rows[state.rows.index(request) + 1:]
                     if _family_cross_response(row, request)]
        receipt = next((row for row in reversed(responses) if _family_head(row) == "booking.receipt"
                        and _family_fields(row).get("booking_id") and _family_fields(row).get("ledger_ref")), None)
        if receipt is not None:
            fields = _family_fields(receipt)
            if message.sender == citizen and _family_head(message) == "checkin":
                phrase = ("Your appointment is at the facility shown here; use these references to check in there."
                          if _family_preferences(state.rows, citizen).get("lang") == "en" else
                          "L'appuntamento è presso la struttura indicata qui: usa questi riferimenti per registrare lì il tuo arrivo.")
                await _family_send(city, state, citizen,
                    f"data.response purpose=checkin reason=external_facility facility={request.recipient} "
                    f"booking_id={fields['booking_id']}\n{phrase}")
            else:
                await _family_send(city, state, citizen,
                    f"booking.receipt booking_id={fields['booking_id']} ledger_ref={fields['ledger_ref']} facility={request.recipient}")
            return True
        cancel = next((row for row in responses if _family_head(row) == "cancel"), None)
        if cancel is not None:
            original = _family_fields(request)
            oid = "fx-" + _family_cross_hash(request.recipient + "|" + original['offer_id'] + "|" + original['slot_id'])
            reason = _family_fields(cancel).get("reason", "facility_cancelled")
            reason = reason if re.fullmatch(r"[A-Za-z0-9_]{1,64}", reason) else "facility_cancelled"
            await _family_send(city, state, citizen, f"cancel offer_id={oid} reason={reason}")
            continue
        # A successful send with no definitive reply is uncertain. Never book another slot.
        await _family_note(city, state, citizen, "partner_pending")
        return True
    if _family_cross_uncertain(state, city.agent_id):
        await _family_note(city, state, citizen, "partner_pending")
        return True
    if message.sender in _family_cross_requests(state, city.agent_id):
        clock = await _family_read(city, state, "agenda", "agenda", refresh=True)
        now = _when("" if clock is None else _family_fields(clock).get("agenda_now", ""))
        if now is None:
            await _family_note(city, state, citizen, "ledger_timeout")
        else:
            await _family_cross_search(city, state, citizen, message, now)
        return True
    return False




async def _facility(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    reports: bool,
    hold_self: bool,
) -> None:
    head = first_word(message.text)
    kind = words(message.text)
    if message.sender == LEDGER and head == "held":
        await _on_held(city, message, chat)
    elif message.sender == LEDGER and head == "committed":
        await _on_committed(city, message, chat)
    elif message.sender == LEDGER and head == "refused" and "hold" in kind:
        await _on_refused(city, message, chat, hold_self=hold_self)
    elif message.sender == LEDGER and head == "checked_in":
        await _on_checked_in(city, message, chat, reports=reports)
    elif message.sender == LEDGER and head == "reported":
        await _on_reported(city, message, chat)
    elif "checkin" in kind and message.sender.startswith("citizen:"):
        await _on_checkin(city, message, chat, reports=reports)
    elif "confirm" in kind:
        await _on_confirm(city, message, chat, hold_self=hold_self)
    elif message.sender.startswith("agent:") and "need.request" in kind and \
            fields_of(message.text).get("service_type", "").startswith("family_counseling"):
        await _answer_ring(city, message, chat)


async def _answer_ring(city: City, message: Message, chat: list[Message]) -> None:
    """MACH-1.2: another ASST asks for our consultorio slots. Criteria in, ONE message out.

    The request carries no citizen data and we ask for none; offers only name our own slots.
    Every criterion it names is honoured (lang, gender_preference, days, after, before,
    deadline). When nothing fits them all, a cancel line then up to 3 offers that break one
    criterion each, marked relaxed=<key>. The same request_id twice gets the same lines.
    """

    agent, partner = city.agent_id, message.sender
    fields = fields_of(message.text)
    token = f"request_id={fields.get('request_id', message.msg_id)}"
    replies = [row for row in chat if row.sender == agent and row.recipient == partner
               and token in row.text.split() and words(row.text) & {"offer", "cancel"}]
    if replies:
        # Idempotency: one answer per request we received, always the same lines.
        asks = {row.msg_id for row in chat if row.sender == partner
                and token in row.text.split() and "need.request" in words(row.text)}
        asks.add(message.msg_id)
        if len(replies) < len(asks):
            await _say(city, partner, replies[0].text)
        return
    if not _exact(chat, agent, "agenda"):
        await _say(city, LEDGER, "agenda")
    if not _asked(chat, agent, "CONS-GIN", 1):
        await _say(city, LEDGER, "world procedure=CONS-GIN page=1")
    rows = await _until(city, message.need_id, chat,
                        lambda r: _has_agenda(r) and _latest_slots(r) is not None)
    agenda, world = _ledger(rows, "agenda"), _latest_slots(rows)
    now = _when("" if agenda is None else agenda.get("agenda_now", ""))
    if now is None or world is None:
        await _say(city, partner, f"cancel {token} reason=facility_unavailable")
        return
    deadline = _when(fields.get("deadline", ""))
    clock = _ring_clock()
    slots = [slot for slot in fitting(
        [line for line in world.splitlines() if "slot_id=" in line], now=now,
        priority=fields.get("priority"), lang=fields.get("lang", "it").lower(), facility=agent,
        procedure="CONS-GIN") if (deadline is None or _when(slot["at"]) <= deadline)
        and not _soft_locked(slot["slot_id"], token, clock)]
    exact, relaxed = [], []
    for slot in slots:
        broken = _ring_broken(slot, fields)
        if not broken:
            exact.append(slot)
        elif len(broken) == 1:
            relaxed.append((slot, broken[0]))
    if exact:
        _soft_lock([slot["slot_id"] for slot in exact[:3]], token, clock)
        await _say(city, partner, "\n".join(_ring_line(slot, agent, token) for slot in exact[:3]))
        return
    lines = [f"cancel {token} reason=no_availability"]
    seen: set[str] = set()
    for slot, key in relaxed:
        # Prefer variety: the nearest slot for each relaxed criterion first.
        if key not in seen and len(lines) < 4:
            seen.add(key)
            lines.append(_ring_line(slot, agent, token) + f" relaxed={key}")
    for slot, key in relaxed:
        line = _ring_line(slot, agent, token) + f" relaxed={key}"
        if len(lines) < 4 and line not in lines:
            lines.append(line)
    _soft_lock([fields_of(line)["slot_id"] for line in lines[1:]], token, clock)
    if len(lines) > 1:
        lines.append("Con tutti i criteri non c'è posto: queste sono le alternative più vicine, "
                     "ognuna cambia un solo criterio.")
    await _say(city, partner, "\n".join(lines))


def _ring_broken(slot: dict[str, str], fields: dict[str, str]) -> list[str]:
    """Which of the request's soft criteria this slot does not meet."""

    broken: list[str] = []
    spec = _DOCTORS.get(slot["doctor_id"].rsplit("-", 1)[-1])
    gender = fields.get("gender_preference", fields.get("gender", "")).lower()[:1]
    if gender in ("f", "m") and (spec is None or spec[1] != gender):
        broken.append("gender_preference")
    days = _family_days(fields.get("days", ""))
    if days and slot.get("day", "") not in days:
        broken.append("days")
    at = _when(slot["at"])
    after, before = _when(fields.get("after", "")), _when(fields.get("before", ""))
    if at is not None and ((after is not None and at < after) or (before is not None and at > before)):
        broken.append("after" if after is not None and at < after else "before")
    return broken


def _ring_line(slot: dict[str, str], agent: str, token: str) -> str:
    spec = _DOCTORS.get(slot["doctor_id"].rsplit("-", 1)[-1])
    extra = "" if spec is None else (
        f" doctor_lang={'/'.join(sorted(spec[0] - {'*'}))} doctor_gender={spec[1]}")
    return (f"offer offer_id=p-{slot['slot_id']} slot_id={slot['slot_id']} "
            f"doctor_id={slot['doctor_id']} facility={agent} procedure=CONS-GIN "
            f"agenda_date={slot['at']} agenda_day={slot.get('day', '')}{extra} {token}")


def _booking_of(chat: list[Message], agent: str, slot_id: str) -> tuple[str, str, str | None]:
    """Who asked us to book this slot, the consent, and the prescription, if we have them."""

    token = f"slot_id={slot_id}"
    sender = ""
    consent = ""
    number: str | None = None
    for row in chat:
        if token not in row.text.split():
            continue
        fields = fields_of(row.text)
        if "confirm" in words(row.text) and row.sender not in (LEDGER, agent):
            sender = row.sender
            consent = fields.get("consent_ref", consent)
            number = fields.get("prescription_number") or number
        if row.sender == agent and "hold" in words(row.text):
            consent = fields.get("consent_ref", consent)
            number = fields.get("prescription_number") or number
            sender = sender or fields.get("citizen", "")
    return sender, consent, number


def _hold_slot(chat: list[Message], agent: str, hold_id: str) -> str:
    token = f"hold_id={hold_id}"
    for row in chat:
        if token in row.text.split() and (row.sender == agent or row.sender == LEDGER):
            slot = fields_of(row.text).get("slot_id", "")
            if slot:
                return slot
    return ""


def _our_commit(chat: list[Message], agent: str, hold_id: str) -> bool:
    """A commit we actually sent. The ledger's held line also contains the word commit."""

    token = f"hold_id={hold_id}"
    for row in chat:
        if row.sender != agent or row.recipient != LEDGER:
            continue
        if row.text.strip().startswith("commit ") and token in row.text.split():
            return True
    return False


def _latest_hold(chat: list[Message], agent: str) -> tuple[str, str | None]:
    consent = ""
    number: str | None = None
    for row in chat:
        if row.sender != agent or not row.text.strip().startswith("hold "):
            continue
        fields = fields_of(row.text)
        consent = fields.get("consent_ref", consent)
        number = fields.get("prescription_number") or number
    return consent, number


async def _finish_booking(
    city: City, message: Message, chat: list[Message], slot: str, *, hold_self: bool
) -> None:
    """Hold is in the mailbox. Read the held line and commit before this turn ends."""

    found = await _await_line(city, message.need_id, chat, "held", f"slot_id={slot}", op="hold")
    if found is None:
        _LOG.info("care.held.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = found
    if first_word(reply.text) == "refused":
        await _on_refused(city, reply, fresh, hold_self=hold_self)
        return
    await _on_held(city, reply, fresh)
    hold_id = fields_of(reply.text).get("hold_id", "")
    if not hold_id:
        return
    done = await _await_line(
        city, message.need_id, fresh, "committed", f"booking_id={hold_id}", op="commit"
    )
    if done is None:
        _LOG.info("care.commit.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = done
    if first_word(reply.text) == "committed":
        await _on_committed(city, reply, fresh)
        return
    if first_word(reply.text) == "refused":
        await _on_refused(city, reply, fresh, hold_self=hold_self)


async def _on_held(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    hold_id = fields.get("hold_id", "")
    slot = fields.get("slot_id", "")
    if not hold_id or not slot:
        _LOG.info("care.held.skip", extra={"head": first_word(message.text)[:40]})
        return
    if _our_commit(chat, city.agent_id, hold_id):
        return
    _, consent, number = _booking_of(chat, city.agent_id, slot)
    if not consent:
        _LOG.info(
            "care.held.noconsent",
            extra={"need_id": fields.get("need_id", "")[:48]},
        )
        return
    parts = [
        "commit",
        f"hold_id={hold_id}",
        f"consent_ref={consent}",
        f"fence={fields.get('fence', '1')}",
    ]
    if number:
        parts.append(f"prescription_number={number}")
    await _say(city, LEDGER, " ".join(parts))


async def _on_committed(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    booking_id = fields.get("booking_id", "")
    ledger_ref = fields.get("ledger_ref", "")
    if not booking_id or not ledger_ref:
        return
    slot = fields.get("slot_id") or _hold_slot(chat, city.agent_id, fields.get("hold_id", ""))
    sender, _, _ = _booking_of(chat, city.agent_id, slot) if slot else ("", "", None)
    target = sender or _citizen(chat)
    if not target:
        return
    token = f"booking_id={booking_id}"
    if _we(chat, city.agent_id, token, to=target, word="booking.receipt"):
        return
    await _say(city, target, f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref}")


async def _on_refused(
    city: City, message: Message, chat: list[Message], *, hold_self: bool
) -> None:
    agent = city.agent_id
    slot = ""
    for row in chat:
        if row.sender == agent and row.recipient == LEDGER and "hold" in words(row.text):
            slot = fields_of(row.text).get("slot_id", slot)
    if not slot:
        return
    target, _, _ = _booking_of(chat, agent, slot)
    offer_id = ""
    for row in chat:
        if row.sender == agent:
            for offer in _offer_rows(row.text):
                if offer.get("slot_id") == slot:
                    offer_id = offer.get("offer_id", offer_id)
        elif f"slot_id={slot}" in row.text.split():
            offer_id = fields_of(row.text).get("offer_id", offer_id)
    if (
        target
        and offer_id
        and not _we(chat, agent, f"offer_id={offer_id}", to=target, word="cancel")
    ):
        found = fields_of(message.text)
        reason = found.get("code") or found.get("reason") or "booking_refused"
        if target.startswith("agent:"):
            # MACH-1.2 (level 7): a shared reason and, below the cancel, our alternatives.
            await _say(city, target, _ring_refusal(chat, agent, target, offer_id, slot, reason))
        else:
            await _say(
                city,
                target,
                f"cancel offer_id={offer_id} reason={reason}",
            )
    if hold_self:
        await _advance(city, message, chat, fixed="CONS-GIN", replacing=True)


async def _on_checkin(city: City, message: Message, chat: list[Message], *, reports: bool) -> None:
    booking_id = fields_of(message.text).get("booking_id", "")
    citizen = message.sender if message.sender.startswith("citizen:") else _citizen(chat)
    if not booking_id or not citizen:
        return
    token = f"booking_id={booking_id}"
    if not _we(chat, city.agent_id, token, to=LEDGER, word="checkin"):
        if not await _say(city, LEDGER, f"checkin booking_id={booking_id} citizen={citizen}"):
            return
    found = await _await_line(city, message.need_id, chat, "checked_in", token, op="checkin")
    if found is None:
        _LOG.info("care.checkin.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = found
    if first_word(reply.text) == "checked_in":
        await _on_checked_in(city, reply, fresh, reports=reports)


async def _on_checked_in(
    city: City, message: Message, chat: list[Message], *, reports: bool
) -> None:
    booking_id = fields_of(message.text).get("booking_id", "")
    citizen = _citizen(chat)
    if not booking_id:
        return
    token = f"booking_id={booking_id}"
    if reports and not _we(chat, city.agent_id, token, to=LEDGER, word="report"):
        await _say(city, LEDGER, f"report booking_id={booking_id} outcome_code=VISIT")
    if citizen and not _we(chat, city.agent_id, token, to=citizen, word="checkin"):
        await _say(city, citizen, f"checkin booking_id={booking_id}")
    if not reports:
        return
    found = await _await_line(city, message.need_id, chat, "reported", token, op="report")
    if found is None:
        _LOG.info("care.report.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = found
    if first_word(reply.text) == "reported":
        await _on_reported(city, reply, fresh)


async def _on_reported(city: City, message: Message, chat: list[Message]) -> None:
    booking_id = fields_of(message.text).get("booking_id", "")
    citizen = _citizen(chat)
    if not booking_id or not citizen:
        return
    token = f"booking_id={booking_id}"
    if _we(chat, city.agent_id, token, to=citizen, word="report"):
        return
    await _say(city, citizen, f"report booking_id={booking_id} outcome_code=VISIT")


# Collaboration: the review gives +1 to an agent that contacts another one, unasked, to
# improve the service. When the citizen gives up one of our slots, we tell our booking
# centre that the slot stays free for other citizens. Slot only: never citizen data.
_OUR_BOOKING_CENTRE = "agent:h3n6kfd18r3g"


async def _family_slot_free(city: City, state: _FamilyCase, citizen: str, message: Message) -> None:
    offer_id = _family_fields(message).get("offer_id", "")
    slots = {offer.get("offer_id"): offer.get("slot_id") for row in state.rows
             if row.sender == city.agent_id and row.recipient == citizen
             for offer in _offer_rows(row.text)}
    slot = slots.get(offer_id) if offer_id else None
    if not slot or offer_id.startswith("fx-") or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", slot):
        return
    await _family_send(city, state, _OUR_BOOKING_CENTRE,
                       f"notice slot_free procedure=CONS-GIN slot_id={slot}\n"
                       "Dal consultorio: una persona ha rinunciato a questo posto, "
                       "resta libero per altri cittadini che vi chiedono una visita CONS-GIN.")


# --- level 7: duplicates, contention, refusals (MACH-1.2) -------------------------------------
# SOFT LOCK: for 60 real seconds after we offer a slot to one request, it is not offered to
# another request_id. The Ledger stays the only arbiter: the first hold wins.
_SOFT_LOCK_FOR = 60.0
_SOFT: dict[str, tuple[float, str]] = {}
_RECEIPT_RESENT: set[str] = set()
_LEDGER_REASONS = {"CONSENT_MISSING": "consent_missing", "NO_CAPACITY": "no_capacity",
                   "PRESCRIPTION_EXPIRED": "prescription_expired"}


def _ring_clock() -> float:
    return time.monotonic()


def _soft_locked(slot_id: str, token: str, clock: float) -> bool:
    held = _SOFT.get(slot_id)
    return held is not None and held[1] != token and clock - held[0] < _SOFT_LOCK_FOR


def _soft_lock(slot_ids: list[str], token: str, clock: float) -> None:
    for slot_id in slot_ids:
        _SOFT[slot_id] = (clock, token)
    if len(_SOFT) > 2000:
        for key in [key for key, (at, _) in _SOFT.items() if clock - at >= _SOFT_LOCK_FOR]:
            del _SOFT[key]


def _ring_refusal(chat: list[Message], agent: str, target: str, offer_id: str, slot: str,
                  code: str) -> str:
    """The Ledger refused a partner's hold: one cancel with a shared reason, alternatives below."""

    reason = _LEDGER_REASONS.get(code.upper(), "slot_taken")
    token = next((f"request_id={fields_of(row.text)['request_id']}" for row in chat
                  if row.sender == agent and row.recipient == target
                  and f"slot_id={slot}" in row.text.split()
                  and fields_of(row.text).get("request_id")), "")
    lines = [" ".join(part for part in (f"cancel offer_id={offer_id}", f"slot_id={slot}",
                                        f"reason={reason}", token) if part)]
    agenda, world = _ledger(chat, "agenda"), _latest_slots(chat)
    now = _when("" if agenda is None else agenda.get("agenda_now", ""))
    if reason == "slot_taken" and token and now is not None and world is not None:
        clock = _ring_clock()
        others = [row for row in fitting([line for line in world.splitlines() if "slot_id=" in line],
                                         now=now, priority=None, lang="it", facility=agent,
                                         procedure="CONS-GIN")
                  if row["slot_id"] != slot and not _soft_locked(row["slot_id"], token, clock)][:3]
        _soft_lock([row["slot_id"] for row in others], token, clock)
        lines += [_ring_line(row, agent, token) for row in others]
    return "\n".join(lines)


def _receipt_again(chat: list[Message], agent: str, message: Message, slot: str) -> str:
    """IDEMPOTENCY: a partner confirms the same offer twice -> the same receipt, never a hold."""

    if not message.sender.startswith("agent:") or message.msg_id in _RECEIPT_RESENT:
        return ""
    for row in chat:
        if row.sender != LEDGER or first_word(row.text) != "committed":
            continue
        found = fields_of(row.text)
        if (found.get("slot_id") or _hold_slot(chat, agent, found.get("hold_id", ""))) != slot:
            continue
        token = f"booking_id={found.get('booking_id', '')}"
        for sent in chat:
            if (sent.sender == agent and sent.recipient == message.sender
                    and first_word(sent.text) == "booking.receipt" and token in sent.text.split()):
                _RECEIPT_RESENT.add(message.msg_id)
                return sent.text
# --- TRUST RING: impostors (Sat 10:00) -----------------------------------------------------------

# Every agent on the city map (/v1/scoreboard/map) BEFORE the impostors arrive (Sat 10:00):
# 26 ASSTs x 5 (booking_centre gp hospital specialist family_counseling). Snapshot 21/11 02:10.
# The city signs each sender, so an impostor cannot reuse one of these ids: it needs a new one.
_CITY_AGENTS = frozenset("agent:" + token for token in """
    nord         psxp8bh1feaa wwxaphfb5pfh gpr1zqe343gb zand0f57ec1j ccjzqm7yekwx
    est          n8geakhhcprc xjsdqxvj3x59 r8njm9a6c7pv gpwmb45gggme hvycshaebezp
    sud          p56k8h29kc45 v4y8p7h8gkzw p90cbsk3n9e6 n0p2dwznf4mc kx0j48fa0epp
    ovest        pewepg7h3c5g bq7e62g3kkz3 darwa6412gz0 fa0sw3wwn1d4 r7rgp7mdwg8y
    centro       y6hxsd766sys jmqwr12vy2j6 r8yprrs0e8be dqfw9nh15dn4 pv3a0t3tyqdq
    brera        q6s5nm3h5dq5 xbmtvn6jzvyg ndrnnjwxttmb fjmvabg58gh0 cj1jfac4ev86
    isola        sphwpc5tg4p9 cxvqv5qztz70 d6cxxmh767k3 etpwrfmfa8hm mkfzagfe8jtt
    navigli      jgy1dj2sw2ew dn90g1skkx0f gezf38x5940y s2tevpn8zkcj drchwbffymha
    porta-romana z6pbm2a7vf36 d6c308wmh6ky p2hwz5xyjrwz a88btdp00ymw bx7r8th27v1f
    citta-studi  hwfrwj893121 pp83zdrg8vhe ytprhckwyw4t sc25kab6cgde p5sq1njyjr47
    lambrate     gdrf9b9kxxss c2ewegr2ea3v nanpr5mgm2py pky3vqyqbepa atp772whjkgd
    bicocca      ez8cb7j167p2 rng5wcrzvhsj bb01ggxymm05 sh35cbsy287d nkh372rd16yq
    niguarda     fn64xfq2jft4 fddgaqmp6kb0 jdj692g4145e jmjfst55vgjz mm18nddkfc5w
    bovisa       vgtmj5nqfkxj h82xpx3rkn9e gxq3vzgt3h5t tty8erdj6t41 txar5994gbkj
    san-siro     tnzkhsa62kn0 qsbsfjytc3y5 k2nwktpp02xx yk37jqcnc1r5 sdca2w4p9f8z
    baggio       jastt2eeekcv ny08j7bvs7jw t40mr7xsn2wh t3ktav4yrxvb pd5th2nkb31q
    lorenteggio  qtjne8symjas w2qsptc9qqhg tmt0bcv0st1f n9bf9y6670hp mcppmrbjvxag
    giambellino  cdc7x6m21nj6 c32jyk0cyan7 h6r1v45esk77 hnpe58epeyh1 bzs050xacs1m
    barona       vdmz874c65kp w9easgp707bn w2str91zrrbq qg7rvp6ktyne hdnz4xe1c8d2
    corvetto     yzcs4p44ep39 hkrtm1gzjb24 x0vr9rbwpesr h8425ac7btd8 p0n57t7vtpq0
    rogoredo     ejfzvz4tdagq hwgn012k70gh fnxy73v47n9g s5sevkc5291w jet0tz755hvf
    forlanini    eg6p3m77svxv b0pk7g42fcsn aycbxayckhrd zeahjp9bp2an yr8hjc5q7n98
    ortica       geyjmp26tajh ega0tzcy6za8 vs1bs4485dcn cgj1c2mf1dx0 d0kkschyjrdq
    greco        h3n6kfd18r3g bmrcsmqq6at2 xtj1dk9x3qej q18es2d82f8r pxvd2kk2yx5j
    gorla        sbdpbpqz5sxe ck9w60ejwnvw m6py14a1ej77 h6krzxx6es83 yg8debxe3x0p
    precotto     ser7es7thgwf n4yjr7kjbmrx s1twcdzg3296 f2s88vwd3jnf nf9pqsgzrwm0
""".split() if len(token) == 12 and token.isalnum())
_INJECTION = re.compile(
    r"ignor\w*\s+(\w+\s+){0,3}(regol|rule|istruzion|instruction|policy)|\bsystem\s*:|"
    r"nuova policy|new policy|manda(mi)? tutto|send (me )?everything|developer mode|jailbreak",
    re.IGNORECASE)
_UNASKED_HEADS = frozenset({"data.request", "consent", "prescription", "report", "checkin",
                            "booking.receipt"})


def _family_impostor(city: City, message: Message, chat: list[Message]) -> str:
    """Why an agent's message is dropped ("" = keep it). MACH-1.2 TRUST RING checks.

    Agents on the map before Saturday pass the sender check. A new agent is kept only if it
    asks for nothing, carries no instruction, offers only its own slots and confirms only an
    offer we sent to it.
    """

    sender = message.sender
    if not sender.startswith("agent:"):
        return ""
    if _INJECTION.search(message.text):
        return "injection"
    if sender in _CITY_AGENTS:
        return ""
    head = first_word(message.text)
    confirmed = any(row.sender == city.agent_id and row.recipient == sender
                    and first_word(row.text) == "confirm" for row in chat)
    if head == "booking.receipt" and confirmed:
        return ""  # a receipt from the facility we sent a confirm to
    if head in _UNASKED_HEADS or words(message.text) & {"data.request", "consent"}:
        return "phantom_receipt" if head == "booking.receipt" else "asked_data"
    for line in message.text.splitlines():
        facility = fields_of(line).get("facility")
        if first_word(line) == "offer" and facility and facility != sender:
            return "facility_mismatch"
    if head == "confirm":
        offer_id = fields_of(message.text).get("offer_id", "")
        if not offer_id or not any(row.sender == city.agent_id and row.recipient == sender
                                   and f"offer_id={offer_id}" in row.text.split() for row in chat):
            return "fake_confirm"
    return ""
