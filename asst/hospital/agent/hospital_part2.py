"""Part of agent.py (loaded into its namespace by _load_parts). Do not import directly."""

from __future__ import annotations

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
    if message.sender.startswith("citizen:") and _facility_red_flag(message):
        await _escalate_here(city, message, chat)
        return
    if message.sender.startswith("citizen:") and _impostor_checkin(message, chat, city.agent_id):
        await _refuse_impostor(city, message, chat)
        return
    if message.sender == LEDGER and head == "held":
        await _on_held(city, message, chat)
    elif message.sender == LEDGER and head == "committed":
        await _on_committed(city, message, chat)
    elif message.sender == LEDGER and head == "refused" and bool({"hold", "commit", "op=hold", "op=commit"} & kind):
        await _on_refused(city, message, chat, hold_self=hold_self)
    elif message.sender == LEDGER and head == "checked_in":
        await _on_checked_in(city, message, chat, reports=reports)
    elif message.sender == LEDGER and head == "reported":
        await _on_reported(city, message, chat)
    elif _checkin_booking(message, chat, city.agent_id):
        await _on_checkin(
            city,
            message,
            chat,
            reports=reports,
            booking_id=_checkin_booking(message, chat, city.agent_id),
        )
    elif message.sender.startswith("agent:") and first_word(message.text) == "need.request":
        await _with_ack(city, message, _answer_partner(city, message, chat), _PARTNER_ACK)
    elif "confirm" in kind:
        work = _on_confirm(city, message, chat, hold_self=hold_self)
        if message.sender.startswith("agent:"):
            await _with_ack(city, message, work, _CONFIRM_ACK)
        else:
            await work


# --- partner ASSTs (cross-ASST) -----------------------------------------------------------------
# Another ASST's booking centre asks for slots with search criteria only. We answer with ONE
# offer message of our own free slots; its later ``confirm`` goes through _on_confirm (hold,
# commit with its consent_ref, receipt back to it). Same block in hospital and specialist.
_PARTNER_MAX = 6
# Scarcity (level 4): page 1 of the world may hold only full or late slots. Read a few more
# pages, but stay inside the fast-reply window.
_PARTNER_PAGES = 3
_PARTNER_BUDGET = 6.0  # seconds spent waiting for extra pages
# Our ASST's rare language (``*`` in _DOCTORS), from the Mondo ASST page: asst-greco-h3.
_RARE_LANG = "si"


def _partner_slots(
    lines: list[str], *, now: datetime, priority: str | None, facility: str, procedure: str
) -> list[dict[str, str]]:
    """Our free slots inside the priority window, spread over doctors and days, at most six."""

    if priority == "U":
        horizon: datetime | None = now.replace(hour=23, minute=59, second=59, microsecond=0)
    elif priority == "P":
        horizon = None
    else:
        horizon = now + timedelta(days=_DEADLINE_DAYS.get(priority or "", 14))
    earliest = now + _LEAD
    timed: list[tuple[datetime, dict[str, str]]] = []
    for line in lines:
        slot = fields_of(line)
        at = _when(slot.get("at", ""))
        if not slot.get("slot_id") or not slot.get("doctor_id") or at is None:
            continue
        if slot.get("facility") != facility or slot.get("procedure") not in (None, procedure):
            continue
        free = slot.get("free")
        if free is not None and free.isdigit() and int(free) <= 0:
            continue
        if slot.get("urgent_reserve", "").lower() == "true" and priority != "U":
            continue
        if not earliest < at or (horizon is not None and at >= horizon):
            continue
        timed.append((at, slot))
    timed.sort(key=lambda item: (item[0], item[1]["slot_id"]))
    first: list[dict[str, str]] = []
    rest: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for _, slot in timed:
        pair = (slot["doctor_id"], slot.get("day", ""))
        (rest if pair in seen else first).append(slot)
        seen.add(pair)
    return (first + rest)[:_PARTNER_MAX]


def _partner_pages(chat: list[Message], procedure: str) -> dict[int, str]:
    """The Ledger's ``slots`` pages for ``procedure`` in this case, by page number."""

    found: dict[int, str] = {}
    for row in chat:
        if row.sender != LEDGER or first_word(row.text) != "slots":
            continue
        head = fields_of(row.text.splitlines()[0])
        if head.get("procedure", procedure) != procedure:
            continue
        page, _ = _page(row.text)
        found[page] = row.text
    return found


# Ledger reads take 15-20 s on the live city. Another agent hears a plain "received" after
# _ACK_AFTER seconds (fast reply, MACH-1) while the work goes on; the text has no act word
# and nothing a booking centre reads as "no availability".
_ACK_AFTER = 4.0
_PARTNER_ACK = "Ricevuto, verifico la disponibilità e vi rispondo in un solo messaggio."
_CONFIRM_ACK = "Ricevuto, blocco il posto sul Ledger e vi mando la ricevuta appena è pronta."
_PARTNER_WAIT = 30.0  # seconds a partner request may wait for the Ledger, in all


async def _with_ack(city: City, message: Message, work, ack: str) -> None:
    turn = asyncio.ensure_future(work)
    done, _pending = await asyncio.wait({turn}, timeout=_ACK_AFTER)
    if not done:
        await _say(city, message.sender, ack)
    await turn


async def _until_long(
    city: City,
    need_id: str | None,
    chat: list[Message],
    ready: Callable[[list[Message]], bool],
    seconds: float,
) -> list[Message]:
    """Like _until, but waits up to ``seconds`` (the Ledger can be slow)."""

    loop = asyncio.get_running_loop()
    stop = loop.time() + max(0.0, seconds)
    fresh = chat
    while True:
        try:
            fresh = await city.history(need_id=need_id)
        except CapError:
            fresh = chat
        if ready(fresh) or loop.time() >= stop:
            return fresh
        await asyncio.sleep(max(_PULL_PAUSE, 0.05))


# procedure -> the world page that held "now" at the last partner request (this process only).
_PARTNER_HINT: dict[str, int] = {}


def _page_span(text: str) -> tuple[datetime, datetime] | None:
    times = [
        at
        for at in (_when(fields_of(line).get("at", "")) for line in text.splitlines() if "slot_id=" in line)
        if at is not None
    ]
    return (min(times), max(times)) if times else None


def _partner_now_page(got: dict[int, str], now: datetime) -> int | None:
    """The first page whose slots reach "now", once the pages read pin it down."""

    info = {page: span for page, text in got.items() if (span := _page_span(text))}
    cover = [page for page, (first, last) in info.items() if first < now <= last]
    if cover:
        return min(cover)
    before = [page for page, (first, last) in info.items() if last < now]
    after = [page for page, (first, last) in info.items() if first >= now]
    if after:
        low = min(after)
        if low == 1 or (before and max(before) == low - 1):
            return low
    return None


def _partner_first_future(got: dict[int, str], now: datetime) -> int | None:
    """Best guess for next time: the lowest page read whose slots reach "now" or later."""

    reach = [page for page, text in got.items() if (span := _page_span(text)) and span[1] >= now]
    return min(reach) if reach else None


def _partner_next_page(got: dict[int, str], now: datetime, pages: int, tried: set[int]) -> int | None:
    """Next page to read. The catalogue is sorted by date: once the page that reaches "now" is
    known, read forward from it; before that, interpolate between a page in the past and one in
    the future, or jump from a past page at about one agenda day per page."""

    start = _partner_now_page(got, now)
    if start is not None:
        for page in range(start + 1, pages + 1):
            if page not in tried:
                return page
        return None
    info = {page: span for page, text in got.items() if (span := _page_span(text))}
    before = [page for page, (first, last) in info.items() if last < now]
    after = [page for page, (first, last) in info.items() if first >= now]
    low = max(before) if before else 0
    high = min(after) if after else pages + 1
    if high - low <= 1:
        return None
    if before and after:
        gap = (info[high][0] - info[low][1]).total_seconds()
        share = (now - info[low][1]).total_seconds() / gap if gap > 0 else 0.5
        target = low + max(1, round(share * (high - low)))
    elif before:
        first, last = info[low]
        per_page = max((last - first).total_seconds(), 86400.0)
        target = low + max(1, int((now - last).total_seconds() // per_page) + 1)
    else:
        first, last = info[high]
        per_page = max((last - first).total_seconds(), 86400.0)
        target = high - max(1, int((first - now).total_seconds() // per_page) + 1)
    target = min(max(target, low + 1), high - 1)
    for step in range(high - low):
        for page in (target + step, target - step):
            if low < page < high and page not in tried:
                return page
    return None


def _partner_lines(chat: list[Message], procedure: str) -> list[str]:
    return [
        line
        for _, text in sorted(_partner_pages(chat, procedure).items())
        for line in text.splitlines()
        if "slot_id=" in line
    ]


async def _answer_partner(city: City, message: Message, chat: list[Message]) -> None:
    """One offer message (or a cancel) for a partner booking centre's ``need.request``."""

    agent = city.agent_id
    fields = fields_of(message.text)
    procedure = fields.get("procedure", "")
    request_id = fields.get("request_id", "")
    priority = fields.get("priority")
    partner = message.sender
    if not procedure or not request_id:
        return
    token = f"request_id={request_id}"
    if any(
        row.sender == agent and row.recipient == partner and token in row.text.split()
        for row in chat
    ):
        return
    if _latest_slots(chat) is None and not any(
        row.sender == agent and row.recipient == LEDGER and first_word(row.text) == "world"
        for row in chat
    ):
        # Start from the page that held "now" last time (the catalogue begins on day one of
        # the event and is sorted by date): page 1 is only the first guess.
        await _say(city, LEDGER, f"world procedure={procedure} page={_PARTNER_HINT.get(procedure, 1)}")
    if _ledger(chat, "agenda") is None and not _exact(chat, agent, "agenda"):
        await _say(city, LEDGER, "agenda")
    loop = asyncio.get_running_loop()
    stop = loop.time() + _PARTNER_WAIT
    chat = await _until_long(
        city,
        message.need_id,
        chat,
        lambda rows: _latest_slots(rows) is not None and _ledger(rows, "agenda") is not None,
        _PARTNER_WAIT,
    )
    world = _latest_slots(chat)
    agenda = _ledger(chat, "agenda")
    now = _when("" if agenda is None else agenda.get("agenda_now", ""))
    if world is None or now is None:
        return
    slots = _partner_slots(
        _partner_lines(chat, procedure) or [line for line in world.splitlines() if "slot_id=" in line],
        now=now,
        priority=priority,
        facility=agent,
        procedure=procedure,
    )
    _, pages = _page(world)
    tried = set(_partner_pages(chat, procedure))
    while len(slots) < _PARTNER_MAX and len(tried) < _PARTNER_PAGES and loop.time() < stop:
        page = _partner_next_page(_partner_pages(chat, procedure), now, pages, tried)
        if page is None:
            break
        tried.add(page)
        if not _asked(chat, agent, procedure, page):
            await _say(city, LEDGER, f"world procedure={procedure} page={page}")
        chat = await _until_long(
            city,
            message.need_id,
            chat,
            lambda rows, wanted=page: wanted in _partner_pages(rows, procedure),
            stop - loop.time(),
        )
        if page not in _partner_pages(chat, procedure):
            break
        slots = _partner_slots(
            _partner_lines(chat, procedure),
            now=now,
            priority=priority,
            facility=agent,
            procedure=procedure,
        )
    found = _partner_now_page(_partner_pages(chat, procedure), now) or _partner_first_future(
        _partner_pages(chat, procedure), now
    )
    if found is not None:
        _PARTNER_HINT[procedure] = found
    if not slots:
        await _say(city, partner, f"cancel {token} reason=no_availability")
        return
    rows: list[str] = []
    for slot in slots:
        parts = [
            "offer",
            f"offer_id=p-{slot['slot_id']}",
            f"slot_id={slot['slot_id']}",
            f"doctor_id={slot['doctor_id']}",
            f"facility={agent}",
            f"procedure={procedure}",
            f"agenda_date={slot['at']}",
            f"agenda_day={slot.get('day', '')}",
            f"expiry={slot['at']}",
        ]
        spec = _DOCTORS.get(slot["doctor_id"].rsplit("-", 1)[-1])
        if spec is not None:
            langs = sorted(_RARE_LANG if lang == "*" else lang for lang in spec[0])
            parts += [f"doctor_lang={'/'.join(langs)}", f"doctor_gender={spec[1]}"]
        if priority:
            parts.append(f"priority={priority}")
        parts.append(token)
        line = " ".join(parts)
        if len("\n".join([*rows, line])) > 2000:
            break
        rows.append(line)
    await _say(city, partner, "\n".join(rows))


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
) -> str:
    """Hold is in the mailbox: "committed", "pending" (the Ledger answer comes later as a
    delivery) or "failed" (refused). Read the held line and commit before this turn ends."""

    clock = asyncio.get_running_loop().time
    started = clock()
    found = await _await_line(city, message.need_id, chat, "held", f"slot_id={slot}", op="hold",
                              tries=_BOOK_TRIES)
    if found is None:
        _LOG.warning("care.hold.result need=%s slot=%s outcome=missing secs=%.0f",
                     (message.need_id or "")[-12:], slot[-16:], clock() - started)
        return "pending"
    reply, fresh = found
    _LOG.warning("care.hold.result need=%s slot=%s outcome=%s secs=%.0f code=%s",
                 (message.need_id or "")[-12:], slot[-16:], first_word(reply.text), clock() - started,
                 (fields_of(reply.text).get("code") or fields_of(reply.text).get("reason") or "-")[:40])
    if first_word(reply.text) == "refused":
        await _on_refused(city, reply, fresh, hold_self=hold_self)
        return "failed"
    await _on_held(city, reply, fresh)
    hold_id = fields_of(reply.text).get("hold_id", "")
    if not hold_id:
        return "failed"
    started = clock()
    done = await _await_line(
        city, message.need_id, fresh, "committed", f"booking_id={hold_id}", op="commit",
        tries=_BOOK_TRIES,
    )
    if done is None:
        _LOG.warning("care.commit.result need=%s hold=%s outcome=missing secs=%.0f",
                     (message.need_id or "")[-12:], hold_id[-12:], clock() - started)
        return "pending"
    reply, fresh = done
    _LOG.warning("care.commit.result need=%s hold=%s outcome=%s secs=%.0f code=%s",
                 (message.need_id or "")[-12:], hold_id[-12:], first_word(reply.text), clock() - started,
                 (fields_of(reply.text).get("code") or fields_of(reply.text).get("reason") or "-")[:40])
    if first_word(reply.text) == "committed":
        await _on_committed(city, reply, fresh)
        return "committed"
    if first_word(reply.text) == "refused":
        await _on_refused(city, reply, fresh, hold_self=hold_self)
        return "failed"
    return "pending"


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
    fields = fields_of(message.text)
    reason = fields.get("reason") or fields.get("code") or "facility_cancelled"
    _LOG.warning("care.refused need=%s reason=%s text=%s", (message.need_id or "")[-12:], reason[:40],
                 " ".join(message.text.split())[:160])
    slot = ""
    for row in chat:
        if row.sender == agent and row.recipient == LEDGER and "hold" in words(row.text):
            slot = fields_of(row.text).get("slot_id", slot)
    if not slot:
        return
    claim = _SLOT_OWNER.get(slot)
    if claim is not None and claim[0] == (message.need_id or ""):
        del _SLOT_OWNER[slot]  # the Ledger refused this citizen's hold: the slot is free again
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
        await _say(city, target, f"cancel offer_id={offer_id} reason={reason}")
    if hold_self:
        await _advance(city, message, chat, fixed="CONS-GIN", replacing=True)


# A citizen may check in with plain words: "Check-in:", "check in", "sono arrivata", or just
# the booking number. Answers to a consent question are never a check-in.
_CHECKIN_WORDS = re.compile(
    r"\bcheck[\s_-]?in\b|\bsono\s+(?:arrivat[oa]|qui)\b|\bmi\s+presento\b|\barrived\b|\bi'?m\s+here\b",
    re.IGNORECASE,
)


def _our_bookings(chat: list[Message], agent: str) -> set[str]:
    """booking_id values the Ledger committed for this facility in this case."""

    found: set[str] = set()
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == "committed":
            booking = fields_of(row.text).get("booking_id", "")
            if booking:
                found.add(booking)
    return found


def _checkin_booking(message: Message, chat: list[Message], agent: str) -> str:
    """The booking a citizen's turn checks in for, or "" when the turn is not a check-in."""

    if not message.sender.startswith("citizen:"):
        return ""
    text = message.text or ""
    named = words(text)
    if "consent" in named:
        return ""
    explicit = "checkin" in named or _CHECKIN_WORDS.search(text) is not None
    ours = _our_bookings(chat, agent)
    booking = fields_of(text).get("booking_id", "")
    if not booking:
        for token in text.replace(",", " ").split():
            bare = token.strip(".,;:!?()\"'")
            if bare in ours:
                booking = bare
                break
    if not booking:
        return ""
    if explicit:
        return booking
    if booking in ours and not _we(chat, agent, f"booking_id={booking}", to=LEDGER, word="checkin"):
        return booking
    return ""


async def _on_checkin(
    city: City, message: Message, chat: list[Message], *, reports: bool, booking_id: str = ""
) -> None:
    booking_id = booking_id or fields_of(message.text).get("booking_id", "")
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
        await _say(city, citizen, f"checkin booking_id={booking_id} {_checkin_words(city, chat, citizen)}")
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


# --- words for the citizen ---------------------------------------------------------------------
# The act and its key=value tokens come first; the sentence after them is for the person.
# No clinical advice. The sentence avoids the words "consent" and "report": the code reads them
# back from sent messages to know what was already done.
_EN_HINTS = re.compile(
    r"\b(hello|hi|good morning|i am|i'm|my|the|please|thanks?|appointment|doctor|here|arrived)\b",
    re.IGNORECASE,
)
_IT_HINTS = re.compile(
    r"\b(buongiorno|buonasera|salve|sono|grazie|il|la|mio|mia|per|della|visita|prenotazione)\b",
    re.IGNORECASE,
)


def _citizen_lang(chat: list[Message], citizen: str) -> str:
    """'en' when the citizen writes (or asked) in English, else 'it'."""

    en = it = 0
    for row in chat:
        if row.sender != citizen:
            continue
        lang = fields_of(row.text).get("lang", "").lower()
        if lang:
            return "en" if lang.startswith("en") else "it"
        en += len(_EN_HINTS.findall(row.text))
        it += len(_IT_HINTS.findall(row.text))
    return "en" if en > it else "it"


def _checkin_words(city: City, chat: list[Message], citizen: str) -> str:
    street = _own_street(city)
    if _citizen_lang(chat, citizen) == "en":
        where = f" at {street}" if street else ""
        return (
            f"Welcome to the physiatry clinic of ASST Greco hospital{where}: your arrival is "
            "registered. Please take a seat in the waiting room, we will call you when the doctor "
            "is ready. After the visit the doctor will tell you the next steps. If you need help "
            "with access, just tell the staff."
        )
    where = f" in {street}" if street else ""
    return (
        f"Benvenuta o benvenuto all'ambulatorio di fisiatria dell'ospedale dell'ASST Greco{where}: "
        "il suo arrivo è registrato. Si accomodi in sala d'attesa, la chiamiamo appena il medico è "
        "pronto. Dopo la visita il medico le spiega i passi successivi. Se ha bisogno di "
        "assistenza per l'accesso, lo dica pure al personale."
    )


# --- level 8: red flags and impostors at the facility ------------------------------------------
# A red flag the citizen tells the facility (chest pain, legs that give way, stroke...) is
# escalated at once: no visit talk, no advice. Same patterns as the booking centre.
_FAC_CHEST = r"\b(petto|torace|sterno|chest)\b"
_FAC_CHEST_BAD = (
    r"\b(dolor|male?\b|peso\b|pesante|oppress|opprim|stringe|stretta|schiacci|brucia|"
    r"fitt[ae]\b|morsa|costri|pain|hurt|ach(e|es|ing)\b|tight|pressure|crush|heavy|squeez)"
)
_FAC_LEGS = r"\b(gamb[ae]|legs?)\b"
_FAC_LEGS_BAD = (
    r"\b(forza|debol|non (le )?sento|non riesco a muover|non si muov|cedono|paralizz|"
    r"intorpidit|insensibil|strength|weak|numb|can'?t (feel|move)|cannot (feel|move)|paraly)"
)
_FAC_RED_FLAG_RE = tuple(
    re.compile(rf"{a}.{{0,40}}{b}|{b}.{{0,40}}{a}", re.DOTALL)
    for a, b in ((_FAC_CHEST, _FAC_CHEST_BAD), (_FAC_LEGS, _FAC_LEGS_BAD))
) + (
    re.compile(r"\binfarto\b|heart attack|\bictus\b|\bstroke\b|non riesco a respirare|can'?t breathe"),
)


def _facility_red_flag(message: Message) -> bool:
    if understood(message).body.get("is_emergency") is True:
        return True
    lowered = (message.text or "").lower()
    return any(flag in lowered for flag in _RED_FLAGS) or any(
        pattern.search(lowered) for pattern in _FAC_RED_FLAG_RE
    )


async def _escalate_here(city: City, message: Message, chat: list[Message]) -> None:
    citizen = message.sender
    if _we(chat, city.agent_id, "escalate.emergency", to=citizen, word="escalate.emergency"):
        return
    if _citizen_lang(chat, citizen) == "en":
        words_ = "This is an emergency: call 112 now. Please do not wait for the visit."
    else:
        words_ = "Questa è un'emergenza: chiami subito il 112. Non aspetti la visita."
    await _say(city, citizen, f"escalate.emergency reason=red_flag {words_}")


def _booking_owner(chat: list[Message], agent: str) -> str:
    """The citizen named in the confirm or hold of this case, if any."""

    for row in chat:
        if row.sender == LEDGER:
            continue
        if "confirm" in words(row.text) or (row.sender == agent and first_word(row.text) == "hold"):
            owner = fields_of(row.text).get("citizen", "")
            if owner.startswith("citizen:"):
                return owner
    return ""


def _impostor_checkin(message: Message, chat: list[Message], agent: str) -> bool:
    """A check-in for a booking that this case books for another citizen."""

    if not _checkin_booking(message, chat, agent):
        return False
    owner = _booking_owner(chat, agent)
    return bool(owner) and owner != message.sender


async def _refuse_impostor(city: City, message: Message, chat: list[Message]) -> None:
    citizen = message.sender
    if any(row.sender == city.agent_id and row.recipient == citizen and "sportello" in row.text for row in chat):
        return
    if _citizen_lang(chat, citizen) == "en":
        words_ = (
            "I cannot register this arrival: the booking is not in your name. Please come to "
            "the front desk with your own booking receipt."
        )
    else:
        words_ = (
            "Non posso registrare questo arrivo: la prenotazione non risulta a suo nome. Si "
            "rivolga allo sportello con la sua ricevuta di prenotazione."
        )
    await _say(city, citizen, words_)
