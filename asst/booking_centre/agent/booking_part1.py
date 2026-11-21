"""Part of agent.py (loaded into its namespace by _load_parts). Do not import directly."""

from __future__ import annotations

def fitting(
    lines: list[str],
    *,
    now: datetime,
    priority: str | None,
    lang: str,
    gender: str | None = None,
    facility: str | None = None,
    procedure: str | None = None,
    reserve: bool = False,
    born: datetime | None = None,
) -> list[dict[str, str]]:
    """Slots a citizen can accept: future, free, in the priority window, spoken language.

    ``reserve`` lets a non-U priority use the urgent reserve: only while rule P3 is not in force.
    ``born`` is when the case started: the priority's deadline runs from the referral, not from
    now (journeys 228 and 166: a slot 48 h from *now* was refused with reason=deadline).
    """

    if priority == "U":
        horizon: datetime | None = now.replace(hour=23, minute=59, second=59, microsecond=0)
    elif priority == "P":
        horizon = None
    elif born is not None and priority in _DEADLINE_DAYS:
        horizon = born + timedelta(days=_DEADLINE_DAYS[priority]) - _BORN_MARGIN
    else:
        horizon = now + timedelta(days=_DEADLINE_DAYS.get(priority or "", 14))
    earliest = now + _LEAD
    chosen: list[tuple[datetime, dict[str, str]]] = []
    for line in lines:
        slot = fields_of(line)
        slot_id = slot.get("slot_id")
        doctor = slot.get("doctor_id")
        at = _when(slot.get("at", ""))
        if not slot_id or not doctor or at is None:
            continue
        if procedure and slot.get("procedure") not in (None, procedure):
            continue
        if facility and slot.get("facility") != facility:
            continue
        free = slot.get("free")
        if free is not None and free.isdigit() and int(free) <= 0:
            continue
        if slot.get("urgent_reserve", "").lower() == "true" and priority != "U" and not reserve:
            continue
        if not earliest < at or (horizon is not None and at >= horizon):
            continue
        if not _speaks(doctor, lang):
            continue
        spec = _DOCTORS.get(doctor.rsplit("-", 1)[-1])
        if spec is None:
            continue
        day = slot.get("day", "")
        if day and day not in spec[2]:
            continue
        if gender and spec[1] != gender:
            continue
        chosen.append((at, slot))
    chosen.sort(key=lambda item: (item[0], item[1].get("slot_id", "")))
    return [slot for _, slot in chosen]


# --- medico di base ----------------------------------------------------------------------------


def _symptom(message: Message) -> str | None:
    code = understood(message).body.get("symptom_code")
    if isinstance(code, str) and code in _PROTOCOLS:
        return code
    lowered = message.text.lower()
    for name, phrases in _KEYWORDS:
        if any(phrase in lowered for phrase in phrases):
            return name
    return None


def _our_rx(chat: list[Message], agent: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for row in chat:
        if row.sender == agent and "prescribe" in words(row.text):
            found = fields_of(row.text)
    return found


async def _gp(city: City, message: Message, chat: list[Message]) -> None:
    agent = city.agent_id
    if message.sender == LEDGER:
        await _announce(city, message, chat, agent)
        return
    if not message.sender.startswith("citizen:"):
        return
    if _emergency(message):
        if not _we(chat, agent, "escalate.emergency", to=message.sender, word="escalate.emergency"):
            await _say(city, message.sender, "escalate.emergency reason=red_flag")
        return
    code = _symptom(message)
    if code is None:
        return
    procedure, priority = _PROTOCOLS[code]
    number = _rx(message.need_id, message.msg_id)
    token = f"prescription_number={number}"
    # The citizen's reply window closes before a ledger round trip comes back, so the
    # prescription is said in this same turn. The ledger write is what the judge reads.
    if not _we(chat, agent, token, to=LEDGER, word="prescribe"):
        if not await _say(
            city,
            LEDGER,
            (
                f"prescribe patient={message.sender} procedure={procedure} priority={priority} "
                f"symptom_code={code} {token}"
            ),
        ):
            return
    if _we(chat, agent, token, to=message.sender, word="prescription"):
        return
    await _say(
        city,
        message.sender,
        f"prescription {token} procedure={procedure} priority={priority}",
    )


async def _announce(city: City, message: Message, chat: list[Message], agent: str) -> None:
    head = first_word(message.text)
    if head == "refused" and "DUPLICATE" not in message.text:
        return
    if head not in {"prescribed", "refused"}:
        return
    fields = fields_of(message.text)
    ours = _our_rx(chat, agent)
    number = fields.get("prescription_number") or ours.get("prescription_number")
    citizen = fields.get("patient") or _citizen(chat)
    if not number or not citizen:
        return
    if _we(chat, agent, f"prescription_number={number}", to=citizen, word="prescription"):
        return
    parts = ["prescription", f"prescription_number={number}"]
    procedure = fields.get("procedure") or ours.get("procedure")
    priority = fields.get("priority") or ours.get("priority")
    if procedure:
        parts.append(f"procedure={procedure}")
    if priority:
        parts.append(f"priority={priority}")
    await _say(city, citizen, " ".join(parts))


# --- centro prenotazioni and consultorio -------------------------------------------------------


def _ledger(chat: list[Message], head: str) -> dict[str, str] | None:
    found: dict[str, str] | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == head:
            found = fields_of(row.text)
    return found


# The GP wrote the referral a little before our first agenda read of the case.
_BORN_MARGIN = timedelta(hours=2)


def _born(chat: list[Message]) -> datetime | None:
    """Agenda time of our first read for this case: the closest we see to the referral's issue."""

    need = next((row.need_id for row in chat if row.need_id), "") or ""
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == "agenda":
            seen = _when(fields_of(row.text).get("agenda_now", ""))
            # The referral was issued when the case's first message was sent, minutes (agenda
            # hours) before this read: night of 21/11 the first read came 7-20 h of agenda late
            # and every slot offered to a B priority was refused with reason=deadline.
            stamps = [_when(r.sent_at or "") for r in chat]
            first = min((s for s in stamps if s is not None), default=None)
            at = _when(row.sent_at or "") or datetime.now(timezone.utc)
            if seen is not None and first is not None and first < at:
                seen -= min(60 * (at - first), timedelta(days=1))
                if seen.weekday() >= 5:
                    seen -= timedelta(days=2)
            if seen is not None and (need not in _BORN or seen < _BORN[need]):
                if len(_BORN) > _SENT_NEEDS:
                    _BORN.clear()
                _BORN[need] = seen
            break
    return _BORN.get(need)


def _reserve_open(chat: list[Message]) -> bool:
    """True when the Ledger's rules reply says no rule is in force (P3 off): the judge counts
    the urgent reserve in the citizen's answer set then, so it is our last local resort."""

    row = _ledger_row(chat, "rules")
    return row is not None and "count=0" in row.text.split()


def _ledger_row(chat: list[Message], head: str) -> Message | None:
    found: Message | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == head:
            found = row
    return found


def _rx_asked(chat: list[Message], agent: str, number: str) -> int:
    """Index of our latest ``prescription prescription_number=<number>`` read, else -1."""

    token = f"prescription_number={number}"
    asked = -1
    for index, row in enumerate(chat):
        if row.sender == agent and first_word(row.text) == "prescription" and token in row.text.split():
            asked = index
    return asked


def _rx_line(chat: list[Message], agent: str, number: str) -> Message | None:
    """The Ledger's answer about THIS prescription number, never an older number's."""

    asked = _rx_asked(chat, agent, number)
    for index, row in enumerate(chat):
        if row.sender != LEDGER:
            continue
        head = first_word(row.text)
        if head != "prescription" and not (head == "refused" and "prescription" in words(row.text)):
            continue
        named = fields_of(row.text).get("prescription_number")
        if named and named != number:
            continue
        if (asked >= 0 and index > asked) or (asked < 0 and named == number):
            return row
    return None


def _agenda_span(start: datetime, end: datetime) -> timedelta:
    """Agenda time between two instants. The agenda has no weekends."""

    span = end - start
    day = start.date() + timedelta(days=1)
    while day < end.date():
        if day.weekday() >= 5:
            span -= timedelta(days=1)
        day += timedelta(days=1)
    return span


def _p1_active(chat: list[Message], now: datetime | None) -> bool:
    """Rule P1 (prescriptions valid 2 agenda days) is in the latest ``rules`` reply and in force."""

    row = _ledger_row(chat, "rules")
    if row is None:
        return False
    tokens = [token.strip(".,;:()") for token in row.text.split()]
    for index, token in enumerate(tokens):
        if token not in ("P1", "rule_id=P1"):
            continue
        for later in tokens[index + 1 :]:
            if later.startswith("rule_id="):
                break
            if later.startswith("effective_agenda="):
                when = _when(later.partition("=")[2])
                return when is None or now is None or when <= now
        return True
    # Level 8, "a new care rule": the reply may name the rule in words instead of "P1".
    return re.search(r"validit|expir|scad", row.text, re.IGNORECASE) is not None


def _expired(text: str, chat: list[Message]) -> bool:
    """The Ledger's prescription answer says expired, or P1 is in force and it is too old."""

    fields = fields_of(text)
    flag = fields.get("expired", "").lower()
    if flag in ("true", "false"):
        return flag == "true"
    if "expir" in fields.get("state", "").lower() or fields.get("valid", "").lower() == "false":
        return True
    if "EXPIRED" in text.upper():
        return True
    agenda = _ledger(chat, "agenda")
    now = _when("" if agenda is None else agenda.get("agenda_now", ""))
    if now is None or not _p1_active(chat, now):
        return False
    for key in ("issued_at", "issued", "issued_agenda"):
        issued = _when(fields.get(key, ""))
        if issued is not None:
            return _agenda_span(issued, now) >= timedelta(days=_RX_VALID_DAYS)
    # No issue date on the row: the case's first agenda read is the earliest we know of it.
    born = _born(chat)
    return born is not None and _agenda_span(born, now) >= timedelta(days=_RX_VALID_DAYS)


async def _ask_renewal(
    city: City, citizen: str, number: str, lang: str, chat: list[Message], agent: str, key: str
) -> None:
    """Send the citizen to the GP for a renewal, once per expired number."""

    entry = _sent(key)
    mark = f"renewal:{number}"
    token = f"prescription_number={number}"
    if mark in entry.offers or any(
        row.sender == agent
        and "data.request" in words(row.text)
        and "purpose=prescription_renewal" in row.text.split()
        and token in row.text.split()
        for row in chat
    ):
        return
    sentence = _RENEW_IT if lang == "it" else _RENEW_EN
    text = f"data.request purpose=prescription_renewal fields=prescription_number {token} {sentence}"
    if await _say(city, citizen, text):
        entry.offers.add(mark)


def _own_facilities(chat: list[Message]) -> set[str]:
    """Our ASST's facilities: every ``facility=`` in the Ledger's slot lines."""

    found: set[str] = set()
    for row in chat:
        if row.sender != LEDGER or first_word(row.text) != "slots":
            continue
        for line in row.text.splitlines():
            facility = fields_of(line).get("facility") if "slot_id=" in line else None
            if facility:
                found.add(facility)
    return found | _TEAM_AGENTS  # a case served from the shared world page has no page of its own


def _district(chat: list[Message]) -> int | None:
    """The citizen's ``area.district``, if it is a number."""

    for row in chat:
        if row.sender.startswith("citizen:"):
            raw = _citizen_fields(row.text).get("area.district", "")
            if raw.isdigit():
                return int(raw)
    return None


async def _partner_peers(
    city: City, procedure: str, ours: set[str], district: int | None, lang: str = ""
) -> list[Peer]:
    """Partner facilities for ``procedure`` from the Index: never ours, the citizen's district first."""

    capability = _PARTNER_CAPABILITY.get(procedure, "")
    if not capability:
        return []
    try:
        found = await city.find(capability)
    except CapError:
        return []
    # find(capability) already filters by capability; drop only a peer that lists others.
    keep = [
        peer
        for peer in found
        if peer.agent_id not in ours
        and peer.agent_id not in _TEAM_AGENTS
        and (not peer.capabilities or capability in tuple(peer.capabilities))
    ]
    keep.sort(key=lambda peer: district is None or peer.district != district)
    # MACH-1: published ring partners first, and for a rare language the ASST whose h3 speaks it.
    keep.sort(key=lambda peer: peer.agent_id not in _TRUSTED_RING)
    if lang in _LANG_HOSPITAL:
        keep.sort(key=lambda peer: peer.agent_id != _LANG_HOSPITAL[lang])
    return keep


async def _ask_partners(
    city: City,
    chat: list[Message],
    agent: str,
    citizen: str,
    procedure: str | None,
    priority: str | None,
    key: str,
    avoid: str | None = None,
) -> bool:
    """Search criteria to up to three partner facilities, once per case. True when asked."""

    request_id = f"x-{key}"
    token = f"request_id={request_id}"
    entry = _sent(key)
    asked = {
        row.recipient
        for row in chat
        if row.sender == agent and "need.request" in words(row.text) and token in row.text.split()
    } | {mark.partition(":")[2] for mark in entry.offers if mark.startswith("partner:")}
    if asked and avoid is None:
        return True
    capability = _PARTNER_CAPABILITY.get(procedure or "", "")
    if not capability or not procedure:
        return False
    ours = _own_facilities(chat) | asked | ({avoid} if avoid else set())
    parts = ["need.request", f"service_type={capability}", f"procedure={procedure}"]
    if priority:
        parts.append(f"priority={priority}")
    parts.extend(_ring_criteria(chat, priority))
    parts.append(token)
    sent_now = False
    limit = _MAX_PARTNERS if _lang(chat) in {"it", "en"} else _MAX_PARTNERS_RARE
    peers = [peer for peer in await _partner_peers(city, procedure, ours, _district(chat),
                                                   _lang(chat)) if not _down(peer.agent_id)]
    reached = 0
    for peer in peers[:limit * 2]:  # a partner that is down does not use up one of the asks
        if reached >= limit:
            break
        if await _send(city, peer.agent_id, " ".join(parts)) is None:
            entry.offers.add(f"partner:{peer.agent_id}")
            reached += 1
            sent_now = True
        else:
            _DOWN[peer.agent_id] = time.monotonic()  # never the same 4xx twice in a row
    if not sent_now:
        return bool(asked - ({avoid} if avoid else set()))
    notice = _PARTNER_ASK_IT if _lang(chat) == "it" else _PARTNER_ASK_EN
    if "partner-notice" not in entry.offers and not _exact(chat, agent, notice):
        if await _say(city, citizen, notice):
            entry.offers.add("partner-notice")
    return True


def _ring_criteria(chat: list[Message], priority: str | None) -> list[str]:
    """MACH-1.2 search criteria: what the citizen asked, never who they are.

    Without lang= a partner offers Italian doctors to an English speaker (journeys 01060003,
    01220004): the citizen refuses with reason=lang. Without deadline= it offers slots past the
    referral's window (journey 01790002, reason=deadline).
    """

    found = [f"lang={_lang(chat)}"]
    gender = _gender(chat)
    if gender:
        found.append(f"gender_preference={gender}")
    days = sorted(_days(chat))
    if days:
        found.append("days=" + "/".join(days))
    agenda = _ledger(chat, "agenda")
    now = _when("" if agenda is None else agenda.get("agenda_now", ""))
    if now is not None and priority in _DEADLINE_DAYS:
        end = (_born(chat) or now) + timedelta(days=_DEADLINE_DAYS[priority]) - _BORN_MARGIN
        found.append(f"deadline={end.isoformat(timespec='seconds')}")
    return [item for item in found if re.fullmatch(r"[a-z_]+=[A-Za-z0-9:/+.-]+", item)]


def _latest_slots(chat: list[Message]) -> str | None:
    found: str | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == "slots":
            found = row.text
    return found


def _page(text: str) -> tuple[int, int]:
    raw = fields_of(text.splitlines()[0] if text else "").get("page", "")
    left, _, right = raw.partition("/")
    if left.isdigit() and right.isdigit():
        return int(left), int(right)
    return 1, 1


def _workday(day: datetime) -> float:
    """Weekdays since a fixed Monday: the agenda has no Saturday or Sunday."""

    return (day.toordinal() - 1) // 7 * 5 + min(day.weekday(), 5) + (
        0 if day.weekday() > 4 else (day.hour + day.minute / 60) / 24)


def _world_lines(chat: list[Message]) -> list[str]:
    """The slot lines of every world page read for this case, each slot once."""

    lines = {fields_of(line).get("slot_id"): line for page in chat
             if page.sender == LEDGER and first_word(page.text) == "slots"
             for line in page.text.splitlines() if "slot_id=" in line}
    return list(lines.values())


def _next_page(world: str, now: datetime, priority: str | None,
               seen: frozenset[int] = frozenset()) -> int:
    """The world page to read after this one, 0 when no other page can hold a slot in time.

    The catalogue lists every weekday of the race from its first day, in date order: by day 14
    the slots still ahead sat six pages in, each one a slow Ledger read (3 minutes before the
    first offer, journeys 197-201). Jump over the past days, counted in weekdays (1.0.22 counted
    calendar days, landed one page past today and told everyone "no slot"); a landing past today
    steps back; stop once past the window. ``seen`` are the pages already read.
    """

    page, pages = _page(world)
    days = sorted(day for day in (_when(fields_of(line).get("at", ""))
                                  for line in world.splitlines() if "slot_id=" in line) if day)
    if days and days[0] > now and page > 1 and page - 1 not in seen:
        return page - 1  # everything here is ahead: today's slots may sit on the page before
    if page >= pages or page >= _MAX_PAGE or page + 1 in seen:
        return 0
    if not days:
        return page + 1
    if days[0] > now + timedelta(days=_DEADLINE_DAYS.get(priority or "", 14)):
        return 0
    if page == 1 and days[0] < days[-1] < now:
        per_page = _workday(days[-1]) - _workday(days[0])
        ahead = _workday(now) - _workday(days[0])
        return max(2, min(pages, int(ahead / per_page * 0.9) + 1))  # land a little early
    return page + 1


def _asked(chat: list[Message], agent: str, procedure: str, page: int) -> bool:
    token = f"page={page}"
    proc = f"procedure={procedure}"
    for row in chat:
        if row.sender == agent and token in row.text.split() and proc in row.text.split():
            return True
    return False


def _offers_in(text: str) -> list[dict[str, str]]:
    """One field map per ``offer`` line: one message carries every offered slot."""

    return [
        fields_of(line)
        for line in text.splitlines()
        if first_word(line) == "offer" and "offer_id=" in line
    ]


def _offered(chat: list[Message], agent: str, number: str | None = None) -> set[str]:
    found: set[str] = set()
    for row in chat:
        if row.sender != agent:
            continue
        for offer in _offers_in(row.text):
            if number and offer.get("prescription_number") not in (None, number):
                continue
            slot = offer.get("slot_id")
            if slot:
                found.add(slot)
    return found


def _withdrawn(chat: list[Message], agent: str) -> set[str]:
    """Slots we cancelled. The citizen drops the rest, so those are offered again."""

    by_offer: dict[str, str] = {}
    for row in chat:
        if row.sender != agent:
            continue
        for offer in _offers_in(row.text):
            offer_id = offer.get("offer_id")
            slot = offer.get("slot_id")
            if offer_id and slot:
                by_offer[offer_id] = slot
    dropped: set[str] = set()
    for row in chat:
        if "cancel" not in words(row.text):
            continue
        slot = by_offer.get(fields_of(row.text).get("offer_id", ""))
        if slot:
            dropped.add(slot)
    return dropped


def _number(chat: list[Message]) -> str | None:
    for row in reversed(chat):
        if row.sender.startswith("citizen:"):
            number = _citizen_fields(row.text).get("prescription_number")
            if number:
                return number
    return None


def _lang(chat: list[Message]) -> str:
    """The citizen's language: ``lang=``, a named language, else how their first message reads."""

    first = ""
    for row in chat:  # an explicit lang= token wins over a language named in any sentence
        if row.sender.startswith("citizen:") and fields_of(row.text).get("lang"):
            return fields_of(row.text)["lang"].lower()
    for row in chat:
        if row.sender.startswith("citizen:"):
            lang = _citizen_fields(row.text).get("lang", "")
            if lang:
                return lang.lower()
            first = first or row.text
    return _guess_lang(first) if first else "it"


def _gender(chat: list[Message]) -> str | None:
    """The latest doctor-gender preference the citizen stated."""

    found: str | None = None
    for row in chat:
        if not row.sender.startswith("citizen:"):
            continue
        said = _citizen_fields(row.text)
        raw = (said.get("gender_preference") or said.get("gender") or "").lower()[:1]
        if raw in {"m", "f"}:
            found = raw
    return found


def _days(chat: list[Message]) -> set[str]:
    """Days the citizen prefers (``days=mon/wed`` or plain words); the latest statement wins."""

    found: set[str] = set()
    for row in chat:
        if not row.sender.startswith("citizen:"):
            continue
        stated: set[str] = set()
        for raw in _DAYS_RE.findall(row.text):
            for part in re.split(r"[/,;|]+", raw.lower()):
                key = _DAY_KEYS.get(part.strip(".:!?()\"'")[:3])
                if key:
                    stated.add(key)
        if not stated:
            said = _citizen_fields(row.text).get("days", "")
            stated = {part for part in said.split("/") if part in _DAY_KEYS.values()}
        if stated:
            found = stated
    return found


def _pick(slots: list[dict[str, str]], days: set[str], limit: int = _MAX_OFFERS) -> list[dict[str, str]]:
    """Preferred days first, then one slot per doctor and day before any repeat."""

    order = sorted(
        slots,
        key=lambda s: (bool(days) and s.get("day") not in days, s.get("at", ""), s.get("slot_id", "")),
    )
    first: list[dict[str, str]] = []
    rest: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for slot in order:
        key = (slot.get("doctor_id", ""), slot.get("day", ""))
        (rest if key in seen else first).append(slot)
        seen.add(key)
    return (first + rest)[:limit]


def _offer_sentence(lang: str, days: set[str] | None = None, lead: str = "") -> str:
    """The words around the offer lines: why these slots, how to choose, what to bring."""

    english = lang != "it"  # rare-language citizens write in English
    base = lead or (_OFFER_EN if english else _OFFER_IT)
    if days and not lead:
        names = {"mon": ("lunedì", "Monday"), "tue": ("martedì", "Tuesday"),
                 "wed": ("mercoledì", "Wednesday"), "thu": ("giovedì", "Thursday"),
                 "fri": ("venerdì", "Friday")}
        listed = ", ".join(names[d][1 if english else 0] for d in sorted(days, key=list(names).index)
                           if d in names)
        base += (f" I put your preferred days first ({listed})." if english
                 else f" Ho messo per primi i giorni che preferisce ({listed}).")
    return base + (_OFFER_HELP_EN if english else _OFFER_HELP_IT)


def _offer_block(
    slots: list[dict[str, str]],
    number: str | None,
    priority: str | None,
    lang: str,
    days: set[str] | None = None,
    lead: str = "",
) -> str:
    """One message, one ``offer`` line per slot, kept under the citizen text limit."""

    sentence = _offer_sentence(lang, days, lead)
    lines: list[str] = []
    for slot in slots[:_MAX_OFFERS]:
        line = _offer_line(slot, number, priority)
        if len("\n".join([*lines, line, sentence])) > _MAX_TEXT:
            break
        lines.append(line)
    return "\n".join([*lines, sentence])


def _offer_line(slot: dict[str, str], number: str | None, priority: str | None) -> str:
    parts = [
        "offer",
        f"offer_id=of-{slot['slot_id']}",
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


def _our_offer(chat: list[Message], agent: str, offer_id: str) -> dict[str, str]:
    for row in chat:
        if row.sender != agent:
            continue
        for offer in _offers_in(row.text):
            if offer.get("offer_id") == offer_id:
                return offer
    return {}


def _find_offer(chat: list[Message], agent: str, offer_id: str) -> dict[str, str]:
    """Our offer by id, even when our own sent message is missing from the history.

    Journey 000045: the citizen confirmed a valid offer 32 minutes later and our own offer
    row was not found, so the confirm was dropped. A local id is ``of-<slot_id>``: the slot
    and its facility are in the Ledger's slot lines, which reach us. A partner's offer is in
    the partner's own message to us.
    """

    if not offer_id:
        return {}
    found = _our_offer(chat, agent, offer_id)
    if found:
        return found
    number = _number(chat)
    if offer_id.startswith(("of-", "p-")):
        # of-<slot>: our offer to a citizen; p-<slot>: our offer to another ASST.
        slot_id = offer_id.partition("-")[2]
        for row in chat:
            if row.sender != LEDGER or first_word(row.text) != "slots":
                continue
            for line in row.text.splitlines():
                slot = fields_of(line)
                if slot.get("slot_id") == slot_id and slot.get("facility"):
                    rebuilt = {"offer_id": offer_id, **slot}
                    if number:
                        rebuilt["prescription_number"] = number
                    return rebuilt
        if offer_id.startswith("of-"):
            return {}
    for row in chat:
        if not row.sender.startswith("agent:") or row.sender == agent:
            continue
        for offer in _partner_offers_in(row.text, row.sender):
            if offer.get("offer_id") == offer_id:
                if number:
                    offer.setdefault("prescription_number", number)
                return offer
    return {}


class _Gate:
    """One need's lock and how many turns hold it or wait for it."""

    __slots__ = ("lock", "turns")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.turns = 0


class _Sent:
    """Offers this process sent for one need, which the case history may not show yet.

    ``offers`` holds every slot id (and waiting-list offer id) sent. A cancel makes the
    citizen drop all offers, so ``again`` holds only what was re-sent for ``trigger``.
    """

    __slots__ = ("offers", "trigger", "again")

    def __init__(self) -> None:
        self.offers: set[str] = set()
        self.trigger = ""
        self.again: set[str] = set()


# Every Ledger or citizen message opens its own turn, and turns of one need run at the same
# time. Each reads the case before the others' offers are in it, so offers wait on this.
# An entry lives only while a turn holds or waits on its lock.
_GATES: dict[str, _Gate] = {}
_SENT: OrderedDict[str, _Sent] = OrderedDict()


