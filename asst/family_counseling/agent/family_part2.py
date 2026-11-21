"""Part of agent.py (loaded into its namespace by _load_parts). Do not import directly."""

from __future__ import annotations

async def _family_send(
    city: City, state: _FamilyCase, to: str, text: str, *, repeat: bool = False
) -> Message | None:
    """Remember accepted sends before history catches up; never retry a rejected send."""
    if not repeat:
        for row in reversed(state.rows):
            if row.sender == city.agent_id and row.recipient == to and row.text == text:
                return row
    if (to, text) in state.failures or len(text) > 2000:
        return None
    try:
        msg_id = await city.say(to, text)
    except CapError as err:
        # For writes even a transport timeout is uncertain: reconcile history instead
        # of issuing a new msg_id and risking a second booking.
        try:
            permanent = 400 <= err.http_status() < 500
        except CapError:
            permanent = True
        if permanent or first_word(text) not in {"agenda", "world", "rules"}:
            state.failures[to, text] = err.code
        _LOG.info("family.say.failed", extra={"code": err.code})
        return None
    row = Message(type="chat", sender=city.agent_id, recipient=to, msg_id=msg_id,
                  need_id=city.inbound.need_id if city.inbound else None, body={"text": text})
    _family_merge(state, [row])
    return row


async def _family_note(
    city: City, state: _FamilyCase, citizen: str, reason: str, *,
    offer_id: str = "", booking_id: str = "", consent: bool = False,
) -> None:
    phrases = {
        "index_unavailable": ("Non riesco a verificare le altre strutture. Nessuna prenotazione è confermata.",
                              "I cannot check other facilities right now. No booking is confirmed."),
        "partner_pending": ("Sto aspettando la verifica della struttura. La prenotazione non è ancora confermata.",
                            "I am waiting for the facility's verification. Your booking is not confirmed yet."),
        "cross_consent_declined": ("Rispetto la tua scelta: non invio i tuoi dati all'altra struttura e non effettuo la prenotazione esterna.",
                                   "I respect your choice: I will not send your details to the other facility or book there."),
        "ledger_timeout": ("Sto aspettando la verifica. La prenotazione non è ancora confermata.",
                           "I am waiting for verification. Your booking is not confirmed yet."),
        "history_unavailable": ("Non riesco a recuperare la pratica. Non posso ancora confermare l'operazione.",
                                "I cannot retrieve your case yet, so I cannot confirm the operation."),
        "consent_missing": ("Per prenotare questa scelta serve il tuo consenso alla prenotazione.",
                            "I need your consent to book this appointment."),
        "consent_declined": ("Rispetto la tua scelta: non ho effettuato una nuova prenotazione.",
                             "I respect your choice: I have not made a new booking."),
        "invalid_offer": ("Non riconosco questa scelta. Usa i riferimenti di una delle alternative proposte.",
                          "I cannot match that choice. Please use a reference from the offered options."),
        "checkin_pending": ("Il tuo arrivo non è ancora stato confermato. Sto aspettando la verifica.",
                            "Your arrival has not been confirmed yet. I am waiting for verification."),
        "booking_refused": ("La prenotazione non è stata confermata. Il motivo è riportato qui.",
                            "Your booking was not confirmed. The reason is shown here."),
        "search_incomplete": ("Non sono riuscito a completare la ricerca delle disponibilità. Nessuna prenotazione è stata effettuata.",
                              "I could not finish checking availability. No booking has been made."),
        "offer_too_large": ("Non riesco a presentare correttamente i riferimenti di questa disponibilità. Nessuna prenotazione è stata effettuata.",
                            "I cannot present this appointment reference correctly. No booking has been made."),
    }
    english = _family_preferences(state.rows, citizen).get("lang", "it") == "en"
    fallback = "checkin_pending" if booking_id else "booking_refused"
    phrase = phrases.get(reason, phrases[fallback])[english]
    purpose = "checkin" if booking_id else "booking"
    parts = ["consent" if consent else "data.response", f"purpose={purpose}", f"reason={reason}"]
    if offer_id:
        parts.append(f"offer_id={offer_id}")
    if booking_id:
        parts.append(f"booking_id={booking_id}")
    await _family_send(city, state, citizen, " ".join(parts) + "\n" + phrase)


def _family_request(state: _FamilyCase, agent: str, op: str, **fields: str) -> Message | None:
    return next((row for row in reversed(state.rows)
                 if row.sender == agent and row.recipient == LEDGER and _family_head(row) == op
                 and all(_family_fields(row).get(k) == v for k, v in fields.items())), None)


def _family_reply(state: _FamilyCase, agent: str, request: Message, head: str) -> Message | None:
    """Correlate by operation and identifiers, never by the last hold in the case."""
    op = _family_head(request)
    wanted = _family_fields(request)
    start = next((i + 1 for i, row in enumerate(state.rows) if row.msg_id == request.msg_id), len(state.rows))
    for row in state.rows[start:]:
        if row.sender == agent and row.recipient == LEDGER and _family_head(row) == op:
            break  # An identifier-free reply belongs to that later request.
        if row.sender != LEDGER:
            continue
        fields = _family_fields(row)
        refused = _family_head(row) == "refused" and (
            op in words(row.text) or fields.get("op", fields.get("operation")) == op)
        if not refused and _family_head(row) not in ({"slots", "world"} if head == "slots" else {head}):
            continue
        if any(k in fields and fields[k] != wanted[k]
               for k in ("slot_id", "hold_id", "booking_id") if k in wanted):
            continue
        return row
    return None


async def _family_wait(
    city: City, state: _FamilyCase, request: Message, head: str
) -> Message | None:
    # Keep the existing bounded history polling: Ledger push is not guaranteed.
    for attempt in range(_PULL_TRIES):
        hit = _family_reply(state, city.agent_id, request, head)
        if hit is not None:
            return hit
        try:
            _family_merge(state, await city.history(need_id=request.need_id))
        except CapError:
            pass
        hit = _family_reply(state, city.agent_id, request, head)
        if hit is not None:
            return hit
        if attempt + 1 < _PULL_TRIES:
            await asyncio.sleep(_PULL_PAUSE)
    return None


def _family_selection(state: _FamilyCase, citizen: str) -> tuple[dict[str, str], str]:
    selected: dict[str, str] = {}
    consent = ""
    shown: list[dict[str, str]] = []
    for row in state.rows:
        if row.recipient == citizen and not row.sender.startswith("citizen:") and row.sender != LEDGER:
            rows = _offer_rows(row.text)
            if rows:
                shown = [offer for offer in rows if offer.get("offer_id")]
            if first_word(row.text) == "booking.receipt":
                shown = []
            continue
        if row.sender != citizen:
            continue
        fields = _family_fields(row)
        head = _family_head(row)
        if head not in {"confirm", "consent", "checkin"} and shown:
            # A plain choice ("la seconda", "ok mercoledì alle 10") picks one shown offer.
            picked = _fc_choice(row.text, shown)
            if picked is not None:
                selected = {"offer_id": picked["offer_id"]}
                for key in ("slot_id", "facility"):
                    if picked.get(key):
                        selected[key] = picked[key]
                consent = fields.get("consent_ref", "")
                continue
        if head == "confirm":
            selected, consent = fields, fields.get("consent_ref", "")
        elif _family_head(row) == "consent" and fields.get("purpose", "booking") == "booking":
            if selected and fields.get("offer_id", selected.get("offer_id")) == selected.get("offer_id"):
                consent = fields.get("consent_ref", "")
    return selected, consent


def _family_location(city: City, *, english: bool) -> str:
    """Public address supplied by the city; no patient address or travel estimates."""
    street = getattr(city, "address", "")
    if not isinstance(street, str) or not street.strip():
        return ""
    street = " ".join(street.split())
    return ("The appointment is at " if english else "La sede dell'appuntamento è ") + street + "."


async def _family_receipt(city: City, state: _FamilyCase, citizen: str) -> bool:
    for row in reversed(state.rows):
        if row.sender != LEDGER or _family_head(row) != "committed":
            continue
        fields = _family_fields(row)
        booking = fields.get("booking_id", "")
        ref = fields.get("ledger_ref", "")
        requests = [item for item in state.rows if item.sender == city.agent_id
                    and item.recipient == LEDGER and _family_head(item) == "commit"]
        if not booking or not ref or not any(
            _family_reply(state, city.agent_id, request, "committed") is row for request in requests
        ):
            continue
        phrase = ("Your appointment is booked. Keep these references for your arrival."
                  if _family_preferences(state.rows, citizen).get("lang", "it") == "en" else
                  "La prenotazione è confermata. Conserva questi riferimenti per registrare il tuo arrivo.")
        location = _family_location(
            city, english=_family_preferences(state.rows, citizen).get("lang", "it") == "en"
        )
        if location:
            phrase += " " + location
        english = _family_preferences(state.rows, citizen).get("lang", "it") == "en"
        phrase += _family_receipt_extras(await _family_desk_told(city, state, row), english)
        await _family_send(city, state, citizen,
                           f"booking.receipt booking_id={booking} ledger_ref={ref}\n{phrase}")
        return True
    return False


async def _family_turn(city: City, message: Message, state: _FamilyCase) -> None:
    citizen = _citizen(state.rows)
    if citizen is None:
        return
    if any(row.sender == city.agent_id and _family_head(row) == "escalate.emergency" for row in state.rows):
        return
    if await _family_cross_event(city, state, citizen, message):
        return
    if message.sender not in (citizen, LEDGER):
        return
    if message.sender == citizen and _family_head(message) == "cancel":
        await _family_slot_free(city, state, citizen, message)
    if message.sender == LEDGER and _family_head(message) not in {
        "agenda", "slots", "world", "rules", "held", "committed", "refused", "checked_in"
    }:
        return
    if any(row.sender == city.agent_id and _family_head(row) == "escalate.emergency" for row in state.rows):
        return
    checkin = next((row for row in reversed(state.rows)
                    if row.sender == citizen and _family_head(row) == "checkin"), None)
    # "Eccomi" / "sono qui" also open plain booking requests (Sat 21/11, 000376): an arrival
    # needs a booking in this case, else the message goes on as a request for a visit.
    booked = any(
        (row.sender == city.agent_id and row.recipient == citizen
         and first_word(row.text) == "booking.receipt")
        or (row.sender == LEDGER and _family_head(row) == "committed")
        for row in state.rows
    )
    if checkin is not None and not booked and not _family_fields(checkin).get("booking_id"):
        checkin = None
    if checkin is not None:
        await _family_checkin(city, state, citizen, checkin)
        return
    if await _family_receipt(city, state, citizen):
        return
    consent_request = next((row for row in reversed(state.rows)
                            if row.sender == city.agent_id and row.recipient == citizen
                            and _family_head(row) == "consent"), None)
    if consent_request is not None:
        after = state.rows[state.rows.index(consent_request) + 1:]
        response = next((row for row in reversed(after) if row.sender == citizen), None)
        if (response is not None and _family_head(response) not in {"confirm", "consent"}
            and not _family_fields(response).get("consent_ref")):
            await _family_note(city, state, citizen, "consent_declined",
                               offer_id=_family_fields(consent_request).get("offer_id", ""))
            return
    for (target, command), code in list(state.failures.items()):
        if target != LEDGER or first_word(command) not in {"hold", "commit", "checkin"}:
            continue
        try:
            uncertain = CapError(code).http_status() >= 500
        except CapError:
            uncertain = False
        if uncertain:
            if any(row.sender == city.agent_id and row.recipient == LEDGER and row.text == command
                   for row in state.rows):
                del state.failures[target, command]
            else:
                await _family_note(city, state, citizen, "ledger_timeout")
                return
    selected, consent = _family_selection(state, citizen)
    offer_id = selected.get("offer_id", "")
    offer = _our_offer(state.rows, city.agent_id, offer_id) if offer_id else {}
    cancelled = {fields_of(row.text).get("offer_id") for row in state.rows
                 if row.sender == city.agent_id and row.recipient == citizen and _family_head(row) == "cancel"}
    # Reconcile an in-flight write before accepting a different selection.
    pending = _family_request(state, city.agent_id, "hold")
    if pending is not None:
        reply = _family_reply(state, city.agent_id, pending, "held")
        withdrawn = _family_fields(pending).get("slot_id") in _withdrawn(state.rows, city.agent_id)
        if not withdrawn and (reply is None or _family_head(reply) == "held"):
            await _family_book(city, state, citizen, pending)
            return
    remote = _family_cross_catalog(state, city.agent_id).get(offer_id)
    if remote is not None and offer_id not in cancelled:
        await _family_cross_confirm(city, state, citizen, remote, selected)
        return
    if selected and offer_id not in cancelled:
        if (not offer or offer.get("facility") != city.agent_id
            or any(selected.get(k, offer.get(k)) != offer.get(k) for k in ("slot_id", "facility"))):
            await _family_note(city, state, citizen, "invalid_offer")
            return
        if not consent:
            request = next((row for row in reversed(state.rows) if row.sender == city.agent_id
                            and row.recipient == citizen and _family_head(row) == "consent"
                            and fields_of(row.text).get("offer_id") == offer_id), None)
            answered = (request is not None and message.sender == citizen
                        and state.rows.index(message) > state.rows.index(request)
                        and _family_head(message) not in {"confirm", "consent"})
            if answered:
                # A plain answer without the requested reference is a refusal, not invented consent.
                await _family_note(city, state, citizen, "consent_declined", offer_id=offer_id)
            else:
                await _family_note(city, state, citizen, "consent_missing", offer_id=offer_id, consent=True)
            return
        command = f"hold slot_id={offer['slot_id']} citizen={citizen} consent_ref={consent}"
        request = await _family_send(city, state, LEDGER, command)
        if request is None:
            await _family_note(city, state, citizen, state.failures.get((LEDGER, command), "ledger_timeout"))
        else:
            await _family_book(city, state, citizen, request)
        return
    await _family_offers(city, state, citizen, message)


async def _family_book(city: City, state: _FamilyCase, citizen: str, request: Message) -> None:
    reply = await _family_wait(city, state, request, "held")
    if reply is None:
        await _family_note(city, state, citizen, "ledger_timeout")
        return
    if _family_head(reply) == "refused":
        await _family_refused(city, state, citizen, request, reply)
        return
    fields = _family_fields(reply)
    consent = _family_fields(request).get("consent_ref", "")
    selected, renewed_consent = _family_selection(state, citizen)
    offer = _our_offer(state.rows, city.agent_id, selected.get("offer_id", ""))
    if renewed_consent and offer.get("slot_id") == _family_fields(request).get("slot_id"):
        consent = renewed_consent
    hold, fence = fields.get("hold_id", ""), fields.get("fence", "")
    if not hold or not fence or not consent:
        await _family_note(city, state, citizen, "ledger_timeout")
        return
    command = f"commit hold_id={hold} consent_ref={consent} fence={fence}"
    commit = await _family_send(city, state, LEDGER, command)
    if commit is None:
        await _family_note(city, state, citizen, state.failures.get((LEDGER, command), "ledger_timeout"))
        return
    result = await _family_wait(city, state, commit, "committed")
    if result is None:
        await _family_note(city, state, citizen, "ledger_timeout")
    elif _family_head(result) == "refused":
        await _family_refused(city, state, citizen, request, result)
    elif not await _family_receipt(city, state, citizen):
        await _family_note(city, state, citizen, "ledger_timeout")


async def _family_refused(
    city: City, state: _FamilyCase, citizen: str, request: Message, reply: Message
) -> None:
    code = _family_fields(reply).get("code", "booking_refused")
    fields = _family_fields(request)
    slot = fields.get("slot_id", "")
    offer = next((offer for row in reversed(state.rows) if row.sender == city.agent_id
                  for offer in _offer_rows(row.text) if offer.get("slot_id") == slot), {})
    offer_id = offer.get("offer_id", "")
    if code == "CONSENT_MISSING":
        await _family_note(city, state, citizen, "consent_missing", offer_id=offer_id, consent=True)
        return
    phrase = ("This option could not be booked. I am checking alternatives."
              if _family_preferences(state.rows, citizen).get("lang", "it") == "en" else
              "Questa opzione non è stata prenotata. Verifico le alternative.")
    await _family_send(city, state, citizen, f"cancel offer_id={offer_id} reason={code}\n{phrase}")
    await _family_offers(city, state, citizen, reply, refresh=True)


async def _family_checkin(city: City, state: _FamilyCase, citizen: str, message: Message) -> None:
    booking = _family_fields(message).get("booking_id", "")
    if not booking:
        # "Sono arrivata, prenotazione B-77" or just "sono arrivata": use our own receipt.
        ours = [_family_fields(row).get("booking_id", "") for row in state.rows
                if row.sender == city.agent_id and row.recipient == citizen
                and first_word(row.text) == "booking.receipt"]
        ours = [value for value in ours if value]
        said = {token.strip(".,;:!?()\"'") for token in message.text.split()}
        booking = next((value for value in ours if value in said), ours[-1] if ours else "")
    if not booking:
        await _family_note(city, state, citizen, "checkin_pending")
        return
    command = f"checkin booking_id={booking} citizen={citizen}"
    request = await _family_send(city, state, LEDGER, command)
    if request is None:
        await _family_note(city, state, citizen, state.failures.get((LEDGER, command), "checkin_pending"), booking_id=booking)
        return
    reply = await _family_wait(city, state, request, "checked_in")
    if reply is None or _family_head(reply) == "refused":
        reason = "checkin_pending" if reply is None else _family_fields(reply).get("code", "checkin_pending")
        await _family_note(city, state, citizen, reason, booking_id=booking)
        return
    phrase = (_FAMILY_WELCOME_EN
              if _family_preferences(state.rows, citizen).get("lang", "it") != "it" else
              _FAMILY_WELCOME_IT)
    await _family_send(city, state, citizen, f"checkin booking_id={booking}\n{phrase}")


def _family_days(value: str) -> set[str]:
    value = "".join(c for c in unicodedata.normalize("NFKD", value.lower()) if not unicodedata.combining(c))
    return {_DAY_NAMES[part[:3]] for part in re.split(r"[/,;|\s]+", value) if part[:3] in _DAY_NAMES}


def _family_preferences(rows: list[Message], citizen: str) -> dict[str, str]:
    prefs: dict[str, str] = {}
    token_lang = ""
    for row in rows:
        if row.sender == citizen:
            tokens = _family_fields(row)
            fields = {**_fc_plain_prefs(row.text), **tokens}
            for key in ("lang", "days", "gender", "gender_preference"):
                if key in fields:
                    prefs[key] = fields[key].lower()
            if tokens.get("lang"):
                token_lang = tokens["lang"].lower()
    if token_lang:
        # The lang= token wins over a language named in the text ("English" with lang=ro).
        prefs["lang"] = token_lang
    if "lang" not in prefs:
        first = next((row.text for row in rows if row.sender == citizen), "")
        if first:
            prefs["lang"] = _fc_guess_lang(first)
    return prefs


# Sat 21/11: every case waited on the Ledger for its own agenda and slot list (15-20+ s each),
# so turns ran long and other citizens' messages were "held while running". A case reuses an
# answer another case got moments ago: the agenda advanced at 60 agenda s per real s, the slot
# pages as they were (a slot taken since is refused at the hold, which re-checks the clock).
_SHARED_READS: dict[str, tuple[float, Message | None]] = {}
# Real seconds. Long on purpose: a fresh Ledger answer takes minutes to arrive. The agenda
# is advanced at 60x; a slot taken since is refused at the hold, which re-reads the clock.
_SHARED_AGENDA_FOR = 900.0
_SHARED_WORLD_FOR = 300.0


def _shared_read(command: str) -> str | None:
    """The text of a recent Ledger answer to ``command`` from any case, or None."""

    item = _SHARED_READS.get(command)
    if item is None or item[1] is None:
        return None
    age = time.monotonic() - item[0]
    if command == "agenda":
        if not 0 <= age <= _SHARED_AGENDA_FOR:
            return None
        then = _when(_family_fields(item[1]).get("agenda_now", ""))
        return None if then is None else f"agenda agenda_now={(then + timedelta(seconds=60 * age)).isoformat()}"
    if command.startswith("world") and 0 <= age <= _SHARED_WORLD_FOR:
        return item[1].text
    return None


async def _family_read(
    city: City, state: _FamilyCase, command: str, head: str, *, refresh: bool, shared: bool = True
) -> Message | None:
    text = _shared_read(command) if shared else None
    if text is not None:
        need = next((row.need_id for row in state.rows if row.need_id), None)
        row = Message(type="chat", sender=LEDGER, msg_id=f"shared-{len(state.rows)}-{first_word(command)}",
                      need_id=need, body={"text": text}, recipient=city.agent_id)
        state.rows.append(row)
        return row
    request = _family_request(state, city.agent_id, first_word(command), **fields_of(command))
    if request is not None and not refresh:
        reply = await _family_wait(city, state, request, head)
    else:
        request = await _family_send(city, state, LEDGER, command, repeat=True)
        reply = None if request is None else await _family_wait(city, state, request, head)
    if reply is not None and _family_head(reply) == head:
        _SHARED_READS[command] = (time.monotonic(), reply)
    return reply


async def _family_offers(
    city: City, state: _FamilyCase, citizen: str, message: Message, *, refresh: bool = False
) -> None:
    offers = [row for row in state.rows if row.sender == city.agent_id
              and row.recipient == citizen and _family_head(row) == "offer"]
    cancels = [i for i, row in enumerate(state.rows) if row.sender == city.agent_id and _family_head(row) == "cancel"]
    if offers and not refresh:
        last = state.rows.index(offers[-1])
        # Ignore unrelated Ledger pushes after an offer; do not send partial batches.
        if message.sender == LEDGER and (not cancels or last > cancels[-1]):
            return
        if message.sender == citizen and _family_head(message) in {"confirm", "consent"}:
            return
    refresh = refresh or bool(offers) or message.sender == citizen
    await _family_send(city, state, LEDGER, "rules", repeat=refresh)  # No prescription for direct access.
    # Ask agenda and the first slot page together: Ledger answers are deliveries (~5 min on
    # Sat 21/11), so asking one after the other cost two delivery rounds before any offer.
    first_page = f"world procedure=CONS-GIN facility={city.agent_id} page=1"
    for command in ("agenda", first_page):
        if _shared_read(command) is None:
            await _family_send(city, state, LEDGER, command, repeat=refresh)
    agenda = await _family_read(city, state, "agenda", "agenda", refresh=False)
    now = _when("" if agenda is None else _family_fields(agenda).get("agenda_now", ""))
    if now is None:
        await _family_note(city, state, citizen, "ledger_timeout")
        return
    lines: list[str] = []
    for page in range(1, _MAX_PAGE + 1):
        command = f"world procedure=CONS-GIN facility={city.agent_id} page={page}"
        world = await _family_read(city, state, command, "slots", refresh=refresh and page > 1)
        if world is None or _family_head(world) == "refused":
            await _family_note(city, state, citizen, "ledger_timeout" if world is None else _family_fields(world).get("code", "booking_refused"))
            return
        lines.extend(world.text.splitlines()[1:])
        current, pages = _page(world.text)
        if current != page:
            await _family_note(city, state, citizen, "ledger_timeout")
            return
        if current >= pages:
            break
    else:
        await _family_note(city, state, citizen, "search_incomplete")
        return
    # Paging can consume agenda time. A late clock reply newer than the world
    # already is a fresh snapshot; do not start a new query on every clock push.
    if state.rows.index(agenda) < state.rows.index(world):
        agenda = await _family_read(city, state, "agenda", "agenda", refresh=True)
        now = _when("" if agenda is None else _family_fields(agenda).get("agenda_now", ""))
        if now is None:
            await _family_note(city, state, citizen, "ledger_timeout")
            return
    prefs = _family_preferences(state.rows, citizen)
    days = _family_days(prefs.get("days", ""))
    # Registry attributes are optional. Unknown doctors are offered by exact id;
    # no language/gender is invented from an id suffix or a wildcard language.
    doctors: dict[str, dict[str, str]] = {}
    for row in state.rows:
        if row.sender == LEDGER:
            for line in row.text.splitlines():
                fields = fields_of(line)
                language = re.search(r"(?<!\S)languages=(\S+)", line)
                if language:
                    fields["languages"] = language.group(1).rstrip(".;!?")
                if fields.get("doctor_id") and "slot_id" not in fields:
                    doctors[fields["doctor_id"]] = fields
    dropped = _withdrawn(state.rows, city.agent_id)
    slots: dict[str, dict[str, str]] = {}
    for line in lines:
        slot = fields_of(line)
        sid, doc = slot.get("slot_id", ""), slot.get("doctor_id", "")
        at = _when(slot.get("at", slot.get("agenda_date", "")))
        if (not sid or not doc or at is None or at <= now + _LEAD or sid in dropped
            or slot.get("facility") != city.agent_id or slot.get("procedure") != "CONS-GIN"):
            continue
        free = slot.get("free", "1").lower()
        if (free == "false" or (free.lstrip("-").isdigit() and int(free) <= 0)
            or slot.get("urgent_reserve", "").lower() == "true"):
            continue
        day = _WEEKDAYS[at.weekday()]
        if day in {"sat", "sun"} or slot.get("day", slot.get("agenda_day", day)) != day:
            continue
        metadata = doctors.get(doc, {})
        languages = metadata.get("languages", slot.get("languages", ""))
        if languages and prefs.get("lang", "it") not in re.split(r"[/,|]", languages.lower()):
            continue
        slot.update(at=at.isoformat(), day=day)
        slots[sid] = slot
    gender = prefs.get("gender_preference", prefs.get("gender", ""))
    def rank(slot: dict[str, str]) -> tuple[bool, bool, str, str]:
        return (bool(days) and slot["day"] not in days,
                bool(gender) and doctors.get(slot["doctor_id"], {}).get("gender") != gender,
                slot["at"], slot["slot_id"])
    chosen = sorted(slots.values(), key=rank)
    # Cover different doctors/days before filling the message with near-identical slots.
    diverse: list[dict[str, str]] = []
    repeated: list[dict[str, str]] = []
    seen: set[tuple[str, str, bool]] = set()
    for slot in chosen:
        group = (slot["doctor_id"], slot["day"], bool(days) and slot["day"] not in days)
        (repeated if group in seen else diverse).append(slot)
        seen.add(group)
    chosen = sorted(diverse, key=rank) + sorted(repeated, key=rank)
    phrase = ("No prescription is needed. Choose one of these available appointments."
              if prefs.get("lang") == "en" else
              "Non serve ricetta. Scegli tra questi appuntamenti disponibili.")
    if chosen and days and not any(slot["day"] in days for slot in chosen):
        phrase = ("No slots match your preferred days; these alternatives are for you to consider."
                  if prefs.get("lang") == "en" else
                  "Non risultano slot nei giorni preferiti; puoi valutare queste alternative.")
    if not chosen:
        await _family_cross_search(city, state, citizen, message, now)
        return
    location = _family_location(city, english=prefs.get("lang") == "en")
    if location:
        phrase += " " + location
    batch: list[str] = []
    for slot in chosen:
        line = _offer_line(slot, None, None)
        if len("\n".join(batch + [line, phrase])) > 2000:
            break
        batch.append(line)
    if batch:
        await _family_send(city, state, citizen, "\n".join([*batch, phrase]))
    else:
        await _family_note(city, state, citizen, "offer_too_large")


# --- direct-access partner discovery ------------------------------------------------------------

_FAMILY_CAPABILITY = "family_counseling.gynecology"
_FAMILY_FIND_TIMEOUT = 3.0
_FAMILY_MAX_PARTNERS = 3


def _family_cross_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _family_cross_marker(state: _FamilyCase, agent: str, key: str) -> str:
    return next((_family_fields(row).get("value", "") for row in reversed(state.rows)
                 if row.sender == agent and row.recipient == LEDGER
                 and first_word(row.text) == "remember" and _family_fields(row).get("key") == key), "")


def _family_cross_requests(state: _FamilyCase, agent: str) -> dict[str, Message]:
    return {row.recipient: row for row in state.rows
            if row.sender == agent and row.recipient.startswith("agent:")
            and _family_head(row) == "need.request"
            and _family_fields(row).get("service_type") == _FAMILY_CAPABILITY
            and _family_fields(row).get("procedure") == "CONS-GIN"}


def _family_cross_catalog(state: _FamilyCase, agent: str) -> dict[str, dict[str, str]]:
    """Only public offers from a facility we actually queried in this need."""
    targets = _family_cross_requests(state, agent)
    offers: dict[str, dict[str, str]] = {}
    for row in state.rows:
        if row.sender not in targets or row.recipient not in ("", agent):
            continue
        if state.rows.index(row) <= state.rows.index(targets[row.sender]):
            continue
        # MACH-1.2: one message may hold a cancel line AND relaxed offer lines under it.
        # Read each line on its own: the first word of the whole text is not enough.
        lines = row.text.splitlines() or [row.text]
        for line in lines:
            if first_word(line) != "cancel":
                continue
            fields = fields_of(line)
            offers = {key: value for key, value in offers.items()
                      if not (value["facility"] == row.sender
                              and fields.get("offer_id", value["partner_offer_id"]) == value["partner_offer_id"]
                              and fields.get("slot_id", value["slot_id"]) == value["slot_id"])}
        rest = "\n".join(line for line in lines if first_word(line) != "cancel").strip()
        for fields in _offer_rows(rest):
            if (fields.get("request_id", _family_fields(targets[row.sender]).get("request_id"))
                != _family_fields(targets[row.sender]).get("request_id")):
                continue
            if (fields.get("facility") != row.sender or fields.get("procedure") != "CONS-GIN"
                or not fields.get("offer_id") or not fields.get("slot_id") or not fields.get("doctor_id")
                or fields.get("waiting_list", "false").lower() == "true"
                or fields.get("urgent_reserve", "false").lower() == "true"):
                continue
            at = _when(fields.get("agenda_date", fields.get("at", "")))
            if at is None or at.weekday() >= 5:
                continue
            day = _WEEKDAYS[at.weekday()]
            if fields.get("agenda_day", fields.get("day", day)) != day:
                continue
            free = fields.get("free", "1").lower()
            if free == "false" or (free.lstrip("-").isdigit() and int(free) <= 0):
                continue
            offer_id = "fx-" + _family_cross_hash(row.sender + "|" + fields["offer_id"] + "|" + fields["slot_id"])
            offers[offer_id] = {"offer_id": offer_id, "partner_offer_id": fields["offer_id"],
                                "slot_id": fields["slot_id"], "doctor_id": fields["doctor_id"],
                                "facility": row.sender, "procedure": "CONS-GIN", "at": at.isoformat(), "day": day,
                                "relaxed": fields.get("relaxed", "")}
    cancelled = {_family_fields(row).get("offer_id") for row in state.rows
                 if row.sender == agent and _family_head(row) == "cancel"}
    return {key: offer for key, offer in offers.items() if key not in cancelled}


def _family_cross_line(offer: dict[str, str]) -> str:
    return (f"offer offer_id={offer['offer_id']} slot_id={offer['slot_id']} "
            f"doctor_id={offer['doctor_id']} facility={offer['facility']} procedure=CONS-GIN "
            f"agenda_date={offer['at']} agenda_day={offer['day']}")


async def _family_cross_search(
    city: City, state: _FamilyCase, citizen: str, message: Message, now: datetime
) -> None:
    key = "family_discovery:" + _family_cross_hash(city.agent_id + "|" + (message.need_id or message.msg_id))
    if not _family_cross_marker(state, city.agent_id, key):
        # Persist the attempt before Index/Relay calls, so a restart does not repeat a 4xx.
        if await _family_send(city, state, LEDGER, f"remember key={key} value=started") is None:
            await _family_note(city, state, citizen, "index_unavailable")
            return
        reason = "no_partners"
        try:
            found = await asyncio.wait_for(city.find(_FAMILY_CAPABILITY), _FAMILY_FIND_TIMEOUT)
        except (CapError, TimeoutError) as err:
            _LOG.info("family.find.failed", extra={"code": getattr(err, "code", "timeout")})
            found, reason = [], "index_unavailable"
        peers = {peer.agent_id: peer for peer in found
                 if peer.agent_id != city.agent_id and _FAMILY_CAPABILITY in peer.capabilities}
        # TRUST RING: consultori on the city map before the impostors; a stranger only if none.
        peers = {key: peer for key, peer in peers.items() if key in _CITY_AGENTS} or peers
        # MACH-1.2 search criteria (days, doctor gender, a rare language): never citizen,
        # prescription, address or profile.
        wanted = _family_preferences(state.rows, citizen)
        criteria = "".join(
            f" {name}={value}" for name, value in (
                ("days", "/".join(sorted(_family_days(wanted.get("days", ""))))),
                ("gender_preference", wanted.get("gender_preference", wanted.get("gender", ""))[:1]),
                ("lang", wanted.get("lang", "") if wanted.get("lang") not in (None, "it", "en") else ""))
            if value and re.fullmatch(r"[a-z/]+", value))
        for target in sorted(peers)[:_FAMILY_MAX_PARTNERS]:
            query = (f"need.request service_type={_FAMILY_CAPABILITY} procedure=CONS-GIN "
                     f"request_id=rq-{key.split(':', 1)[1]}{criteria}")
            if await _family_send(city, state, target, query) is not None:
                reason = "partners_queried"
        await _family_send(city, state, LEDGER, f"remember key={key} value={reason}")
    requests = _family_cross_requests(state, city.agent_id)
    if not requests:
        reason = _family_cross_marker(state, city.agent_id, key)
        if reason not in {"no_partners", "partners_queried"}:
            await _family_note(city, state, citizen, "index_unavailable")
            return
    # A single bounded collection window; later replies never append another choice batch.
    answered: set[str] = set()
    for attempt in range(_PARTNER_TRIES):
        try:
            _family_merge(state, await city.history(need_id=message.need_id))
        except CapError:
            await _family_note(city, state, citizen, "history_unavailable")
            return
        answered = {row.sender for row in state.rows if row.sender in requests
                    and _family_head(row) in {"offer", "cancel"}}
        if answered == set(requests):
            break
        if attempt + 1 < _PARTNER_TRIES:
            await asyncio.sleep(_PULL_PAUSE)
    last_cancel = max((i for i, row in enumerate(state.rows) if row.sender == city.agent_id
                       and row.recipient == citizen and _family_head(row) == "cancel"), default=-1)
    if any(i > last_cancel and row.sender == city.agent_id and row.recipient == citizen
           and _family_head(row) == "offer" and any(o.get("offer_id", "").startswith("fx-")
                                                    for o in _offer_rows(row.text))
           for i, row in enumerate(state.rows)):
        return
    prefs = _family_preferences(state.rows, citizen)
    days = _family_days(prefs.get("days", ""))
    offers = [offer for offer in _family_cross_catalog(state, city.agent_id).values()
              if _when(offer["at"]) > now]
    offers.sort(key=lambda offer: (bool(offer.get("relaxed")), bool(days) and offer["day"] not in days,
                                   offer["at"], offer["offer_id"]))
    if offers:
        phrase = ("These appointments are at another facility. I will ask your permission before sharing booking details."
                  if prefs.get("lang") == "en" else
                  "Questi appuntamenti sono presso un'altra struttura. Prima di condividere i dati per prenotare ti chiederò il consenso.")
        batch: list[str] = []
        for offer in offers:
            line = _family_cross_line(offer)
            if len("\n".join([*batch, line, phrase])) > 2000:
                break
            batch.append(line)
        if any(offer.get("relaxed") for offer in offers[:len(batch)]):
            # Never pass relaxed= to the citizen: say in words what changes and ask.
            phrase += (" Some of them change one of your wishes (day, time or doctor): tell me if that is fine."
                       if prefs.get("lang") == "en" else
                       " Alcune cambiano una delle tue preferenze (giorno, orario o medico): dimmi se ti va bene.")
        if batch:
            await _family_send(city, state, citizen, "\n".join([*batch, phrase]))
        else:
            await _family_note(city, state, citizen, "offer_too_large")
        return
    if set(requests) - answered:
        await _family_note(city, state, citizen, "partner_pending")
        return
    phrase = ("I found no available appointments locally or among the contacted facilities. No booking has been made."
              if prefs.get("lang") == "en" else
              "Non ho trovato appuntamenti disponibili qui o tra le strutture contattate. Nessuna prenotazione è stata effettuata.")
    await _family_send(city, state, citizen, "offer available=false reason=no_matching_slots\n" + phrase)




async def _family_desk_told(city: City, state: _FamilyCase, committed: Message) -> bool:
    """Collaboration: our booking centre learns the slot is taken, so it never offers it again.

    The slot only: never the citizen, a name or a reason for the visit.
    """

    hold = _family_fields(committed).get("hold_id", "")
    slot = next((_family_fields(row).get("slot_id", "") for row in state.rows
                 if row.sender == LEDGER and _family_head(row) == "held"
                 and _family_fields(row).get("hold_id") == hold), "")
    if not slot or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", slot):
        return False
    note = (f"notice slot_booked procedure=CONS-GIN slot_id={slot}\n"
            "Dal consultorio: questo posto è appena stato prenotato da noi, non proponetelo ad altri.")
    return await _family_send(city, state, _OUR_BOOKING_CENTRE, note) is not None


def _family_receipt_extras(told: bool, english: bool) -> str:
    """Proactivity: one practical step nobody asked for; collaboration only when it happened."""

    if english:
        extra = " Bring your health card; no referral is needed at the consultorio."
        if told:
            extra += " I have also told our booking centre, so your slot is not offered to anyone else."
        return extra
    extra = " Porta la tessera sanitaria; al consultorio non serve la ricetta."
    if told:
        extra += " Ho già avvisato anche il centro prenotazioni, così il tuo posto non viene proposto ad altri."
    return extra
