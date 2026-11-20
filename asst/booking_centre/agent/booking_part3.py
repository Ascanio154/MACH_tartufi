"""Part of agent.py (loaded into its namespace by _load_parts). Do not import directly."""

from __future__ import annotations

async def _agent_note(city: City, message: Message, chat: list[Message]) -> None:
    """An agent wrote something we cannot act on: explain our format once, never loop."""

    if message.sender in _TEAM_AGENTS or message.sender == LEDGER:
        return
    if first_word(message.text) in _ACT_WORDS:
        return
    mark = (message.sender, message.need_id or message.msg_id)
    if mark in _AGENT_NOTED:
        return
    _AGENT_NOTED.add(mark)
    await _say(
        city,
        message.sender,
        "data.request purpose=coordination fields=procedure,priority,request_id "
        "Booking centre here: to ask for our free slots, write to us with service_type=... "
        "procedure=... priority=... request_id=... (search criteria only, no citizen data).",
    )


async def _advance(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    fixed: str | None,
    replacing: bool,
    avoid: str | None = None,
) -> None:
    key = message.need_id or message.msg_id
    await _locked(
        key,
        lambda: _advance_alone(
            city, message, chat, key, fixed=fixed, replacing=replacing, avoid=avoid
        ),
    )


async def _locked(key: str, work: Callable[[], Awaitable[None]]) -> None:
    """Run ``work`` alone for this need: turns of one need run at the same time."""

    gate = _GATES.get(key)
    if gate is None:
        gate = _GATES[key] = _Gate()
    gate.turns += 1
    try:
        async with gate.lock:
            await work()
    finally:
        gate.turns -= 1
        if gate.turns == 0 and _GATES.get(key) is gate:
            del _GATES[key]


def _partner_offers_in(text: str, default_facility: str) -> list[dict[str, str]]:
    """Every offer in a partner's message, one per line or several on one line."""

    found: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for token in text.replace(",", " ").split():
        key, eq, value = token.strip(";:!?()\"'").partition("=")
        value = value.strip(".,;:!?")
        if not eq or not key or not value:
            continue
        if key == "offer_id":
            if current is not None:
                found.append(current)
            current = {"offer_id": value}
        elif current is not None and key not in current:
            current[key] = value
    if current is not None:
        found.append(current)
    for offer in found:
        offer.setdefault("facility", default_facility)
    return [offer for offer in found if offer.get("slot_id")]


def _partner_line(
    slot: dict[str, str], offer_id: str, number: str | None, priority: str | None
) -> str:
    parts = [
        "offer",
        f"offer_id={offer_id}",
        f"slot_id={slot['slot_id']}",
        f"doctor_id={slot['doctor_id']}",
        f"facility={slot['facility']}",
        f"procedure={slot['procedure']}",
        f"agenda_date={slot['at']}",
        f"agenda_day={slot['day']}",
        f"expiry={slot['at']}",
    ]
    if priority:
        parts.append(f"priority={priority}")
    if number:
        parts.append(f"prescription_number={number}")
    return " ".join(parts)


async def _on_partner_offer(city: City, message: Message, chat: list[Message]) -> None:
    """A partner facility's slots: filter them for this citizen, offer them in ONE message."""

    agent = city.agent_id
    citizen = _citizen(chat)
    ours = _own_facilities(chat)
    number = _number(chat)
    if citizen is None or not number or message.sender in ours:
        return
    row = _rx_line(chat, agent, number)
    reply = {} if row is None else fields_of(row.text)
    procedure, priority = reply.get("procedure"), reply.get("priority")
    agenda = _ledger(chat, "agenda")
    now = _when("" if agenda is None else agenda.get("agenda_now", ""))
    if not procedure or now is None:
        return
    key = message.need_id or message.msg_id
    sent = _sent(f"{key}|{number}")
    if "partner-offered" in sent.offers:
        return
    lang, gender = _lang(chat), _gender(chat)
    tagged: list[str] = []  # the partner told us the doctor's languages and gender
    untagged: list[str] = []  # judged with our _DOCTORS table (same keys in every ASST)
    offer_ids: dict[str, str] = {}
    for offer in _partner_offers_in(message.text, message.sender):
        if offer["facility"] in ours:
            continue
        raw_day = (offer.get("agenda_day") or offer.get("day", "")).lower()
        day = _DAY_KEYS.get(raw_day[:3], raw_day)
        at = offer.get("agenda_date") or offer.get("at", "")
        line = (
            f"slot_id={offer['slot_id']} doctor_id={offer.get('doctor_id', '')} "
            f"facility={offer['facility']} procedure={offer.get('procedure', procedure)} "
            f"at={at} day={day}"
        )
        langs = offer.get("doctor_lang", "")
        if langs:
            speaks = lang in {part.strip().lower() for part in langs.split("/")}
            matches = not gender or offer.get("doctor_gender", gender) == gender
            if speaks and matches:
                tagged.append(line)
        else:
            untagged.append(line)
        offer_ids[offer["slot_id"]] = offer["offer_id"]
    taken = _offered(chat, agent, number) | sent.offers
    # Every doctor speaks Italian in every ASST: with language and gender already checked,
    # ``lang="it"`` keeps only the time window, deadline and working-day checks of ``fitting``.
    fits = [
        slot
        for slot in fitting(tagged, now=now, priority=priority, lang="it", procedure=procedure)
        + fitting(untagged, now=now, priority=priority, lang=lang, gender=gender,
                  procedure=procedure)
        if slot["slot_id"] not in taken
    ]
    if not fits:
        return
    lead = _PARTNER_LEAD_IT if _lang(chat) == "it" else _PARTNER_LEAD_EN
    rows: list[str] = []
    for slot in _pick(fits, _days(chat)):
        line = _partner_line(slot, offer_ids[slot["slot_id"]], number, priority)
        if len("\n".join([*rows, line, lead])) > _MAX_TEXT:
            break
        rows.append(line)
    text = "\n".join([*rows, lead])
    if await _say(city, citizen, text):
        sent.offers.add("partner-offered")
        for offer in _offers_in(text):
            sent.offers.add(offer["slot_id"])


async def _advance_alone(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
    *,
    fixed: str | None,
    replacing: bool,
    avoid: str | None = None,
) -> None:
    agent = city.agent_id
    citizen = _citizen(chat)
    if citizen is None:
        return
    reply = _ledger(chat, "prescription")
    priority = None if reply is None else reply.get("priority")
    procedure = fixed or (None if reply is None else reply.get("procedure"))
    number = _number(chat)
    agenda = _ledger(chat, "agenda")
    now = _when("" if agenda is None else agenda.get("agenda_now", ""))
    world = _latest_slots(chat)
    # Ask, then read the reply from history: the push of a ledger message does not open a turn.
    for _pass in range(3):
        number = _number(chat)
        row = _rx_line(chat, agent, number) if number and fixed is None else None
        reply = None if row is None else fields_of(row.text)
        rules = _ledger_row(chat, "rules")
        if rules is not None:
            _LOG.info("care.ledger.rules %s", rules.text[:300])
        if row is not None and number:
            _LOG.info("care.ledger.rx %s", row.text[:300])
            if _expired(row.text, chat):
                await _ask_renewal(city, citizen, number, _lang(chat), chat, agent, key)
                return
            if first_word(row.text) == "refused":
                # Unknown number (often typed by hand): ask for it again, once per number.
                entry = _sent(key)
                if f"bad-rx:{number}" not in entry.offers:
                    sentence = _phrase("bad_number", _lang(chat))
                    if await _say(
                        city,
                        citizen,
                        f"data.request purpose=booking fields=prescription_number {sentence}",
                    ):
                        entry.offers.add(f"bad-rx:{number}")
                return
        priority = None if reply is None else reply.get("priority")
        if priority == "P":
            offer_id = f"wl-{message.need_id or message.msg_id}"
            token = f"offer_id={offer_id}"
            sent = _sent(key)
            if offer_id in sent.offers:
                return
            if not _we(chat, agent, token, to=citizen, word="offer"):
                if await _say(city, citizen, f"offer offer_id={offer_id} waiting_list=true"):
                    sent.offers.add(offer_id)
            return
        procedure = fixed or (None if reply is None else reply.get("procedure"))
        if fixed is None and number and not procedure:
            if _pass == 2:
                return
            if row is None and _rx_asked(chat, agent, number) < 0:
                await _say(city, LEDGER, f"prescription prescription_number={number}")
            if not _exact(chat, agent, "agenda"):
                await _say(city, LEDGER, "agenda")
            # The rules in force ride along with the first reads; nothing waits on them.
            if not _exact(chat, agent, "rules"):
                await _say(city, LEDGER, "rules")
            asked = number
            chat = await _until(
                city,
                message.need_id,
                chat,
                lambda rows: _rx_line(rows, agent, asked) is not None and _has_agenda(rows),
            )
            continue
        if not procedure:
            return
        state = None if reply is None else reply.get("state")
        if fixed is None and state not in (None, "issued"):
            return
        agenda = _ledger(chat, "agenda")
        now = _when("" if agenda is None else agenda.get("agenda_now", ""))
        world = _latest_slots(chat)
        if now is not None and world is not None:
            break
        if _pass == 2:
            return
        if now is None and not _exact(chat, agent, "agenda"):
            await _say(city, LEDGER, "agenda")
        if world is None and not _asked(chat, agent, procedure, 1):
            await _say(city, LEDGER, f"world procedure={procedure} page=1")
        chat = await _until(
            city,
            message.need_id,
            chat,
            lambda rows: _has_agenda(rows) and _latest_slots(rows) is not None,
        )
    else:
        return
    if now is None or world is None:
        return
    # A cancel drops every offer the citizen was holding, so the ones still free are sent again,
    # once per cancel: a second turn for the same cancel or refusal does not repeat them.
    # Offers are tracked per prescription number: a renewed number starts with a fresh offer.
    sent = _sent(f"{key}|{number}")
    if replacing:
        trigger = fields_of(message.text).get("offer_id") or message.msg_id
        if sent.trigger != trigger:
            sent.trigger = trigger
            sent.again = set()
        taken = _withdrawn(chat, agent) | sent.again
    else:
        taken = _offered(chat, agent, number) | sent.offers
    fits = fitting(
        [line for line in world.splitlines() if "slot_id=" in line],
        now=now,
        priority=priority,
        lang=_lang(chat),
        gender=_gender(chat),
        facility=agent if fixed == "CONS-GIN" else None,
        procedure=procedure,
    )
    chosen = [
        slot
        for slot in fits
        if slot.get("slot_id") not in taken and slot.get("facility") != avoid
    ]
    if avoid and not chosen:
        # The facility is unreachable and nothing else fits here: a partner ASST may have it.
        if fixed is None and await _ask_partners(
            city, chat, agent, citizen, procedure, priority, key, avoid=avoid
        ):
            return
        none = _NO_OTHER_IT if _lang(chat) == "it" else _NO_OTHER_EN
        if not _exact(chat, agent, none):
            await _say(city, citizen, none)
        return
    if chosen:
        text = _offer_block(_pick(chosen, _days(chat)), number, priority, _lang(chat))
        if await _say(city, citizen, text):
            for offer in _offers_in(text):
                sent.offers.add(offer["slot_id"])
                if replacing:
                    sent.again.add(offer["slot_id"])
        return
    if taken and not replacing:
        return
    page, pages = _page(world)
    if page < pages and page < _MAX_PAGE:
        if not _asked(chat, agent, procedure, page + 1):
            await _say(city, LEDGER, f"world procedure={procedure} page={page + 1}")
        return
    # Our ASST has nothing that fits: ask the ring partners (approach A).
    if not fits and fixed is None and priority != "P":
        already = _partners_asked(chat, agent, key)
        asked = await _ask_partners(city, chat, agent, citizen, procedure, priority, key)
        # No partner to ask (empty Index, error): say so now instead of leaving them waiting.
        if not asked:
            await _close_search(city, chat, agent, citizen, key)
            return
        # The citizen writes again and no partner slot reached them: say so, not silence.
        if already and message.sender.startswith("citizen:") and not _partner_offered(key, number):
            await _close_search(city, chat, agent, citizen, key)


def _partners_asked(chat: list[Message], agent: str, key: str) -> set[str]:
    """Partner facilities we sent this case's search to."""

    token = f"request_id=x-{key}"
    return {
        row.recipient
        for row in chat
        if row.sender == agent and "need.request" in words(row.text) and token in row.text.split()
    } | {mark.partition(":")[2] for mark in _sent(key).offers if mark.startswith("partner:")}


def _partner_offered(key: str, number: str | None) -> bool:
    return "partner-offered" in _sent(f"{key}|{number}").offers


async def _close_search(city: City, chat: list[Message], agent: str, citizen: str, key: str) -> None:
    """Once per case: nobody, here or at the partners, has a fitting slot."""

    entry = _sent(key)
    text = _NO_PARTNER_IT if _lang(chat) == "it" else _NO_PARTNER_EN
    if "partner-closed" in entry.offers or _exact(chat, agent, text):
        return
    if await _say(city, citizen, text):
        entry.offers.add("partner-closed")


async def _on_confirm(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    hold_self: bool,
    picked: dict[str, str] | None = None,
    consent_ref: str = "",
) -> None:
    agent = city.agent_id
    fields = fields_of(message.text)
    if picked is not None:
        # A plain-language choice ("la seconda", "ok mercoledì"): the offer we sent decides.
        fields = {key: value for key, value in fields.items() if key in ("consent_ref",)}
        fields["offer_id"] = picked.get("offer_id", "")
    if consent_ref:
        fields["consent_ref"] = consent_ref
    offer_id = fields.get("offer_id", "")
    consent = fields.get("consent_ref", "")
    if not offer_id and message.sender.startswith("citizen:"):
        bare = _choice(message.text, _open_offers(chat, agent, message.sender))
        if bare is not None:
            offer_id = bare.get("offer_id", "")
    offer = _find_offer(chat, agent, offer_id)
    slot = fields.get("slot_id") or offer.get("slot_id", "")
    facility = fields.get("facility") or offer.get("facility", "")
    citizen = fields.get("citizen") or (
        message.sender if message.sender.startswith("citizen:") else ""
    )
    if message.sender.startswith("citizen:") and not hold_self:
        if not slot:
            # An offer id we never sent, or none at all: say which ones are open.
            await _say(city, message.sender, _phrase("unknown_offer", _lang(chat), chat=chat,
                                                     agent=agent, citizen=message.sender))
            return
        partner_slot = bool(facility) and facility not in _own_facilities(chat) and not (
            offer_id or f"of-{slot}"
        ).startswith("of-")
        if not consent and not partner_slot:
            await _ask_booking_consent(city, chat, agent, message.sender, offer_id or f"of-{slot}")
            return
        _BOOK_ASKED.pop(message.sender, None)
        if not consent:
            consent = "pending-cross-asst"  # replaced by the cross_asst ref below
    if not slot or not consent:
        return
    number = fields.get("prescription_number") or offer.get("prescription_number")
    if message.sender.startswith("agent:") and not hold_self and facility != agent:
        # Another agent confirms a slot we offered it: the facility books, not the desk (our
        # grant cannot hold). Forward it and send the receipt back to that agent.
        _CONFIRMED_BY[message.need_id or message.msg_id] = message.sender
        if facility and citizen:
            await _forward_confirm(city, message, chat, offer_id=offer_id or f"of-{slot}",
                                   slot=slot, citizen=citizen, consent=consent,
                                   facility=facility, number=number,
                                   cross=fields.get("cross_asst_consent_ref", ""))
        return
    book_here = hold_self or facility == agent
    if book_here:
        if not _we(chat, agent, f"slot_id={slot}", to=LEDGER, word="hold"):
            parts = ["hold", f"slot_id={slot}"]
            if citizen:
                parts.append(f"citizen={citizen}")
            if number:
                parts.append(f"prescription_number={number}")
            parts.append(f"consent_ref={consent}")
            if not await _say(city, LEDGER, " ".join(parts)):
                return
        await _finish_booking(city, message, chat, slot, hold_self=hold_self)
        return
    if not facility or not citizen:
        return
    offer_id = offer_id or offer.get("offer_id", "")
    cross = ""
    if facility not in _own_facilities(chat) and not offer_id.startswith("of-"):
        # A partner ASST's slot: no citizen data leaves before the cross_asst consent (P2).
        ref = _cross_consent_ref(chat, citizen)
        if not ref:
            await _ask_cross_consent(city, chat, agent, citizen, offer_id)
            return
        consent = cross = ref
    await _forward_confirm(
        city,
        message,
        chat,
        offer_id=offer_id,
        slot=slot,
        citizen=citizen,
        consent=consent,
        facility=facility,
        number=number,
        cross=cross,
    )


def _cross_consent_ref(chat: list[Message], citizen: str) -> str:
    """The citizen's latest cross_asst consent reference, if they gave one."""

    ref = ""
    for row in chat:
        if row.sender != citizen or "purpose=cross_asst" not in row.text.split():
            continue
        ref = fields_of(row.text).get("consent_ref", ref)
    return ref


# Citizen -> offer of our open cross_asst question, in case our own sent row is not found in
# the history (see _find_offer). Cleared when the question is answered.
_CROSS_ASKED: dict[str, str] = {}


def _open_cross_ask(chat: list[Message], agent: str, citizen: str = "") -> str:
    """The offer of our latest cross_asst consent question that is still unanswered."""

    open_id = _CROSS_ASKED.get(citizen, "") if citizen else ""
    for row in chat:
        if row.sender != agent:
            continue
        fields = fields_of(row.text)
        if first_word(row.text) == "consent" and "purpose=cross_asst" in row.text.split():
            open_id = fields.get("offer_id", "")
        elif open_id and fields.get("offer_id") == open_id and (
            first_word(row.text) in ("confirm", "cancel")
        ):
            open_id = ""
    return open_id


async def _ask_cross_consent(
    city: City, chat: list[Message], agent: str, citizen: str, offer_id: str
) -> None:
    """Ask once per offer for the consent a cross-ASST hold needs."""

    token = f"offer_id={offer_id}"
    if _CROSS_ASKED.get(citizen) == offer_id or any(
        row.sender == agent
        and first_word(row.text) == "consent"
        and "purpose=cross_asst" in row.text.split()
        and token in row.text.split()
        for row in chat
    ):
        return
    sentence = _CROSS_ASK_IT if _lang(chat) == "it" else _CROSS_ASK_EN
    if await _say(city, citizen, f"consent purpose=cross_asst {token} {sentence}"):
        _CROSS_ASKED[citizen] = offer_id


async def _on_cross_consent(city: City, message: Message, chat: list[Message]) -> None:
    """The citizen answered our cross_asst question: forward with the ref, or drop on a no."""

    agent = city.agent_id
    citizen = message.sender
    offer_id = _open_cross_ask(chat, agent, citizen)
    if not offer_id:
        return
    _CROSS_ASKED.pop(citizen, None)
    offer = _find_offer(chat, agent, offer_id)
    ref = fields_of(message.text).get("consent_ref", "")
    if ref:
        await _forward_confirm(
            city,
            message,
            chat,
            offer_id=offer_id,
            slot=offer.get("slot_id", ""),
            citizen=citizen,
            consent=ref,
            facility=offer.get("facility", ""),
            number=offer.get("prescription_number") or _number(chat),
            cross=ref,
        )
        return
    if not _we(chat, agent, f"offer_id={offer_id}", to=citizen, word="cancel"):
        sentence = _CROSS_NO_IT if _lang(chat) == "it" else _CROSS_NO_EN
        await _say(city, citizen, f"cancel offer_id={offer_id} reason=consent_declined {sentence}")


async def _forward_confirm(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    offer_id: str,
    slot: str,
    citizen: str,
    consent: str,
    facility: str,
    number: str | None,
    cross: str = "",
) -> None:
    """Send the citizen's choice to the facility that holds the slot; tell them if it fails.

    ``cross`` is the citizen's cross_asst consent: the shared ring format (Giambellino,
    Lorenteggio) reads it as ``cross_asst_consent_ref``.
    """

    agent = city.agent_id
    if not slot or not facility or not consent:
        return
    if _we(chat, agent, f"slot_id={slot}", to=facility, word="confirm"):
        return
    parts = [
        "confirm",
        f"offer_id={offer_id}",
        f"slot_id={slot}",
        f"citizen={citizen}",
        f"consent_ref={consent}",
        f"facility={facility}",
    ]
    if cross:
        parts.append(f"cross_asst_consent_ref={cross}")
    if number:
        parts.append(f"prescription_number={number}")
    code = await _send(city, facility, " ".join(parts))
    if code is None:
        if citizen.startswith("citizen:") and not message.sender.startswith("agent:"):
            _PENDING[citizen] = offer_id
        return
    # Hospital down (TARGET_NOT_VISIBLE) or another refusal: never resend a 4xx. Tell the
    # citizen, then offer what is still free at the other facilities.
    _LOG.info("care.forward.refused %s", code)
    if message.sender.startswith("agent:"):
        # Another ASST's confirm: tell that ASST, never their citizen directly.
        await _say(city, message.sender, f"cancel offer_id={offer_id} reason=facility_unavailable")
        return
    if not _we(chat, agent, f"offer_id={offer_id}", to=citizen, word="cancel"):
        why = _UNREACHABLE_IT if _lang(chat) == "it" else _UNREACHABLE_EN
        await _say(city, citizen, f"cancel offer_id={offer_id} reason=facility_unavailable {why}")
    await _advance(city, message, chat, fixed=None, replacing=True, avoid=facility)


async def _forward_receipt(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    booking_id = fields.get("booking_id", "")
    ledger_ref = fields.get("ledger_ref", "")
    partner = _CONFIRMED_BY.get(message.need_id or message.msg_id, "")
    if partner and booking_id and ledger_ref and message.sender != partner:
        # The booking was another ASST's: the receipt goes back to its booking centre.
        if booking_id not in _RECEIPTS:
            _RECEIPTS.add(booking_id)
            if not await _say(
                city, partner, f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref}"
            ):
                _RECEIPTS.discard(booking_id)
        return
    citizen = _citizen(chat)
    if not booking_id or not ledger_ref or not citizen:
        return
    _PENDING.pop(citizen, None)
    _BOOKED.add(citizen)
    token = f"booking_id={booking_id}"
    # Journey 000098 got the same receipt twice: our own sent row is not always found in the
    # history, so remember forwarded bookings in memory too.
    if booking_id in _RECEIPTS or _we(chat, city.agent_id, token, to=citizen, word="booking.receipt"):
        return
    _RECEIPTS.add(booking_id)
    sentence = _phrase("receipt", _lang(chat))
    if not await _say(
        city, citizen, f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref} {sentence}"
    ):
        _RECEIPTS.discard(booking_id)


_RECEIPTS: set[str] = set()


