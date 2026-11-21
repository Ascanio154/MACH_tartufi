"""Part of agent.py (loaded into its namespace by _load_parts). Do not import directly."""

from __future__ import annotations

async def _partner_cancel(
    city: City, message: Message, chat: list[Message], *, fixed: str | None
) -> None:
    fields = fields_of(message.text)
    offer_id = fields.get("offer_id", "")
    if offer_id.lower() in ("none", "null", "nil", "-", "0"):
        offer_id = ""  # journey 116: never pass "cancel offer_id=none" on to the citizen
    citizen = _citizen(chat)
    key = message.need_id or message.msg_id
    partner = _CONFIRMED_BY.get(key, "")
    if partner and message.sender != partner and offer_id:
        # Our facility refused a slot another ASST confirmed through us: tell that ASST.
        reason = _ring_reason(fields.get("reason", "hold_refused"))
        await _say(city, partner, f"cancel offer_id={offer_id} reason={reason}")
        return
    if citizen and offer_id and _PENDING.get(_who(chat, citizen)) == offer_id:
        _PENDING.pop(_who(chat, citizen), None)
    if not offer_id and (
        fields.get("request_id") == f"x-{key}"
        or message.sender in _partners_asked(chat, city.agent_id, key)
    ):
        # A partner has no slot for our search. When every partner asked said so, tell the
        # citizen instead of leaving them waiting.
        entry = _sent(key)
        entry.offers.add(f"partner-no:{message.sender}")
        refused = {mark.partition(":")[2] for mark in entry.offers if mark.startswith("partner-no:")}
        refused |= {
            row.sender
            for row in chat
            if row.sender.startswith("agent:")
            and first_word(row.text) == "cancel"
            and f"request_id=x-{key}" in row.text.split()
        }
        asked = _partners_asked(chat, city.agent_id, key)
        if citizen and asked and asked <= refused and not _partner_offered(key, _number(chat)):
            await _close_search(city, chat, city.agent_id, citizen, key)
        return
    # P1: the Ledger refused the commit because the prescription expired. Offering the same
    # number again books an expired prescription, so send the citizen to the GP instead.
    if not offer_id:
        return  # a cancel naming no offer of ours: nothing to withdraw
    expired = "EXPIR" in fields.get("reason", "").upper()
    reason = "prescription_expired" if expired else "facility_cancelled"
    refused = _find_offer(chat, city.agent_id, offer_id)
    if refused.get("doctor_id"):
        when = refused.get("agenda_date") or refused.get("at", "")
        # Level 7 contention: one refusal is a lost race for that slot. Only a second one for
        # the same doctor and day (journey 104) takes the doctor's other slots out.
        mark = f"{refused['doctor_id']}|{when[:10]}"
        marks = _sent(key).offers
        marks.add(f"bad-doc:{mark}" if f"lost:{mark}" in marks else f"lost:{mark}")
    mark = (citizen or "", offer_id)
    if (
        citizen
        and mark not in _CANCELS_SENT
        and not _we(chat, city.agent_id, f"offer_id={offer_id}", to=citizen, word="cancel")
    ):
        _CANCELS_SENT.add(mark)
        sentence = "" if expired else _phrase("refused", _lang(chat))
        await _say(city, citizen, f"cancel offer_id={offer_id} reason={reason} {sentence}".rstrip())
    if expired:
        number = _our_offer(chat, city.agent_id, offer_id).get("prescription_number") or _number(chat)
        if citizen and number:
            key = message.need_id or message.msg_id
            await _ask_renewal(city, citizen, number, _lang(chat), chat, city.agent_id, key)
        return
    if citizen and fixed is None and message.sender not in _own_facilities(chat) \
            and refused.get("facility", message.sender) not in _own_facilities(chat):
        # Journey 118: a partner ASST refused the slot it offered. Our ASST had nothing, so
        # ask the partners we have not asked yet instead of closing the path.
        await _partner_retry(city, message, chat, citizen, key)
        return
    await _advance(city, message, chat, fixed=fixed, replacing=True)


async def _partner_retry(
    city: City, message: Message, chat: list[Message], citizen: str, key: str
) -> None:
    agent = city.agent_id
    number = _number(chat)
    row = _rx_line(chat, agent, number) if number else None
    reply = {} if row is None else fields_of(row.text)
    before = _partners_asked(chat, agent, key)
    _sent(f"{key}|{number}").offers.discard("partner-offered")
    await _ask_partners(city, chat, agent, citizen, reply.get("procedure"), reply.get("priority"),
                        key, avoid=message.sender)
    fresh = _partners_asked(chat, agent, key) - before
    if not fresh:
        await _close_search(city, chat, agent, citizen, key)
        return

    def answered(rows: list[Message]) -> bool:
        replies = {r.sender for r in rows if r.sender in fresh and first_word(r.text) == "cancel"}
        return any(r.sender in fresh and first_word(r.text) == "offer" for r in rows) or (
            fresh <= replies)

    rows = await _until(city, message.need_id, chat, answered, _SHORT_TRIES)
    if not any(r.sender in fresh and first_word(r.text) == "offer" for r in rows):
        await _close_search(city, rows, agent, citizen, key)


# --- facility ----------------------------------------------------------------------------------


def _facility_turn(message: Message) -> bool:
    head = first_word(message.text)
    kind = words(message.text)
    if message.sender == LEDGER and head in {"held", "committed", "checked_in", "reported"}:
        return True
    if message.sender == LEDGER and head == "refused" and "hold" in kind:
        return True
    if "checkin" in kind and message.sender.startswith("citizen:"):
        return True
    return "confirm" in kind and (
        message.sender.startswith("agent:") or message.sender.startswith("citizen:")
    )


async def _family(city: City, message: Message, chat: list[Message]) -> None:
    if _facility_turn(message):
        await _facility(city, message, chat, reports=False, hold_self=True)
        return
    await _desk(city, message, chat, hold_self=True, fixed="CONS-GIN")


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
        consent, held_number = _latest_hold(chat, city.agent_id)
        number = number or held_number
    need = fields.get("need_id", "")
    if not consent and need.startswith("N-"):
        # The citizen's booking consent is cons-<need_id>-booking. The held line does not
        # echo the real ref, and the hold that carried it may not be in this page of history.
        consent = f"cons-{need}-booking"
    if not consent:
        _LOG.info("care.held.noconsent", extra={"need_id": need[:48]})
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
        if f"slot_id={slot}" in row.text.split():
            offer_id = fields_of(row.text).get("offer_id", offer_id)
    if (
        target
        and offer_id
        and not _we(chat, agent, f"offer_id={offer_id}", to=target, word="cancel")
    ):
        await _say(city, target, f"cancel offer_id={offer_id} reason=facility_cancelled")
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


# (citizen, offer) cancels already passed on: journey 104 got the same cancel twice.
_CANCELS_SENT: set[tuple[str, str]] = set()


def _needs_access(chat: list[Message]) -> bool:
    """The citizen mentioned a wheelchair, a disability or similar accessibility needs."""

    return any(row.sender.startswith("citizen:") and _ACCESS_WORDS.search(row.text)
               for row in chat)


# --- Ledger answers shared between cases ----------------------------------------------------------
# Every case used to wait for four Ledger answers (referral, agenda, rules, world) and the first
# offer came minutes later, after the slots inside the priority window had gone by. The referral is
# still checked for every case. The agenda clock is computed from the last answer (it runs 60 times
# faster than real time and has no weekend) and a world page read seconds ago for the same
# procedure is used again; _USED and _SHOWN keep the slots we already gave away out of it.
_AGENDA: list[datetime] = []  # [agenda time in the Ledger's answer, real time of that answer]
_WORLD: dict[str, dict[int, tuple[datetime, str]]] = {}  # procedure -> page -> (real time, text)
_AGENDA_FOR, _WORLD_FOR = 300.0, 90.0  # seconds a cached answer is used for
_BORN: dict[str, datetime] = {}  # need -> the agenda time we first saw for it


def _age(at: datetime) -> float:
    return (datetime.now(timezone.utc) - at).total_seconds()


def _learn(rows: list[Message]) -> None:
    """Remember the agenda and world answers in ``rows`` by the real time the Ledger sent them."""

    for row in rows:
        if row.sender != LEDGER or row.msg_id.startswith("cache-") or not row.sent_at:
            continue
        try:
            at = datetime.fromisoformat(row.sent_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if at.tzinfo is None:
            continue
        head = first_word(row.text)
        if head == "agenda":
            when = _when(fields_of(row.text).get("agenda_now", ""))
            if when is not None and (not _AGENDA or at > _AGENDA[1]):
                _AGENDA[:] = [when, at]
        elif head == "slots" and row.text:
            procedure = fields_of(row.text.splitlines()[0]).get("procedure", "")
            pages = _WORLD.setdefault(procedure, {})
            page = _page(row.text)[0]
            if procedure and (page not in pages or at > pages[page][0]):
                pages[page] = (at, row.text)


def _agenda_guess() -> datetime | None:
    """The agenda time now, from the last answer: 60 agenda seconds per real second, no weekend."""

    if not _AGENDA or not 0 <= _age(_AGENDA[1]) <= _AGENDA_FOR:
        return None
    guess = _AGENDA[0] + timedelta(seconds=60 * _age(_AGENDA[1]))
    return guess + timedelta(days=2) if guess.weekday() >= 5 else guess


def _agenda_stale() -> bool:
    """True when the next case should ask the Ledger again (the answer is not waited for)."""

    return not _AGENDA or not 0 <= _age(_AGENDA[1]) <= _AGENDA_FOR / 2


def _world_cached(procedure: str, limit: float = _WORLD_FOR) -> list[str]:
    pages = _WORLD.get(procedure, {})
    return [pages[page][1] for page in sorted(pages) if 0 <= _age(pages[page][0]) <= limit]


def _cached(rows: list[Message]) -> list[Message]:
    """``rows`` plus the agenda and world answers another case just read, when this one lacks them."""

    _learn(rows)
    need = next((row.need_id for row in rows if row.need_id), None)
    texts: list[str] = []
    # The case's own agenda row is from its first turn: at a cancel minutes later it is hours of
    # agenda behind and the new offer repeated slots already past (88 "reason=past" in one night).
    # The computed time goes last, so it is the one every later read of the agenda finds.
    guess = _agenda_guess()
    seen = _when((_ledger(rows, "agenda") or {}).get("agenda_now", ""))
    if guess is not None and (seen is None or guess > seen):
        texts.append("agenda agenda_now=" + guess.isoformat())
    procedure = (_ledger(rows, "prescription") or {}).get("procedure", "")
    if procedure and _latest_slots(rows) is None:
        texts += _world_cached(procedure)
    if not texts:
        return rows
    _LOG.info("care.cache %s", " ".join(text.split()[0] for text in texts))
    return list(rows) + [
        Message(type="chat", sender=LEDGER, msg_id=f"cache-{index}", need_id=need,
                body={"text": text})
        for index, text in enumerate(texts)
    ]
