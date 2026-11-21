"""Part of agent.py (loaded into its namespace by _load_parts). Do not import directly."""

from __future__ import annotations

def _sent(key: str) -> _Sent:
    entry = _SENT.get(key)
    if entry is None:
        entry = _SENT[key] = _Sent()
        while len(_SENT) > _SENT_NEEDS:
            _SENT.popitem(last=False)
    else:
        _SENT.move_to_end(key)
    return entry


async def _desk(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    hold_self: bool,
    fixed: str | None,
) -> None:
    if message.sender == city.agent_id:
        return
    chat = _cached(chat)
    _TURN_END.set(time.monotonic() + _TURN_BUDGET)
    suspect = _impostor(message)
    if suspect and suspect != "not_listed_ok":
        # TRUST RING: never answered, never obeyed, never given data. Logged for ring.alert.
        _LOG.info("care.ring.alert suspect=%s reason=%s", message.sender, suspect)
        return
    if fixed is None and message.sender.startswith("agent:") and _partner_request(message):
        # MACH-1: first answer to a partner within 10 s; a plain "received" if the Ledger is slow.
        turn = asyncio.ensure_future(
            _desk_turn(city, message, chat, hold_self=hold_self, fixed=fixed))
        done, _pending = await asyncio.wait({turn}, timeout=_ACK_AFTER)
        if not done:
            await _say(city, message.sender, _PARTNER_ACK)
        await turn
        return
    if fixed is not None or not message.sender.startswith("citizen:"):
        await _desk_turn(city, message, chat, hold_self=hold_self, fixed=fixed)
        return
    # A citizen always gets an answer in the same turn: the review marks a message left
    # without reply as passive (-1). _say flags every message that reaches the citizen.
    flag = _REPLIED.set([False])
    key = message.need_id or message.msg_id
    if key not in _WARM and len(_WARM) < _SENT_NEEDS:
        _WARM[key] = asyncio.ensure_future(_warm_line(city, message.text, _lang(chat)))
    try:
        turn = asyncio.ensure_future(
            _desk_turn(city, message, chat, hold_self=hold_self, fixed=fixed))
        if "confirm" not in words(message.text):
            # Ledger reads can take 15-20 s (live log, level 4): a short "on it" first, so
            # the citizen is never left waiting in silence; the offer follows in the same turn.
            done, _pending = await asyncio.wait({turn}, timeout=_ACK_AFTER)
            if not done and not _REPLIED.get()[0]:
                await _no_silence(city, message, chat)
        await turn
        if not _REPLIED.get()[0]:
            await _no_silence(city, message, chat)
    finally:
        _REPLIED.reset(flag)


async def _desk_turn(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    hold_self: bool,
    fixed: str | None,
) -> None:
    kind = words(message.text)
    head = first_word(message.text)
    citizen_turn = message.sender.startswith("citizen:")
    agent_turn = message.sender.startswith("agent:")
    if citizen_turn and fixed is None and _emergency(message):
        # Red flag: no booking, no advice, the emergency line only.
        if not _we(chat, city.agent_id, "escalate.emergency", to=message.sender,
                   word="escalate.emergency"):
            sentence = _EMERGENCY_IT if _lang(chat) == "it" else _EMERGENCY_EN
            await _say(city, message.sender, f"escalate.emergency reason=red_flag {sentence}")
        return
    if citizen_turn and fixed is None and _we(chat, city.agent_id, "escalate.emergency",
                                              to=message.sender, word="escalate.emergency"):
        # Level 8: the case stopped at the emergency. Nothing is booked after it; a later
        # message gets the same instruction in plain words, never silence and never an offer.
        await _say(city, message.sender, _EMERGENCY_IT if _lang(chat) == "it" else _EMERGENCY_EN)
        return
    key = message.need_id or message.msg_id
    if agent_turn and fixed is None and head not in _ACT_WORDS and message.sender in (
        _partners_asked(chat, city.agent_id, key)
    ):
        # A partner we asked answers in plain words. "No availability" closes its part.
        if _NO_AVAILABILITY.search(_plain(message.text)):
            refusal = Message(type="chat", sender=message.sender, msg_id=message.msg_id,
                              need_id=message.need_id, recipient=message.recipient,
                              body={"text": f"cancel request_id=x-{key} reason=no_availability"})
            await _partner_cancel(city, refusal, chat, fixed=fixed)
        return
    if agent_turn and head == "release":
        # MACH-1.3: an asker gives back the slots we kept for its request. Nothing to answer.
        mine = f"ring:{message.sender}|{fields_of(message.text).get('request_id', '')}"
        for slot_id in [sid for sid, (_at, who) in _SHOWN.items() if who == mine]:
            del _SHOWN[slot_id]
        return
    if agent_turn and fixed is None and _partner_request(message):
        await _answer_partner(city, message, chat)
        return
    if "confirm" in kind and (citizen_turn or agent_turn):
        await _on_confirm(city, message, chat, hold_self=hold_self)
        return
    if "booking.receipt" in kind and agent_turn:
        await _forward_receipt(city, message, chat)
        return
    if agent_turn and head == "prescription" and message.sender in _TEAM_AGENTS:
        # The GP's notice is already in this case history for the citizen's booking turn.
        # It asks nothing back; an acknowledgement only adds another Relay delivery.
        return
    # MACH-1.2: a partner's reply can open with "cancel reason=no_availability" and carry
    # relaxed "offer ..." lines below. Read it line by line: real slots win over the cancel.
    slots = agent_turn and bool(_partner_offers_in(message.text, message.sender))
    if agent_turn and message.sender not in _TEAM_AGENTS:
        _LOG.info("care.partner.said %s %s", message.sender, message.text[:400])
    if "cancel" in kind and agent_turn and not slots:
        await _partner_cancel(city, message, chat, fixed=fixed)
        return
    if ("offer" in kind or slots) and agent_turn:
        key = message.need_id or message.msg_id
        await _locked(key, lambda: _on_partner_offer(city, message, chat))
        return
    if agent_turn and fixed is None:
        await _agent_note(city, message, chat)
        return
    if (
        citizen_turn
        and "prescription_number=" not in message.text
        and _open_cross_ask(chat, city.agent_id, message.sender)
    ):
        await _on_cross_consent(city, message, chat)
        return
    if citizen_turn and fixed is None:
        if _open_booking_ask(message.sender) and head not in {"need.request"}:
            await _on_booking_consent(city, message, chat)
            return
        if "cancel" in kind and "confirm" not in kind:
            # Journey 116: a refusal names the offer too, and is never a pick. Journeys 224 and
            # 227: the citizen cancels the offers it leaves and confirms the one it takes, in the
            # same second. So a cancel is answered only when it leaves nothing open and nothing
            # on its way to a facility, and once per burst: then we look for something else.
            clock = time.monotonic()
            # The citizen gives the slot back: the next citizen can have it at once.
            back = _find_offer(chat, city.agent_id,
                               fields_of(message.text).get("offer_id", "")).get("slot_id", "")
            if _SHOWN.get(back, (0.0, ""))[1] == key:
                del _SHOWN[back]
            busy = (_open_offers(chat, city.agent_id, message.sender)
                    or _who(chat, message.sender) in _PENDING
                    or clock - _CANCEL_AT.get(key, -1e9) < _CANCEL_GAP)
            if busy:
                if _REPLIED.get() is not None:
                    _REPLIED.get()[0] = True  # nothing to say: the choice has its own turn
                return
            _CANCEL_AT[key] = clock
            await _advance(city, message, chat, fixed=fixed, replacing=False)
            return
        offers = _open_offers(chat, city.agent_id, message.sender)
        new_number = _citizen_fields(message.text).get("prescription_number")
        same_case = new_number is None or any(
            offer.get("prescription_number") in (None, new_number) for offer in offers
        )
        if offers and same_case:
            picked = _choice(message.text, offers)
            if picked is not None:
                await _on_confirm(city, message, chat, hold_self=hold_self, picked=picked)
                return
            if _changes_preferences(message.text):
                # New days or doctor preference: send the slots again, preferred ones first.
                await _advance(city, message, chat, fixed=fixed, replacing=True)
                return
        if _medical_question(message.text):
            await _say(city, message.sender, _phrase("medical", _lang(chat)))
        if _booked(chat, city.agent_id, message.sender):
            return  # already booked: _no_silence answers with the next step, never new offers
    if message.sender == LEDGER or citizen_turn:
        await _advance(city, message, chat, fixed=fixed, replacing=False)


# --- every citizen gets an answer; partner agents get one too -----------------------------------

# Set per citizen turn by _desk; _say flips it when something reaches the citizen.
_REPLIED: contextvars.ContextVar[list[bool] | None] = contextvars.ContextVar(
    "booking_replied", default=None
)
# Offers this process last sent to each citizen (our own sent rows can be missing from history).
_LAST_OFFERS: dict[str, list[dict[str, str]]] = {}
# Citizen -> offer whose booking consent we asked for; the value counts the asks.
_BOOK_ASKED: dict[str, tuple[str, int]] = {}
# Citizen -> offer forwarded to the facility, waiting for the receipt.
_PENDING: dict[str, str] = {}


def _who(chat: list[Message], citizen: str) -> str:
    """Key of one citizen's need: a citizen can come back with another need later."""

    need = next((row.need_id for row in chat if row.need_id), "")
    return f"{need}|{citizen}"

# Need -> the agent (another ASST's booking centre) whose confirm we forwarded.
_CONFIRMED_BY: dict[str, str] = {}
# Slots already sent to a facility for a hold (booked or refused): never offered to anyone again.
_USED: set[str] = set()
# Slot -> (when, need) of our latest offer of it; _SHOWN_FOR seconds of head start for that need.
_SHOWN: dict[str, tuple[float, str]] = {}
# 21/11: five citizens confirmed the same slot in one minute (40 s of head start, but a citizen
# answers after 3-4 minutes); only one of them can get it.
# 21/11 10:50: nine citizens confirmed s2 22/12 10:30; the facility's refusal reached the eight
# losers up to 13 minutes later. A slot we offered stays with that citizen for 15 minutes (by
# then it is forwarded and in _USED, given back by a cancel, or past).
_SHOWN_FOR = 900.0
# Procedure -> the world page that held today's slots for the last case: the next case starts
# there instead of page 1 (one slow Ledger read less). Days only move forward.
_TODAY_PAGE: dict[str, int] = {}
# Level 7 (faults and contention). Agent -> when a send to it was last refused (facility switched
# off, partner down): its slots are not offered and it is not asked again for _DOWN_FOR seconds.
_DOWN: dict[str, float] = {}
_DOWN_FOR = 180.0
# Need|citizen -> when the choice went to the facility. With no receipt and no refusal after
# _PENDING_STALE seconds, the citizen's next message frees the case (the facility went down).
_PENDING_AT: dict[str, float] = {}
_PENDING_STALE = 480.0


def _down(agent_id: str) -> bool:
    return time.monotonic() - _DOWN.get(agent_id, -1e9) < _DOWN_FOR


# Need -> when we last looked for other slots after the citizen's cancels.
_CANCEL_AT: dict[str, float] = {}
_CANCEL_GAP = 30.0
# Citizens whose receipt we forwarded.
_BOOKED: set[str] = set()
# (agent, need) pairs we already explained our message format to.
_AGENT_NOTED: set[tuple[str, str]] = set()

_EMERGENCY_IT = "Questa è un'emergenza: chiami subito il 112. Non prenoto una visita ordinaria."
_EMERGENCY_EN = "This is an emergency: call 112 now. I am not booking a routine visit."
_CHEST = r"\b(petto|torace|sterno|chest)\b"
_CHEST_BAD = (
    r"\b(dolor|male?\b|peso\b|pesante|oppress|opprim|stringe|stretta|schiacci|brucia|"
    r"fitt[ae]\b|morsa|costri|pain|hurt|ach(e|es|ing)\b|tight|pressure|crush|heavy|squeez)"
)
_LEGS = r"\b(gamb[ae]|legs?)\b"
_LEGS_BAD = (
    r"\b(forza|debol|non (le )?sento|non riesco a muover|non si muov|cedono|paralizz|"
    r"intorpidit|addormentat|insensibil|strength|weak|numb|can'?t (feel|move)|"
    r"cannot (feel|move)|giving way|give way|paraly)"
)
_RED_FLAG_RE = tuple(
    re.compile(rf"{a}.{{0,40}}{b}|{b}.{{0,40}}{a}", re.DOTALL)
    for a, b in ((_CHEST, _CHEST_BAD), (_LEGS, _LEGS_BAD))
) + (re.compile(r"\binfarto\b|heart attack|\bictus\b|\bstroke\b|non riesco a respirare|"
               r"fatico a respirare|fatica a respirare|manca il respiro|can'?t breathe|cannot breathe|"
               r"sto per svenire|sono svenut|perso conoscenza|passed out|fainted"),)

# Sentences around the tokens. None of them holds an act word on its own (offer, confirm,
# cancel, consent, prescription, report, checkin, hold): understood() reads the LAST act word
# in a text as the act, so a stray one would change what the citizen and the judge read.
_PHRASES: dict[str, tuple[str, str]] = {
    "medical": (
        "Sono il centro prenotazioni: non posso dare diagnosi, consigli sui farmaci o ricette. "
        "Per questo si rivolga al suo medico di base; io intanto la aiuto a fissare la visita.",
        "I am the booking centre: I cannot give diagnoses, medicine advice or medical "
        "referrals. Please ask your GP about that; meanwhile I can help you book the visit.",
    ),
    "need_number": (
        "Buongiorno, la aiuto volentieri a prenotare. Mi scrive il numero della ricetta del "
        "medico di base (per esempio prescription_number=prescription_number-ABCD)? Se vuole, "
        "mi dica anche lingua, giorni preferiti o se preferisce un medico uomo o donna.",
        "Hello, I am glad to help you book. Could you send me the number your GP gave you "
        "(for example prescription_number=prescription_number-ABCD)? You can also tell me "
        "your language, preferred days, or whether you prefer a male or female doctor.",
    ),
    "bad_number": (
        "Non trovo questa ricetta nel registro. Può ricontrollare il numero e riscrivermelo "
        "esattamente come lo ha ricevuto dal medico di base?",
        "I can't find this number in the register. Could you check it and send it again "
        "exactly as your GP wrote it?",
    ),
    "checking": (
        "Grazie, ho ricevuto la sua richiesta e me ne occupo subito: sto verificando la ricetta "
        "e i posti liberi, le mando le proposte tra pochi istanti.",
        "Thank you, I have your request and I am on it: I am checking your referral and the free "
        "slots, you will have the options in a moment.",
    ),
    "still_checking": (
        "Ci sto ancora lavorando: il registro risponde lentamente, le mando i posti liberi "
        "appena sono pronti. Non serve riscrivere, la sua richiesta è già in lavorazione.",
        "I am still working on it: the register is slow to answer, you will get the free slots "
        "as soon as they are ready. No need to write again, your request is in progress.",
    ),
    "offers_open": (
        "Le proposte che le ho inviato sono ancora valide: mi scriva l'offer_id che preferisce, "
        "oppure semplicemente 'la prima', 'la seconda' e così via. Se preferisce altri giorni "
        "o un medico uomo o donna, me lo dica e cerco di nuovo.",
        "The options I sent you are still open: write me the offer_id you prefer, or simply "
        "'the first one', 'the second one' and so on. If you prefer other days or a male or "
        "female doctor, tell me and I will search again.",
    ),
    "partners_wait": (
        "Sto ancora aspettando la risposta delle ASST vicine: le scrivo appena ho un posto.",
        "I am still waiting for the nearby ASSTs to answer: I will write as soon as I have a slot.",
    ),
    "pending": (
        "Ho inoltrato la sua scelta alla struttura: le giro la ricevuta appena arriva.",
        "I passed your choice to the facility: I will forward the receipt as soon as it arrives.",
    ),
    "booked": (
        "La sua prenotazione è registrata. Il giorno della visita si presenti alla struttura "
        "con il booking_id della ricevuta per registrare l'arrivo. Buona giornata!",
        "Your booking is registered. On the day of the visit go to the facility with the "
        "booking_id from your receipt to register your arrival. Have a nice day!",
    ),
    "unknown_offer": (
        "Non trovo questa proposta tra quelle che le ho inviato.",
        "I can't find this option among the ones I sent you.",
    ),
    "book_consent": (
        "Per completare la prenotazione mi serve il suo consenso a usare i dati necessari.",
        "To complete the booking I need your agreement to use the necessary data.",
    ),
    "book_consent_again": (
        "Grazie! Per registrarla mi serve il riferimento del suo consenso (consent_ref).",
        "Thank you! To register it I need the reference of your agreement (consent_ref).",
    ),
    "book_declined": (
        "Va bene, non prenoto nulla senza il suo consenso. Le proposte restano valide se "
        "cambia idea.",
        "All right, I won't book anything without your agreement. The options stay open if "
        "you change your mind.",
    ),
    "still_none": (
        "Purtroppo non ci sono ancora posti adatti. Se può indicarmi altri giorni o un'altra "
        "preferenza sul medico, riprovo subito.",
        "Unfortunately there is still no suitable slot. If you can tell me other days or "
        "another doctor preference, I will search again at once.",
    ),
    "refused": (
        "La struttura non ha potuto tenere questo posto: le propongo subito altre date.",
        "The facility could not keep this slot: I am sending you other dates right away.",
    ),
    "receipt": (
        "Prenotazione confermata: ho sentito la struttura, che ha bloccato e registrato il posto "
        "a suo nome. Il giorno della visita si presenti alla struttura con questo "
        "booking_id, la ricetta e un documento per registrare l'arrivo. Se può, usi i mezzi "
        "pubblici; per esigenze di accessibilità me lo scriva e avviso la struttura.",
        "Booking confirmed: I contacted the facility, which held and registered the slot in your "
        "name. On the day of the visit go to the facility with this booking_id, "
        "your referral and an ID document to register your arrival. If you can, use public "
        "transport; for accessibility needs write to me and I will tell the facility.",
    ),
}
_ACT_WORDS = frozenset(
    {"need.request", "offer", "confirm", "cancel", "prescription", "consent", "data.request",
     "data.response", "checkin", "report", "booking.receipt", "escalate.emergency", "hold",
     "case.closed"}
)


def _phrase(
    name: str,
    lang: str,
    *,
    chat: list[Message] | None = None,
    agent: str = "",
    citizen: str = "",
) -> str:
    italian, english = _PHRASES[name]
    text = italian if lang == "it" else english
    if name == "unknown_offer" and chat is not None:
        ids = [o.get("offer_id", "") for o in _open_offers(chat, agent, citizen) if o.get("offer_id")]
        if ids:
            listed = " ".join(f"offer_id={oid}" for oid in ids[:_MAX_OFFERS])
            more = "Quelle aperte sono:" if lang != "en" else "The open ones are:"
            text = f"{text} {more} {listed}"
    return text


def _emergency(message: Message) -> bool:
    if understood(message).body.get("is_emergency") is True:
        return True
    lowered = message.text.lower()
    if any(flag in lowered for flag in _RED_FLAGS):
        return True
    return any(pattern.search(lowered) for pattern in _RED_FLAG_RE)


def _booked(chat: list[Message], agent: str, citizen: str) -> bool:
    return _who(chat, citizen) in _BOOKED or any(
        row.sender == agent and row.recipient in ("", citizen)
        and first_word(row.text) == "booking.receipt"
        for row in chat
    )


def _gone(offer: dict[str, str]) -> bool:
    """True when the offered time is past, or too close for a hold to reach the Ledger in time.

    21/11 10:36-11:11: 33 confirms, no receipt. Citizens confirmed (and repeated every 15
    minutes) slots already past, we forwarded them and listed them again as "still valid".
    Unknown agenda time or no date in the offer (waiting list): not gone.
    """

    when = _when(offer.get("agenda_date") or offer.get("at", ""))
    now = _agenda_guess()
    return when is not None and now is not None and when < now + _LEAD


def _open_offers(chat: list[Message], agent: str, citizen: str) -> list[dict[str, str]]:
    """The offers the citizen can still pick: our latest offer message, minus cancelled ones."""

    if not citizen or _who(chat, citizen) in _PENDING:
        return []
    latest: list[dict[str, str]] = []
    cancelled: set[str] = set()
    booked = False
    for row in chat:
        if row.sender == citizen and "cancel" in words(row.text):
            cancelled.add(fields_of(row.text).get("offer_id", ""))  # the citizen refused it
        if row.sender != agent or row.recipient not in ("", citizen):
            continue
        found = _offers_in(row.text)
        if found:
            latest = found
        head = first_word(row.text)
        if head == "cancel":
            cancelled.add(fields_of(row.text).get("offer_id", ""))
        if head == "booking.receipt":
            booked = True
    if booked:
        return []
    remembered = _LAST_OFFERS.get(citizen, [])
    pool = remembered or latest
    return [o for o in pool
            if o.get("offer_id") not in cancelled and o.get("offer_id") and not _gone(o)]


def _choice(text: str, offers: list[dict[str, str]]) -> dict[str, str] | None:
    """Which open offer a plain sentence picks, or None when it picks none for sure."""

    if not offers:
        return None
    tokens = {token.strip(".,;:!?()\"'") for token in text.replace("=", " ").split()}
    named = fields_of(text)
    for offer in offers:
        oid, sid = offer.get("offer_id", ""), offer.get("slot_id", "")
        if named.get("offer_id") == oid or named.get("slot_id") == sid or oid in tokens:
            return offer
        if sid and sid in tokens:
            return offer
    plain = _plain(text)
    hit = _ORDINAL.search(plain)
    if hit:
        word = hit.group(1) or hit.group(2)
        index = int(word) - 1 if word.isdigit() else _ORDINALS.get(word, 99)
        if -1 <= index < len(offers):
            return offers[index]
    yes = _YES.search(plain) is not None
    no = _NO.search(plain) is not None and not yes
    if no:
        return None
    wanted, _ = _said_days(plain)
    clock = re.search(r"\b(?:alle|ore|at|h)\s*(\d{1,2})(?:[:.](\d{2}))?\b", plain) or re.search(
        r"\b(\d{1,2})[:.](\d{2})\b", plain
    )
    if wanted or clock:
        picks = []
        for offer in offers:
            day = (offer.get("agenda_day") or "").lower()[:3]
            at = _when(offer.get("agenda_date", ""))
            if wanted and day not in wanted:
                continue
            if clock and (at is None or at.hour != int(clock.group(1))):
                continue
            picks.append(offer)
        if len(picks) == 1 and (yes or clock):
            return picks[0]
    if len(offers) == 1 and yes:
        return offers[0]
    if len(plain.split()) <= 3:
        digit = re.fullmatch(r"\D*([1-6])\D*", plain)
        if digit and int(digit.group(1)) <= len(offers):
            return offers[int(digit.group(1)) - 1]
    return None


def _changes_preferences(text: str) -> bool:
    said = _citizen_fields(text)
    return bool(said.get("days") or said.get("gender") or said.get("gender_preference"))


def _open_booking_ask(citizen: str) -> str:
    return _BOOK_ASKED.get(citizen, ("", 0))[0]


async def _ask_booking_consent(
    city: City, chat: list[Message], agent: str, citizen: str, offer_id: str
) -> None:
    """The citizen picked an offer without a consent_ref: ask for it, once per offer."""

    asked, count = _BOOK_ASKED.get(citizen, ("", 0))
    if asked == offer_id and count:
        return
    sentence = _phrase("book_consent", _lang(chat))
    if await _say(city, citizen, f"consent purpose=booking offer_id={offer_id} {sentence}"):
        _BOOK_ASKED[citizen] = (offer_id, 1)


async def _on_booking_consent(city: City, message: Message, chat: list[Message]) -> None:
    """The answer to our booking consent question: book, ask once more, or stop politely."""

    citizen = message.sender
    offer_id, count = _BOOK_ASKED.get(citizen, ("", 0))
    ref = _citizen_fields(message.text).get("consent_ref", "")
    lang = _lang(chat)
    if ref:
        _BOOK_ASKED.pop(citizen, None)
        await _on_confirm(city, message, chat, hold_self=False, picked={"offer_id": offer_id},
                          consent_ref=ref)
        return
    plain = _plain(message.text)
    if _YES.search(plain) and count < 2:
        _BOOK_ASKED[citizen] = (offer_id, count + 1)
        sentence = _phrase("book_consent_again", lang)
        await _say(
            city,
            citizen,
            f"data.request purpose=booking fields=consent_ref offer_id={offer_id} {sentence}",
        )
        return
    # A plain answer without the field we asked for is a refusal (kit rules): move on.
    _BOOK_ASKED.pop(citizen, None)
    await _say(city, citizen, _phrase("book_declined", lang))


# (citizen, phrase) -> when this process last said a status phrase to that citizen.
_NOTED: dict[tuple[str, str], float] = {}


async def _no_silence(city: City, message: Message, chat: list[Message]) -> None:
    """Nothing reached the citizen this turn: tell them where their booking stands."""

    citizen = message.sender
    try:
        fresh = await _history(city, message.need_id, chat)
    except CapError:
        fresh = chat
    known = {row.msg_id for row in fresh}
    fresh = list(fresh) + [row for row in chat if row.msg_id not in known]
    agent = city.agent_id
    lang = _lang(fresh)
    key = message.need_id or message.msg_id
    number = _number(fresh)
    still = _open_offers(fresh, agent, citizen)
    if _booked(fresh, agent, citizen):
        name = "booked"
    elif _who(fresh, citizen) in _PENDING:
        name = "pending"
    elif still:
        # "Which offer_id, slot_id?": the citizen reads fields, so list the open offers again.
        lines = ["offer " + " ".join(f"{k}={v}" for k, v in offer.items()) for offer in still]
        await _say(city, citizen, "\n".join([*lines, _phrase("offers_open", lang)])[:_MAX_TEXT])
        return
    elif "partner-closed" in _sent(key).offers:
        name = "still_none"
    elif _partners_asked(fresh, agent, key) and not _partner_offered(key, number):
        name = "partners_wait"
    elif not number:
        sentence = _phrase("need_number", lang)
        await _say(city, citizen, f"data.request purpose=booking fields=prescription_number {sentence}")
        return
    else:
        row = _rx_line(fresh, agent, number)
        name = "bad_number" if row is not None and first_word(row.text) == "refused" else "checking"
        if name == "bad_number":
            sentence = _phrase(name, lang)
            await _say(
                city, citizen, f"data.request purpose=booking fields=prescription_number {sentence}"
            )
            return
    if name == "checking":
        # Diagnostics (Sat 21/11: 0 offers in 58 first replies): what this case is missing.
        rx = _ledger(fresh, "prescription") or {}
        procedure = rx.get("procedure", "")
        _LOG.warning(
            "care.stall need=%s rx=%s proc=%s agenda=%s slots_in_case=%d cache_pages=%d "
            "asked_world=%d ledger_rows=%d", (message.need_id or "")[-12:], rx.get("state", "-"),
            procedure or "-", bool(_ledger(fresh, "agenda")),
            sum(1 for row in fresh if row.sender == LEDGER and first_word(row.text) == "slots"),
            len(_world_cached(procedure)) if procedure else -1,
            sum(1 for row in fresh if row.sender == agent and row.recipient == LEDGER
                and first_word(row.text) == "world"),
            sum(1 for row in fresh if row.sender == LEDGER))
        # A redelivery: this message already got its "I am on it" after it. A NEW message after
        # one (21/11 12:31: no offer came, the citizen wrote again) gets a reply, never silence.
        said = {_phrase("checking", lang), _phrase("still_checking", lang)}
        at = next((i for i, row in enumerate(fresh) if row.msg_id == message.msg_id), len(fresh))
        if any(row.sender == agent and row.text.strip() in said for row in fresh[at + 1:]):
            return
        if _exact(fresh, agent, _phrase("checking", lang)):
            name = "still_checking"
    if name == "pending":
        # A confirm and its cancels in the same second run as parallel turns: both read the
        # history before either wrote, and the citizen got "Ho inoltrato..." twice (000319).
        mark = (citizen, name)
        if time.monotonic() - _NOTED.get(mark, float("-inf")) < 120.0:
            return
        _NOTED[mark] = time.monotonic()
    if name == "still_none":
        # Four cancels in the same second (journey 01790002) gave four identical sentences:
        # one is an answer, four read as a passive agent.
        clock = time.monotonic()
        if clock - _STILL_NONE_AT.get(citizen, -1e9) < _STILL_NONE_GAP:
            return
        _STILL_NONE_AT[citizen] = clock
    await _say(city, citizen, _phrase(name, lang))


_STILL_NONE_AT: dict[str, float] = {}
_STILL_NONE_GAP = 20.0


# --- other ASSTs (and our own agents) writing to the booking centre -----------------------------

_SERVICE_PROCEDURE = {
    "hospital.physiatry": "VIS-FIS",
    "specialist_care.cardiology": "VIS-CARD",
    "family_counseling.gynecology": "CONS-GIN",
}
_PROCEDURE_WORDS = (
    ("VIS-FIS", re.compile(r"\b(vis-fis|fisiatr|physiatr|fisioterap|physiother|riabilit|rehab)")),
    ("CTRL-CARD", re.compile(r"\b(ctrl-card|controllo cardiolog|cardiology follow)")),
    ("VIS-CARD", re.compile(r"\b(vis-card|cardiolog|cardio)")),
    ("CONS-GIN", re.compile(r"\b(cons-gin|ginecolog|gynaecolog|gynecolog|consultorio)")),
)
_NO_AVAILABILITY = re.compile(
    r"\b(nessun|non (abbiamo|ci sono|c'e)|niente|esaurit|no (slot|availability|free)|"
    r"not available|unavailable|fully booked|none available|we have no)"
)
_SLOT_WORDS = re.compile(
    r"\b(slot|posti|posto|disponibilit|availability|available|appointment|appuntament|"
    r"agenda|need\.request)"
)


def _partner_fields(text: str) -> dict[str, str]:
    """Search criteria from a partner's request, tokens first, then plain words."""

    found = fields_of(text)
    plain = _plain(text)
    if not found.get("procedure"):
        mapped = _SERVICE_PROCEDURE.get(found.get("service_type", ""))
        if mapped:
            found["procedure"] = mapped
        else:
            for procedure, pattern in _PROCEDURE_WORDS:
                if pattern.search(plain):
                    found["procedure"] = procedure
                    break
    if not found.get("priority"):
        hit = re.search(r"\b(?:priorit[a-z]*|priority)\s*[:=]?\s*([ubdp])\b", plain)
        if hit:
            found["priority"] = hit.group(1).upper()
    return found


def _partner_request(message: Message) -> bool:
    """An agent asks us for slots: ``need.request`` or a plain question naming a visit."""

    if message.sender in _TEAM_AGENTS:
        return False
    if first_word(message.text) == "need.request":
        return True
    head = first_word(message.text)
    if head in _ACT_WORDS:
        return False
    plain = _plain(message.text)
    return bool(_SLOT_WORDS.search(plain)) and bool(_partner_fields(message.text).get("procedure"))


def _desk_partner_slots(
    lines: list[str],
    *,
    now: datetime,
    priority: str | None,
    procedure: str,
    lang: str = "",
    gender: str = "",
    days: set[str] | None = None,
    after: datetime | None = None,
    deadline: datetime | None = None,
    open_end: bool = False,
    skip: frozenset[str] = frozenset(),
) -> list[dict[str, str]]:
    """Free slots of any of our facilities inside the priority window, spread, at most six.

    A partner may pass the citizen's ``lang``, ``gender`` and ``days`` (shared ring format):
    language and gender filter by our doctor table, days only put those slots first.
    MACH-1.3 R4: the asker's ``after`` and ``deadline`` only narrow our own window.
    ``open_end`` drops the far end of the window (the first free date for ``next_free``).
    """

    if priority == "U":
        horizon: datetime | None = now.replace(hour=23, minute=59, second=59, microsecond=0)
    elif priority == "P":
        horizon = None
    else:
        horizon = now + timedelta(days=_DEADLINE_DAYS.get(priority or "", 14))
    earliest = now + _lead(priority)
    if after is not None:
        earliest = max(earliest, after - timedelta(seconds=1))  # a slot at ``after`` is inside
    if deadline is not None and (horizon is None or deadline < horizon):
        horizon = deadline
    if open_end:
        horizon = None
    timed: list[tuple[datetime, dict[str, str]]] = []
    for line in lines:
        slot = fields_of(line)
        at = _when(slot.get("at", ""))
        if not slot.get("slot_id") or not slot.get("doctor_id") or not slot.get("facility"):
            continue
        if slot["slot_id"] in skip:
            continue
        if at is None or slot.get("procedure") not in (None, procedure):
            continue
        free = slot.get("free")
        if free is not None and free.isdigit() and int(free) <= 0:
            continue
        if slot.get("urgent_reserve", "").lower() == "true" and priority != "U":
            continue
        if not earliest < at or (horizon is not None and at >= horizon):
            continue
        spec = _DOCTORS.get(slot["doctor_id"].rsplit("-", 1)[-1])
        if lang and lang not in ("it",) and spec is not None and not _speaks(slot["doctor_id"], lang):
            continue
        if gender in ("m", "f") and spec is not None and spec[1] != gender:
            continue
        timed.append((at, slot))
    timed.sort(key=lambda item: (item[0], item[1]["slot_id"]))
    return _pick([slot for _, slot in timed], days or set())


def _lease_for(fields: dict[str, str]) -> int:
    """Seconds we keep the offered slots for this request: MACH-1.4 R14-R15.

    Only for an asker that declares ``mach=1.3`` or later (it leases its own slots to us too),
    as long as it asks (default 900) and never longer than our own citizens get (_SHOWN_FOR).
    """
    # ponytail: no "three expired leases -> 120 s" (R15); the 900 s cap bounds a careless asker.
    # Count leases that end without confirm or release per partner if one starts hoarding.

    try:
        declared = tuple(int(part) for part in fields.get("mach", "").split(".")[:2])
    except ValueError:
        return 0
    if declared < (1, 3):
        return 0
    asked = fields.get("lease_s", "")
    return max(0, min(int(asked) if asked.isdigit() else 900, int(_SHOWN_FOR)))


def _ring_when(value: str, now: datetime) -> datetime | None:
    """A partner's ``after``/``deadline``: agenda time, read in the agenda's zone when it has none."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=now.tzinfo)


async def _answer_partner(city: City, message: Message, chat: list[Message]) -> None:
    """Another ASST asks our desk for slots: ONE message of offers at our facilities, or a cancel.

    The offers name the facility (``facility=``) that holds and commits, so the partner can
    confirm there directly; a confirm sent to us is forwarded (see _on_confirm).
    """

    agent = city.agent_id
    partner = message.sender
    fields = _partner_fields(message.text)
    procedure = fields.get("procedure", "")
    priority = fields.get("priority")
    request_id = fields.get("request_id") or f"rq-{message.msg_id}"
    token = f"request_id={request_id}"
    if any(row.sender == agent and row.recipient == partner and token in row.text.split()
           for row in chat):
        return
    if not procedure:
        await _say(
            city,
            partner,
            f"cancel {token} reason=missing_procedure Please name the visit with procedure= "
            "(VIS-FIS, VIS-CARD or CONS-GIN) and priority=.",
        )
        return
    query = f"world procedure={procedure} page=1"
    if not any(row.sender == agent and row.text.strip() == query for row in chat):
        await _say(city, LEDGER, query)
    if _ledger(chat, "agenda") is None and not _exact(chat, agent, "agenda"):
        await _say(city, LEDGER, "agenda")

    def ready(rows: list[Message]) -> bool:
        world = _latest_slots(rows)
        return world is not None and procedure in world and _ledger(rows, "agenda") is not None

    chat = await _until(city, message.need_id, chat, ready, _SHORT_TRIES * 2)  # partners want it fast
    world = _latest_slots(chat)
    agenda = _ledger(chat, "agenda")
    now = _when("" if agenda is None else agenda.get("agenda_now", ""))
    if world is None or now is None:
        await _say(city, partner, f"cancel {token} reason=facility_unavailable")
        return
    lines = [line for line in world.splitlines() if "slot_id=" in line]
    # MACH-1.3 R6: a slot one request is deciding on (a citizen of ours, another ASST's lease)
    # or already sent for a hold is not offered to a second one.
    clock = time.monotonic()
    mine = f"ring:{partner}|{request_id}"
    held = frozenset(_USED | {sid for sid, (at, who) in _SHOWN.items()
                              if who != mine and clock - at <= _SHOWN_FOR})
    lease = _lease_for(fields)
    criteria = {"now": now, "priority": priority, "procedure": procedure, "skip": held,
                "lang": fields.get("lang", "").lower(),
                "gender": fields.get("gender", fields.get("gender_preference", "")).lower()[:1]}
    slots = _desk_partner_slots(
        lines,
        days={_DAY_KEYS.get(part[:3].lower(), "") for part in re.split(r"[/,;|]+", fields.get("days", ""))} - {""},
        after=_ring_when(fields.get("after") or fields.get("earliest", ""), now),
        deadline=_ring_when(fields.get("deadline", ""), now),
        **criteria,
    )
    if not slots:
        # MACH-1.4 R16: the "no" says when, if the page we read shows a later free slot.
        later = _desk_partner_slots(lines, open_end=True, **criteria)
        hint = f" next_free={later[0]['at']}" if later else ""
        await _say(city, partner, f"cancel {token} reason=no_availability{hint}")
        return
    rows: list[str] = []
    for slot in slots:
        parts = [
            "offer",
            f"offer_id=p-{slot['slot_id']}",
            f"slot_id={slot['slot_id']}",
            f"doctor_id={slot['doctor_id']}",
            f"facility={slot['facility']}",
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
        if lease:
            parts += [f"lease_s={lease}", f"mach={_MACH}"]
        parts.append(token)
        line = " ".join(parts)
        if len("\n".join([*rows, line])) > _MAX_TEXT:
            break
        rows.append(line)
    if await _say(city, partner, "\n".join(rows)):
        _OFFERED_TO.update((partner, slot["slot_id"]) for slot in slots)
        if lease:
            for slot in slots[:len(rows)]:
                # Backdated so that _SHOWN_FOR after this mark is ``lease`` seconds from now.
                _SHOWN[slot["slot_id"]] = (clock - (_SHOWN_FOR - lease), mine)


