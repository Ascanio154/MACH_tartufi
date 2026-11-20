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
               f"citizen={citizen} consent_ref={consent} purpose=cross_asst")
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
        reason = fields_of(message.text).get("code", "booking_refused")
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
