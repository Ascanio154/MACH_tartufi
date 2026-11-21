"""Part of agent.py (loaded into its namespace by _load_parts). Do not import directly."""

from __future__ import annotations

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
    chat = await _until(
        city,
        message.need_id,
        chat,
        lambda rows: _latest_slots(rows) is not None and _ledger(rows, "agenda") is not None,
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
    loop = asyncio.get_running_loop()
    stop = loop.time() + _PARTNER_BUDGET
    tried = set(_partner_pages(chat, procedure))
    while len(slots) < _PARTNER_MAX and len(tried) < _PARTNER_PAGES and loop.time() < stop:
        page = _partner_next_page(_partner_pages(chat, procedure), now, pages, tried)
        if page is None:
            break
        tried.add(page)
        if not _asked(chat, agent, procedure, page):
            await _say(city, LEDGER, f"world procedure={procedure} page={page}")
        chat = await _until(
            city,
            message.need_id,
            chat,
            lambda rows, wanted=page: wanted in _partner_pages(rows, procedure),
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
    fields = fields_of(message.text)
    reason = fields.get("reason") or fields.get("code") or "facility_cancelled"
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
    if citizen and not _we(chat, city.agent_id, token, to=citizen, word="checkin"):
        await _say(city, citizen, f"checkin booking_id={booking_id} {_checkin_words(city, chat, citizen)}")
    if not reports:
        return
    if _we(chat, city.agent_id, token, to=LEDGER, word="report"):
        return
    consent_ref = ""
    if citizen:
        # Ask consent to share the report, wait briefly for the answer, then file the report
        # anyway: silence counts as a no (filed unshared), like a plain-words refusal.
        if not _we(chat, city.agent_id, token, to=citizen, word="consent"):
            await _say(
                city,
                citizen,
                f"consent purpose=report_sharing {token} {_consent_words(chat, citizen)}",
            )
        chat = await _until(
            city,
            message.need_id,
            chat,
            lambda rows: _consent_answer(rows, city.agent_id, citizen) is not None,
        )
        answer = _consent_answer(chat, city.agent_id, citizen)
        if answer is not None:
            consent_ref = _report_consent_ref(answer, chat, city.agent_id)
    await _file_report(city, message, chat, booking_id, consent_ref=consent_ref)


def _consent_answer(chat: list[Message], agent: str, citizen: str) -> Message | None:
    """The citizen's first message after our latest report_sharing question, if any."""

    asked = -1
    for index, row in enumerate(chat):
        if (
            row.sender == agent
            and row.recipient == citizen
            and first_word(row.text) == "consent"
            and "purpose=report_sharing" in row.text.split()
        ):
            asked = index
    if asked < 0:
        return None
    for row in chat[asked + 1 :]:
        if row.sender == citizen:
            return row
    return None


def _report_consent_ref(answer: Message, chat: list[Message], agent: str) -> str:
    """The consent_ref in the citizen's answer, or "" for a refusal (any answer without one)."""

    fields = fields_of(answer.text)
    ref = fields.get("consent_ref", "")
    if fields.get("purpose", "report_sharing") != "report_sharing":
        return ""
    if ref in _booking_consents(chat, agent):
        return ""
    return ref


# Bookings whose report this process has started to write.
_FILING: set[str] = set()


def _report_pending(chat: list[Message], agent: str, citizen: str) -> str:
    """Booking whose report-consent question we asked this citizen and whose report is not filed."""

    booking_id = ""
    for row in chat:
        if row.sender != agent or row.recipient != citizen:
            continue
        if first_word(row.text) == "consent" and "purpose=report_sharing" in row.text.split():
            booking_id = fields_of(row.text).get("booking_id", booking_id)
    if not booking_id or _we(chat, agent, f"booking_id={booking_id}", to=LEDGER, word="report"):
        return ""
    return booking_id


def _booking_consents(chat: list[Message], agent: str) -> set[str]:
    """consent_ref values already used for the booking (hold/commit), not for the report."""

    used: set[str] = set()
    for row in chat:
        if row.recipient == LEDGER and row.sender == agent:
            ref = fields_of(row.text).get("consent_ref", "")
            if ref:
                used.add(ref)
        if "confirm" in words(row.text):
            ref = fields_of(row.text).get("consent_ref", "")
            if ref:
                used.add(ref)
    return used


async def _on_report_consent(city: City, message: Message, chat: list[Message]) -> None:
    """The citizen answered the report_sharing question. Any answer without a new consent_ref
    is a refusal: file the report anyway, unshared."""

    booking_id = _report_pending(chat, city.agent_id, message.sender)
    if not booking_id:
        return
    ref = _report_consent_ref(message, chat, city.agent_id)
    await _file_report(city, message, chat, booking_id, consent_ref=ref)


async def _file_report(
    city: City, message: Message, chat: list[Message], booking_id: str, *, consent_ref: str
) -> None:
    """Write the report on the Ledger first, then tell the citizen."""

    token = f"booking_id={booking_id}"
    if not _we(chat, city.agent_id, token, to=LEDGER, word="report"):
        # Two turns of one case can run at once (the check-in turn waiting for consent and
        # the consent answer's own turn): only the first one writes the report.
        if booking_id in _FILING:
            return
        _FILING.add(booking_id)
        parts = ["report", token, "outcome_code=VISIT"]
        if consent_ref:
            parts.append(f"consent_ref={consent_ref}")
        follow_up = await _follow_up(city, message, chat, booking_id)
        if follow_up:
            parts.append(f"follow_up_prescription_number={follow_up}")
        if not await _say(city, LEDGER, " ".join(parts)):
            return
    found = await _await_line(city, message.need_id, chat, "reported", token, op="report")
    if found is None:
        _LOG.info("care.report.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = found
    if first_word(reply.text) == "reported":
        await _on_reported(city, reply, fresh)
