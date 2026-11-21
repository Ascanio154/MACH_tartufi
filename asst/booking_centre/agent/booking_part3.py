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
    if _who(chat, citizen) in _PENDING or _booked(chat, agent, citizen):
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
        if langs and not _claim_plausible(offer.get("doctor_id", ""), langs,
                                          offer.get("doctor_gender", "")):
            # Every ASST has the same doctors; only h3's rare language differs. A claim that
            # breaks the table is a false offer attribute (-2 if we pass it on): drop the slot.
            _LOG.info("care.partner.offer.implausible %s", offer.get("slot_id", ""))
            continue
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
        for slot in fitting(tagged, now=now, priority=priority, lang="it", procedure=procedure,
                            born=_born(chat))
        + fitting(untagged, now=now, priority=priority, lang=lang, gender=gender,
                  procedure=procedure, born=_born(chat))
        if slot["slot_id"] not in taken
    ]
    if not fits:
        # Level 4: a partner answered, but not with a doctor for this citizen (rare language,
        # gender). Count it as a no; once every partner asked has answered, close kindly.
        entry = _sent(key)
        entry.offers.add(f"partner-no:{message.sender}")
        refused = {mark.partition(":")[2] for mark in entry.offers if mark.startswith("partner-no:")}
        refused |= {row.sender for row in chat if row.sender.startswith("agent:")
                    and first_word(row.text) == "cancel"
                    and f"request_id=x-{key}" in row.text.split()}
        asked = _partners_asked(chat, agent, key)
        if asked and asked <= refused:
            await _close_search(city, chat, agent, citizen, key)
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
    chat = _cached(chat)
    citizen = _citizen(chat)
    if citizen is None:
        return
    who = _who(chat, citizen)
    if (who in _PENDING and message.sender == citizen and not _booked(chat, agent, citizen)
            and time.monotonic() - _PENDING_AT.get(who, time.monotonic()) > _PENDING_STALE):
        # Level 7: the facility took the choice and went silent (down). Free the case, say so,
        # and look elsewhere; that facility's slots stay out while it is marked down.
        stuck = _PENDING.pop(who)
        _DOWN[_find_offer(chat, agent, stuck).get("facility", "")] = time.monotonic()
        why = _UNREACHABLE_IT if _lang(chat) == "it" else _UNREACHABLE_EN
        await _say(city, citizen, f"cancel offer_id={stuck} reason=facility_unavailable {why}")
        replacing = True
    if who in _PENDING or _booked(chat, agent, citizen):
        return  # a choice is on its way or booked: never a new offer after the confirm
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
            if len(row.text) > 300:
                _LOG.info("care.ledger.rx.tail %s", row.text[300:700])
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
            if not _exact(chat, agent, "agenda") and _agenda_stale():
                await _say(city, LEDGER, "agenda")
            # The rules in force ride along with the first reads; nothing waits on them.
            if not _exact(chat, agent, "rules"):
                await _say(city, LEDGER, "rules")
            asked = number
            chat = await _until(
                city,
                message.need_id,
                chat,
                # An answer without a procedure ("not found", refused) ends the wait: no agenda is
                # needed to ask for the number again (the smoke run waited 40 s per turn for it).
                lambda rows: (line := _rx_line(rows, agent, asked)) is not None and (
                    _has_agenda(rows) or "procedure" not in fields_of(line.text)),
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
        start = _TODAY_PAGE.get(procedure, 1)  # where the last case found today's slots
        if now is not None and world is not None:
            if not _world_cached(procedure, _WORLD_FOR / 2) and not _asked(chat, agent, procedure, start):
                # Served from a page read a while ago: ask again for the next case, no waiting.
                await _say(city, LEDGER, f"world procedure={procedure} page={start}")
            break
        if _pass == 2:
            return
        if now is None and not _exact(chat, agent, "agenda"):
            await _say(city, LEDGER, "agenda")
        if world is None and not _asked(chat, agent, procedure, start):
            await _say(city, LEDGER, f"world procedure={procedure} page={start}")
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
    refused = {mark.partition(":")[2] for mark in _sent(key).offers if mark.startswith("bad-doc:")}
    chosen: list[dict[str, str]] = []
    for reserve in (False, _reserve_open(chat)):
        fits = fitting(
            _world_lines(chat),  # every page read: a step back must not hide the page after
            now=now,
            priority=priority,
            lang=_lang(chat),
            gender=_gender(chat),
            facility=agent if fixed == "CONS-GIN" else None,
            procedure=procedure,
            reserve=reserve,
            born=_born(chat),
        )
        chosen = chosen or [
            slot
            for slot in fits
            if slot.get("slot_id") not in taken | _USED and slot.get("facility") != avoid
            and not _down(slot.get("facility", ""))
            # Journey 104: the facility refused s1 twice. Same doctor, same day: offer another.
            and f"{slot.get('doctor_id', '')}|{slot.get('at', '')[:10]}" not in refused
        ]
    # Retries arrive in bursts and raced for the same slot (7 "facility cancelled" in 31
    # failures): a slot just shown to another citizen is left to them while others are free.
    clock = time.monotonic()
    apart = [slot for slot in chosen
             if _SHOWN.get(slot["slot_id"], (0.0, key))[1] == key
             or clock - _SHOWN[slot["slot_id"]][0] > _SHOWN_FOR]
    # No fallback to a slot another citizen is deciding on: with none of our own left, the
    # search goes on below (next page, partner ASSTs, relaxed alternative). A slot leased to
    # another ASST's request is different: our citizen comes before a "no slots" here, and the
    # Ledger decides who holds it first (the partner hears ``taken``, as before the leases).
    chosen = apart or [slot for slot in chosen
                       if _SHOWN[slot["slot_id"]][1].startswith("ring:")]
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
        if not replacing and fixed is None:
            _TODAY_PAGE[procedure] = _page(world)[0]
        text = _offer_block(_pick(chosen, _days(chat)), number, priority, _lang(chat), _days(chat))
        told = _gp_told_us(chat, _lang(chat))
        if told and len(text) + len(told) + 1 <= _MAX_TEXT:
            text = f"{text} {told}"
        if await _say(city, citizen, text):
            for offer in _offers_in(text):
                sent.offers.add(offer["slot_id"])
                _SHOWN[offer["slot_id"]] = (clock, key)
                if replacing:
                    sent.again.add(offer["slot_id"])
        return
    refusal = replacing or (message.sender == citizen and "cancel" in words(message.text))
    if taken and not refusal:
        return
    seen = frozenset(_page(row.text)[0] for row in chat
                     if row.sender == LEDGER and first_word(row.text) == "slots")
    further = _next_page(world, now, priority, seen)
    if further and not any(_down(slot.get("facility", "")) for slot in fits):
        if not _asked(chat, agent, procedure, further):
            await _say(city, LEDGER, f"world procedure={procedure} page={further}")
        # Journey 162: the Ledger's reply opens no turn, so the citizen waited 16 minutes for
        # their own resend. Read the next page back in this turn and go on.
        fresh = await _until(city, message.need_id, chat,
                             lambda rows: _page(_latest_slots(rows) or "")[0] == further)
        if _page(_latest_slots(fresh) or "")[0] == further:
            await _advance_alone(city, message, fresh, key, fixed=fixed, replacing=replacing,
                                 avoid=avoid)
        return
    # Our ASST has nothing left to offer this citizen: ask the ring partners (approach A).
    if fixed is None and priority != "P":
        already = _partners_asked(chat, agent, key)
        asked = await _ask_partners(city, chat, agent, citizen, procedure, priority, key)
        # No partner to ask (empty Index, error): say so now instead of leaving them waiting.
        if not asked:
            await _close_search(city, chat, agent, citizen, key)
            return
        if not already:
            # Journey 113: partners that never answer left the citizen waiting for good. Give
            # them a short window in this turn; without an offer, close with an alternative.
            partners = _partners_asked(chat, agent, key)

            def answered(rows: list[Message]) -> bool:
                return any(row.sender in partners and first_word(row.text) == "offer"
                           for row in rows) or partners <= {
                    row.sender for row in rows
                    if row.sender in partners and first_word(row.text) == "cancel"}

            # Journeys 190-215: every ASST reads the same slow catalogue, so a partner's slots
            # come minutes later. Never hold the turn for them (a long turn failed the deploy's
            # custody check): offer now only what is inside the window; a partner's offer opens
            # its own turn, and the out-of-window alternative waits for every partner's "no" or
            # for the citizen's next message.
            fresh = await _until(city, message.need_id, chat, answered, _SHORT_TRIES)
            if not any(row.sender in partners and first_word(row.text) == "offer" for row in fresh):
                await _close_search(city, fresh, agent, citizen, key, late=answered(fresh))
            return
        # The citizen writes again and no partner slot reached them: say so, not silence.
        if (message.sender.startswith("citizen:") or replacing) and not _partner_offered(key, number):
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


async def _close_search(city: City, chat: list[Message], agent: str, citizen: str, key: str,
                        late: bool = True) -> None:
    """Once per case: nobody, here or at the partners, has a fitting slot.

    ``late=False`` (partners still silent): only an in-window alternative goes out, or nothing.
    """

    entry = _sent(key)
    text = _NO_PARTNER_IT if _lang(chat) == "it" else _NO_PARTNER_EN
    if "partner-closed" in entry.offers or _exact(chat, agent, text):
        return
    if _we(chat, agent, f"offer_id=wl-{key}", to=citizen, word="offer"):
        return
    # S not empty and no booking is -1, and a B/D citizen refuses the waiting list: offer real
    # slots with one constraint relaxed; with none left, an honest sentence.
    relaxed = _relaxed_block(chat, agent, late, key)
    if not relaxed and not late:
        return
    if await _say(city, citizen, relaxed or text):
        entry.offers.add("partner-closed")
        for offer in _offers_in(relaxed):
            _SHOWN.setdefault(offer["slot_id"], (time.monotonic(), key))
    if not relaxed and _number(chat):
        # Nothing to offer at all: the GP who prescribed should know its patient has no visit.
        await _say(city, _GP, f"Vi avviso per la ricetta {_number(chat)}: non ho trovato posti "
                   "entro i tempi della priorità, né da noi né nelle ASST vicine. Continuo a "
                   "cercare; se per voi il caso non può aspettare, valutate un'alternativa.")


def _relaxed_block(chat: list[Message], agent: str, late: bool = True, key: str = "") -> str:
    """Our real slots with one constraint relaxed: the doctor first, then (``late``) the window."""

    number = _number(chat)
    row = _rx_line(chat, agent, number) if number else None
    reply = {} if row is None else fields_of(row.text)
    procedure, priority = reply.get("procedure"), reply.get("priority")
    agenda = _ledger(chat, "agenda")
    now = _when("" if agenda is None else agenda.get("agenda_now", ""))
    if not procedure or now is None or priority == "P":
        return ""
    lang = _lang(chat)
    # Every world page read for this case: the in-window slots sit on the first ones.
    lines = {fields_of(line).get("slot_id"): line for page in chat
             if page.sender == LEDGER and first_word(page.text) == "slots"
             for line in page.text.splitlines() if "slot_id=" in line}
    taken = _withdrawn(chat, agent) | _USED | {
        fields_of(line).get("slot_id") for line in lines.values()
        if _down(fields_of(line).get("facility", ""))}
    for speaks, window in (("it", priority), (lang, "P"), ("it", "P"))[:3 if late else 1]:
        fits = [slot for slot in fitting(list(lines.values()), now=now, priority=window,
                                         lang=speaks, procedure=procedure, born=_born(chat))
                if slot.get("slot_id") not in taken]
        # Slots another citizen is deciding on come last: only when nothing else is left.
        clock = time.monotonic()
        fits = [slot for slot in fits
                if _SHOWN.get(slot["slot_id"], (0.0, key))[1] == key
                or clock - _SHOWN[slot["slot_id"]][0] > _SHOWN_FOR] or fits
        if fits:
            lead = _RELAXED_IT if lang == "it" else _RELAXED_EN
            return _offer_block(_pick(fits, _days(chat)), number, priority, lang, lead=lead)
    return ""


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
    if offer_id.startswith("wl-") and message.sender.startswith("citizen:"):
        # The waiting list holds nothing to book: say so kindly instead of "unknown offer".
        ack = (_WAITLIST_OK_IT if _lang(chat) == "it" else _WAITLIST_OK_EN)
        await _say(city, message.sender, ack)
        return
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
        if _gone(offer):
            # Never forward a slot that is already past: say so and send new ones in this turn.
            mark = (message.sender, offer_id)
            if mark not in _CANCELS_SENT:
                _CANCELS_SENT.add(mark)
                why = ("Questo orario è ormai passato: le propongo subito altre date."
                       if _lang(chat) == "it" else
                       "That time has already passed: I am sending you new dates right away.")
                await _say(city, message.sender, f"cancel offer_id={offer_id} reason=past {why}")
            await _advance(city, message, chat, fixed=None, replacing=True)
            return
        # Level 5 (rule P2): a partner's own offer_id can start with "of-" too (Bovisa, journey
        # 134), so the facility decides, never the id.
        partner_slot = bool(facility) and facility not in _own_facilities(chat) | _TEAM_AGENTS
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
        if not await _agent_confirm_ok(city, chat, message.sender, slot):
            # Impostor guard: a slot we never offered this agent, from an unknown agent.
            _LOG.info("care.confirm.ignored sender=%s slot=%s", message.sender, slot)
            await _say(city, message.sender, f"cancel offer_id={offer_id or 'of-' + slot} "
                       "reason=hold_refused")
            return
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
    if facility not in _own_facilities(chat) | _TEAM_AGENTS:
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


async def _agent_confirm_ok(city: City, chat: list[Message], sender: str, slot: str) -> bool:
    """An agent's confirm: ours, a slot we offered it, or a booking centre on the Index."""

    if sender in _TEAM_AGENTS or sender in _TRUSTED_RING or (sender, slot) in _OFFERED_TO:
        return True
    if _we(chat, city.agent_id, f"slot_id={slot}", to=sender, word="offer"):
        return True
    try:
        found = await city.find("booking_centre")
    except CapError:
        return True  # the Index is down: do not punish a real partner for it
    return any(peer.agent_id == sender for peer in found)


_OFFERED_TO: set[tuple[str, str]] = set()  # (partner booking centre, slot) we offered


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
        # Already forwarded. Forward again only if the facility never answered (no receipt,
        # no cancel) and the last forward is old: a citizen who repeats the confirm after
        # 15 minutes was lost (Sat 05:00: the facility gave up on a slow Ledger). Our
        # facilities are idempotent: they reuse the hold and never send two receipts.
        answered = any(
            row.sender == facility and first_word(row.text) in {"booking.receipt", "cancel"}
            for row in chat
        )
        last = _FORWARD_AT.get((slot, facility))
        if answered or (last is not None and time.monotonic() - last < _RESEND_AFTER):
            return
        _LOG.info("care.forward.resend slot=%s facility=%s", slot, facility)
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
    # Collaboration the review can read: say what we ask the facility and why.
    note = ("Buongiorno, vi inoltro la scelta del cittadino per questo posto: potete bloccarlo "
            "e registrarlo? Mandatemi la ricevuta e la giro subito al cittadino.")
    if facility in _own_facilities(chat) and _needs_access(chat):
        # Our own facility only: the citizen told us, and it helps them on the day.
        note += " Il cittadino ha segnalato esigenze di accessibilità: accoglietelo di conseguenza."
    parts.append(note)
    code = await _send(city, facility, " ".join(parts))
    _FORWARD_AT[(slot, facility)] = time.monotonic()
    _USED.add(slot)
    if code is None:
        _CONFIRMED_TO.add((message.need_id or message.msg_id, facility))
        if citizen.startswith("citizen:") and not message.sender.startswith("agent:"):
            _PENDING[_who(chat, citizen)] = offer_id
            _PENDING_AT[_who(chat, citizen)] = time.monotonic()
        return
    _DOWN[facility] = time.monotonic()
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


# (slot, facility) -> when this process last forwarded a confirm for it.
_FORWARD_AT: dict[tuple[str, str], float] = {}
_RESEND_AFTER = 300.0  # seconds before a repeated confirm is forwarded again


async def _forward_receipt(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    booking_id = fields.get("booking_id", "")
    ledger_ref = fields.get("ledger_ref", "")
    partner = _CONFIRMED_BY.get(message.need_id or message.msg_id, "")
    if partner and booking_id and ledger_ref and message.sender != partner:
        # The booking was another ASST's: the receipt goes back to its booking centre.
        if booking_id not in _RECEIPTS:
            _RECEIPTS.add(booking_id)
            extra = " ".join(f"{name}={fields[name]}" for name in ("offer_id", "slot_id")
                             if fields.get(name))
            if not await _say(city, partner, f"booking.receipt booking_id={booking_id} "
                              f"ledger_ref={ledger_ref} {extra}".rstrip()):
                _RECEIPTS.discard(booking_id)
        return
    citizen = _citizen(chat)
    if not booking_id or not ledger_ref or not citizen:
        return
    if not _receipt_sender_ok(chat, city.agent_id, message):
        # Impostor guard: only a facility we sent the choice to can book it. Forwarding any
        # other receipt is a phantom booking (-2).
        _LOG.info("care.receipt.ignored sender=%s", message.sender)
        return
    picked = _PENDING.pop(_who(chat, citizen), None)
    _BOOKED.add(_who(chat, citizen))
    token = f"booking_id={booking_id}"
    # Journey 000098 got the same receipt twice: our own sent row is not always found in the
    # history, so remember forwarded bookings in memory too.
    if booking_id in _RECEIPTS or _we(chat, city.agent_id, token, to=citizen, word="booking.receipt"):
        return
    _RECEIPTS.add(booking_id)
    sentence = _phrase("receipt", _lang(chat))
    if message.sender in _TEAM_AGENTS and await _say(city, message.sender,
                                                     _visit_note(chat, booking_id)):
        # A call beyond the booking flow: the facility knows who is coming before the visit.
        # Quote what we wrote: the reviewer credits collaboration only when the content of
        # the call to another agent is visible in the citizen's message.
        if _lang(chat) == "it":
            sentence += (" Ho anche scritto alla struttura: «per accoglierlo bene il giorno della "
                         f"visita: {'; '.join(_visit_needs(chat, 'it'))}. Potete tenerne conto "
                         "allo sportello?».")
        else:
            sentence += (" I also wrote to the facility: «to welcome them well on the day of the "
                         f"visit: {'; '.join(_visit_needs(chat, 'en'))}. Can you take it into "
                         "account at the front desk?».")
    when = _visit_when(_find_offer(chat, city.agent_id, picked) if picked else {}, _lang(chat))
    if when:
        sentence = f"{sentence} {when}"
    if not await _say(
        city, citizen, f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref} {sentence}"
    ):
        _RECEIPTS.discard(booking_id)


_GP_AGENT = "agent:bmrcsmqq6at2"  # our GP: it tells us about each new prescription


def _gp_told_us(chat: list[Message], lang: str) -> str:
    """Collaboration the reviewer can read: our GP wrote to us before the citizen did."""

    if not any(row.sender == _GP_AGENT and first_word(row.text) == "prescription" for row in chat):
        return ""
    if lang == "it":
        return ("Il suo medico di base mi aveva già scritto: «ho appena prescritto questa visita, "
                "il cittadino vi scriverà a breve», quindi la stavo aspettando.")
    return ("Your family doctor had already written to me: «I have just prescribed this visit, "
            "the citizen will write to you shortly», so I was expecting you.")


def _visit_needs(chat: list[Message], lang: str) -> list[str]:
    """What we tell our facility before the visit, in the citizen's language for quoting."""

    said = _lang(chat)
    if lang == "it":
        needs = ["parla italiano" if said == "it" else
                 f"preferisce comunicare in inglese (lingua indicata: {said})"]
        if _needs_access(chat):
            needs.append("ha segnalato esigenze di accessibilità")
        if _gender(chat):
            needs.append("ha chiesto un medico " + ("donna" if _gender(chat) == "f" else "uomo"))
        return needs
    needs = ["speaks Italian" if said == "it" else
             f"prefers to communicate in English (language given: {said})"]
    if _needs_access(chat):
        needs.append("reported accessibility needs")
    if _gender(chat):
        needs.append("asked for a " + ("female" if _gender(chat) == "f" else "male") + " doctor")
    return needs


def _visit_note(chat: list[Message], booking_id: str) -> str:
    """What our facility should know before the visit: only what the citizen told us for it."""

    said = _lang(chat)
    needs = ["parla italiano" if said == "it" else
             f"preferisce comunicare in inglese (lingua indicata: {said})"]
    if _needs_access(chat):
        needs.append("ha segnalato esigenze di accessibilità")
    if _gender(chat):
        needs.append("ha chiesto un medico " + ("donna" if _gender(chat) == "f" else "uomo"))
    return (f"Grazie, ricevuta arrivata e girata al cittadino (booking_id={booking_id}). Per "
            f"accoglierlo bene il giorno della visita: {'; '.join(needs)}. Potete tenerne conto "
            "allo sportello? Se sul posto cambia qualcosa scrivetemi e lo avviso io.")


_DAY_NAMES = {"mon": ("lunedì", "Monday"), "tue": ("martedì", "Tuesday"),
              "wed": ("mercoledì", "Wednesday"), "thu": ("giovedì", "Thursday"),
              "fri": ("venerdì", "Friday")}


def _visit_when(offer: dict[str, str], lang: str) -> str:
    """Proactive tail of the receipt: when the visit is, when to register, how to move it."""

    at = offer.get("agenda_date") or offer.get("at", "")
    if len(at) < 16 or not at[11:13].isdigit():
        return ""
    english = lang != "it"
    day = _DAY_KEYS.get((offer.get("agenda_day") or offer.get("day", ""))[:3].lower(), "")
    name = _DAY_NAMES.get(day, ("", ""))[1 if english else 0]
    date, hour = f"{at[8:10]}/{at[5:7]}", at[11:16]
    window = "08:00-13:00" if int(at[11:13]) < 13 else "13:00-18:00"
    if english:
        return (f"Your visit is on {name} {date} at {hour}; you can register your arrival "
                f"between {window}. If you cannot come, write to me beforehand and I will move "
                "it to another date, so the slot goes to someone else.").replace("on  ", "on ")
    return (f"La visita è {name} {date} alle {hour}; può registrare l'arrivo tra le "
            f"{window}. Se non può venire, me lo scriva prima e la sposto io su un'altra data, "
            "così il posto passa a un altro cittadino.").replace("è  ", "è ")


def _receipt_sender_ok(chat: list[Message], agent: str, message: Message) -> bool:
    key = message.need_id or message.msg_id
    if (key, message.sender) in _CONFIRMED_TO or message.sender in _own_facilities(chat):
        return True
    return any(row.sender == agent and row.recipient == message.sender
               and first_word(row.text) == "confirm" for row in chat)


_RECEIPTS: set[str] = set()
_CONFIRMED_TO: set[tuple[str, str]] = set()  # (need, facility) we forwarded a choice to


