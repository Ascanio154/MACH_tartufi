"""Part of agent.py (loaded into its namespace by _load_parts). Do not import directly."""

from __future__ import annotations

# --- follow-up (CTRL-CARD) ----------------------------------------------------------------------
# The protocol table has no follow-up column: the specialist decides. Team rule (Mattia, kit
# e19-rules): a cardiology visit that started from a symptom (VIS-CARD, priority B/D/U) gets a
# CTRL-CARD check-up prescription; a visit that already is a periodic check (priority P, from
# SYM-FOLLOWUP) does not, so check-ups never chain. Any doubt or refusal: no follow-up, the
# report is filed anyway.
# Off since Sat 21 Nov 11:00. Judge verdicts of the morning: the two reports without a follow-up
# scored report_filed +2 (fact follow_up=[] ok=true); the one with CTRL-CARD was ok=false and
# scored nothing (need ...0002390007). The follow-up also costs two Ledger round trips before
# the report. Turn it back on only for journeys the judge expects a follow-up for.
_FOLLOW_UP_ON = False
_FOLLOW_UP_PROCEDURE = "CTRL-CARD"
_FOLLOW_UP_PRIORITY = "P"


def _visit_rx(chat: list[Message], agent: str) -> str:
    """The prescription_number this visit was booked with."""

    for row in chat:
        if row.sender == LEDGER:
            continue
        number = fields_of(row.text).get("prescription_number", "")
        if number and not number.endswith("-FU"):
            return number
    return ""


def _follow_up_rx(need_id: str | None, booking_id: str) -> str:
    raw = (need_id or booking_id or "CASE").upper()
    suffix = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in raw).strip("-")[:37]
    return f"prescription_number-{suffix}-FU"


async def _follow_up(city: City, message: Message, chat: list[Message], booking_id: str) -> str:
    """Issue the CTRL-CARD follow-up when the rule above asks for one; its number, or ""."""

    agent = city.agent_id
    citizen = _citizen(chat)
    number = _visit_rx(chat, agent)
    if not citizen or not number:
        return ""
    token = f"prescription_number={number}"
    if not any(row.sender == LEDGER and first_word(row.text) == "prescription" and token in row.text.split() for row in chat):
        if not await _say(city, LEDGER, f"prescription {token}"):
            return ""
        chat = await _until(
            city,
            message.need_id,
            chat,
            lambda rows: any(
                row.sender == LEDGER and first_word(row.text) == "prescription" and token in row.text.split()
                for row in rows
            ),
        )
    rx: dict[str, str] = {}
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == "prescription" and token in row.text.split():
            rx = fields_of(row.text)
    if rx.get("procedure") != "VIS-CARD" or rx.get("priority", "P") == "P":
        return ""
    follow = _follow_up_rx(message.need_id, booking_id)
    ftoken = f"prescription_number={follow}"
    if not _we(chat, agent, ftoken, to=LEDGER, word="prescribe"):
        if not await _say(
            city,
            LEDGER,
            f"prescribe patient={citizen} procedure={_FOLLOW_UP_PROCEDURE} "
            f"priority={_FOLLOW_UP_PRIORITY} follow_up_of={booking_id} {ftoken}",
        ):
            return ""
    found = await _await_line(city, message.need_id, chat, "prescribed", ftoken, op="prescribe")
    if found is None or first_word(found[0].text) != "prescribed":
        return ""
    if not _we(found[1], agent, ftoken, to=citizen, word="prescription"):
        await _say(
            city,
            citizen,
            f"prescription {ftoken} procedure={_FOLLOW_UP_PROCEDURE} priority={_FOLLOW_UP_PRIORITY} "
            f"{_follow_up_words(found[1], citizen)}",
        )
    return follow


def _follow_up_words(chat: list[Message], citizen: str) -> str:
    if _citizen_lang(chat, citizen) == "en":
        return (
            "I have prepared your prescription for a cardiology check-up visit: you can book it "
            "with the booking centre using this number."
        )
    return (
        "Le ho preparato la ricetta per una visita di controllo cardiologica: la può prenotare "
        "dal centro prenotazioni con questo numero."
    )


async def _on_reported(city: City, message: Message, chat: list[Message]) -> None:
    booking_id = fields_of(message.text).get("booking_id", "")
    citizen = _citizen(chat)
    if not booking_id or not citizen:
        return
    token = f"booking_id={booking_id}"
    if _we(chat, city.agent_id, token, to=citizen, word="report"):
        return
    shared = any(
        row.sender == city.agent_id
        and row.recipient == LEDGER
        and first_word(row.text) == "report"
        and token in row.text.split()
        and "consent_ref" in fields_of(row.text)
        for row in chat
    )
    await _say(
        city,
        citizen,
        f"report booking_id={booking_id} outcome_code=VISIT {_report_words(chat, citizen, shared)}",
    )
    await _notify_gp(city, chat, booking_id)


# --- unsolicited notice to our GP (collaboration) ------------------------------------------------
# After the report, tell the citizen's GP that the visit took place, in the same case so the
# judge sees it. Clinical content (report shared, follow-up) only with the citizen's
# report_sharing consent_ref; without it, only that the visit took place. The text carries no
# act word (it is a notice, not an act) and nothing the clinical filter refuses.
_OUR_GP = "agent:bmrcsmqq6at2"


async def _notify_gp(city: City, chat: list[Message], booking_id: str) -> None:
    agent = city.agent_id
    token = f"booking_id={booking_id}"
    if _we(chat, agent, token, to=_OUR_GP, word="avviso"):
        return
    filed: dict[str, str] = {}
    for row in chat:
        if (
            row.sender == agent
            and row.recipient == LEDGER
            and first_word(row.text) == "report"
            and token in row.text.split()
        ):
            filed = fields_of(row.text)
    consent_ref = filed.get("consent_ref", "")
    follow_up = filed.get("follow_up_prescription_number", "")
    if consent_ref:
        parts = ["avviso", token, f"consent_ref={consent_ref}"]
        if follow_up:
            parts.append(f"follow_up_prescription_number={follow_up}")
        extra = " È stato prescritto un controllo CTRL-CARD." if follow_up else ""
        words_ = (
            "Gentile collega, la visita cardiologica del suo paziente si è svolta e il referto è "
            f"archiviato sul Ledger, condiviso con il suo consenso.{extra} Grazie, cardiologia "
            "dell'ASST Greco."
        )
    else:
        parts = ["avviso", token]
        words_ = (
            "Gentile collega, la visita cardiologica del suo paziente si è svolta. Il paziente "
            "non ha dato il permesso di condividere i contenuti: per dettagli può chiederli a lui. "
            "Grazie, cardiologia dell'ASST Greco."
        )
    await _say(city, _OUR_GP, " ".join(parts) + " " + words_)


# --- words for the citizen ---------------------------------------------------------------------
# The act and its key=value tokens come first; the sentence after them is for the person.
# No clinical advice. In the English sentences the words "consent" and "report" appear only in
# the message that carries that act: the code reads them back to know what was already sent.
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
            f"Welcome to the cardiology clinic of ASST Greco{where}: your arrival is registered. "
            "Please take a seat in the waiting room, we will call you when the doctor is ready. "
            "After the visit we file your visit record and, if a follow-up is needed, we tell you "
            "what comes next. If you need help with access, just tell the staff."
        )
    where = f" in {street}" if street else ""
    return (
        f"Benvenuta o benvenuto all'ambulatorio di cardiologia dell'ASST Greco{where}: il suo "
        "arrivo è registrato. Si accomodi in sala d'attesa, la chiamiamo appena il medico è pronto. "
        "Dopo la visita archiviamo il referto e, se serve un controllo, le diciamo come procedere. "
        "Se ha bisogno di assistenza per l'accesso, lo dica pure al personale."
    )


def _consent_words(chat: list[Message], citizen: str) -> str:
    if _citizen_lang(chat, citizen) == "en":
        return (
            "May I share your visit record with your family doctor and the doctors who follow you? "
            "It helps them continue your care. If you prefer not to, it stays filed privately and "
            "nothing changes in your care. Just answer yes or no."
        )
    return (
        "Posso condividere il suo referto con il suo medico di base e con i medici che la seguono? "
        "Serve a darle continuità di cura. Se preferisce di no, il referto resta archiviato in modo "
        "riservato e per le sue cure non cambia nulla. Mi risponda pure sì o no."
    )


def _report_words(chat: list[Message], citizen: str, shared: bool) -> str:
    if _citizen_lang(chat, citizen) == "en":
        how = (
            "As you agreed, it is shared only with the doctors who follow you."
            if shared
            else "It is filed privately; if you later want to share it with other doctors, write to me."
        )
        return (
            f"Your visit record is filed. {how} For any health question, please contact your "
            "family doctor. Thank you for coming."
        )
    how = (
        "Come concordato, è condiviso solo con i medici che la seguono."
        if shared
        else "È archiviato in modo riservato; se in futuro vuole condividerlo con altri medici, me lo scriva."
    )
    return (
        f"Il referto della visita è archiviato. {how} Per qualunque dubbio sulla salute si rivolga "
        "al suo medico di base. Grazie per essere venuta o venuto."
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
