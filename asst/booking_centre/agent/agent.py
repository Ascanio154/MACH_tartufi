"""Care-protocol example. ``city init`` does not copy this file.

The scaffold brain is ``templates/agent/agent/agent.py``. This one is the full care
protocol (prescribe, offer, hold, commit, check-in, report), one inbound message per
step. The role is ``role`` in ``card.yaml``. Ledger writes are chat to ``city:ledger``;
citizens read the act word and ``key=value`` tokens (ADR-110).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path

from city_sdk.city import (
    LEDGER,
    CapError,
    City,
    Message,
    Need,
    OutOfScope,
    Peer,
    clinical,
    understood,
)

ROLES = ("booking_centre", "gp", "hospital", "specialist_care", "family_counseling")
_MAX_PAGE = 8
# Four agenda hours is about half a minute of real time at 480x, so a slot is still
# ahead when the citizen decides. One hour was already past by then.
_LEAD = timedelta(hours=4)
# Doctor key (the last segment of doctor_id) -> languages, gender, days. ``*`` is the ASST's
# rare language. The Ledger's slot line names the doctor; it does not repeat this table.
_DOCTORS: dict[str, tuple[frozenset[str], str, frozenset[str]]] = {
    "h1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed"})),
    "h2": (frozenset({"it"}), "m", frozenset({"wed", "thu", "fri"})),
    "h3": (frozenset({"it", "*"}), "m", frozenset({"tue", "thu"})),
    "s1": (frozenset({"it", "en"}), "m", frozenset({"mon", "wed", "fri"})),
    "s2": (frozenset({"it"}), "f", frozenset({"tue", "thu"})),
    "c1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed", "thu", "fri"})),
}
_PROTOCOLS: dict[str, tuple[str, str]] = {
    "SYM-BACK": ("VIS-FIS", "B"),
    "SYM-KNEE": ("VIS-FIS", "D"),
    "SYM-PALPITATIONS": ("VIS-CARD", "B"),
    "SYM-FOLLOWUP": ("VIS-CARD", "P"),
}
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("SYM-BACK", ("mal di schiena", "back pain")),
    ("SYM-KNEE", ("ginocchio", "knee")),
    ("SYM-PALPITATIONS", ("cuore che batte", "heart racing")),
    ("SYM-FOLLOWUP", ("controllo del cuore", "routine heart check")),
)
_RED_FLAGS = (
    "perdita di forza alle gambe",
    "loss of strength in the legs",
    "dolore al petto",
    "chest pain",
)
_DEADLINE_DAYS = {"B": 2, "D": 5}
_RX = re.compile(r"^prescription_number-[A-Z0-9-]{4,40}$")
_LOG = logging.getLogger("care")
# The Relay accepts a ledger reply and then the agent rejects the push, so the reply sits in
# the case history and never opens a turn. Wait briefly and read it back from history.
_PULL_TRIES = 24
_PULL_PAUSE = 0.25
# Needs whose sent offers this process remembers; the oldest is forgotten first.
_SENT_NEEDS = 4096
# Every slot goes in ONE offer message: the citizen chooses in that turn, and a later message
# does not enter a choice already made. A citizen message holds at most 2000 characters.
_MAX_OFFERS = 6
_MAX_TEXT = 2000
_OFFER_IT = "Ecco gli appuntamenti disponibili: mi indichi l'offer_id che preferisce."
_OFFER_EN = "Here are the available appointments: tell me the offer_id you prefer."
# Citizen ``days=`` in English or Italian, keyed by the first three letters.
_DAY_KEYS = {
    "mon": "mon", "lun": "mon",
    "tue": "tue", "mar": "tue",
    "wed": "wed", "mer": "wed",
    "thu": "thu", "gio": "thu",
    "fri": "fri", "ven": "fri",
}
_DAYS_RE = re.compile(r"\bdays=(\S+)", re.IGNORECASE)
_UNREACHABLE_IT = "La struttura non è raggiungibile in questo momento."
_UNREACHABLE_EN = "The facility can't be reached right now."
_NO_OTHER_IT = (
    "Al momento non ci sono altri posti disponibili: "
    "la struttura è temporaneamente non raggiungibile."
)
_NO_OTHER_EN = "No other slot is available right now: the facility is temporarily unreachable."
# Cross-ASST: the ring partner may have what our ASST lacks. The search carries only these
# criteria; no citizen data leaves before the cross_asst consent.
_PARTNER_CAPABILITY = {
    "VIS-FIS": "hospital.physiatry",
    "VIS-CARD": "specialist_care.cardiology",
    "CTRL-CARD": "specialist_care.cardiology",
}
_MAX_PARTNERS = 3
_PARTNER_ASK_IT = "Nella nostra ASST non ci sono posti adatti: sto chiedendo alle ASST vicine."
_PARTNER_ASK_EN = "No suitable slot in our ASST: I am asking the nearby ASSTs."
_PARTNER_LEAD_IT = "Questi posti sono in un'altra ASST della rete."
_PARTNER_LEAD_EN = "These appointments are at a partner ASST."
# Our ASST's rare language (``*`` in _DOCTORS), from the Mondo ASST page: asst-greco-h3.
_RARE_LANG = "si"
# A citizen speaking a rare language needs the partner whose h3 speaks it: ask more partners.
_MAX_PARTNERS_RARE = 6
_NO_PARTNER_IT = (
    "Mi dispiace: al momento non ci sono posti adatti né da noi né nelle ASST vicine. "
    "Se può indicarmi altri giorni o un'altra preferenza sul medico, riprovo subito."
)
_NO_PARTNER_EN = (
    "I'm sorry: right now there is no suitable slot in our ASST or the nearby ones. "
    "If you can tell me other days or another doctor preference, I will search again at once."
)
_CROSS_ASK_IT = (
    "Per prenotare in un'altra ASST mi serve il suo consenso a condividere con quella "
    "struttura i dati necessari."
)
_CROSS_ASK_EN = (
    "To book at another ASST I need your consent to share the necessary data with that facility."
)
_CROSS_NO_IT = "Va bene, non condivido i suoi dati con l'altra ASST."
_CROSS_NO_EN = "All right, I won't share your data with the other ASST."
# Rule P1: a prescription is valid for 2 agenda days after it is issued.
_RX_VALID_DAYS = 2
_RENEW_IT = (
    "La ricetta risulta scaduta: chieda il rinnovo al suo medico di base "
    "e poi mi mandi il nuovo prescription_number."
)
_RENEW_EN = (
    "Your referral has expired: please ask your GP to renew it, "
    "then send me the new prescription_number."
)


def role_of() -> str:
    """The role this process serves, from ``CITY_ROLE`` or the card next to the brain."""

    forced = os.environ.get("CITY_ROLE", "").strip()
    if forced in ROLES:
        return forced
    path = os.environ.get("CITY_CARD", "")
    if not path:
        return ""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    return _card_role(text)


async def main(need: Need, city: City) -> None:
    message = city.inbound
    if message is None:
        return
    text = message.text or need.summary
    if clinical(text):
        raise OutOfScope()
    case = await city.history(need_id=message.need_id)
    await handle(role_of(), city, message, case)


def _asks_address(message: Message) -> bool:
    """True when this turn is a ``data.request`` that names the field ``address``."""

    text = message.text
    if text:
        named = False
        asked = False
        for token in text.split():
            bare = token.strip(".,;:!?()\"'")
            if bare == "data.request":
                named = True
            key, eq, value = bare.partition("=")
            if eq and key == "fields":
                names = {part.strip() for part in value.split(",") if part.strip()}
                asked = asked or "address" in names
        return named and asked
    if message.type != "data.request":
        return False
    raw = message.body.get("fields")
    if isinstance(raw, list):
        return "address" in raw
    if isinstance(raw, str):
        return "address" in {part.strip() for part in raw.replace(",", " ").split() if part.strip()}
    return False


def _own_street(city: City) -> str:
    """This agent's door: the city value, else the street shipped beside the brain."""

    street = getattr(city, "address", "")
    if isinstance(street, str) and street.strip():
        return street.strip()
    env = os.environ.get("CITY_ADDRESS", "").strip()
    if env:
        return env
    path = Path(__file__).with_name("door.txt")
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


async def handle(role: str, city: City, message: Message, chat: list[Message]) -> None:
    street = _own_street(city)
    if street and _asks_address(message):
        await city.say(message.sender, f"address={street}")
        return
    if role == "gp":
        await _gp(city, message, chat)
    elif role == "booking_centre":
        await _desk(city, message, chat, hold_self=False, fixed=None)
    elif role == "hospital":
        await _facility(city, message, chat, reports=False, hold_self=False)
    elif role == "specialist_care":
        await _facility(city, message, chat, reports=True, hold_self=False)
    elif role == "family_counseling":
        await _family(city, message, chat)


# --- shared ------------------------------------------------------------------------------------


def _card_role(text: str) -> str:
    role = ""
    types: list[str] = []
    in_types = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line[0] in " \t":
            if in_types:
                item = line.strip().removeprefix("-").strip().strip("'\"")
                if item:
                    types.append(item)
            continue
        in_types = False
        key, _, value = line.partition(":")
        value = value.strip().strip("'\"")
        if key.strip() == "role":
            role = value
        elif (
            key.strip() in {"capabilities", "service_types"}
            and value.startswith("[")
            and value.endswith("]")
        ):
            types = [part.strip().strip("'\"") for part in value[1:-1].split(",") if part.strip()]
        elif key.strip() in {"capabilities", "service_types"}:
            in_types = True
    if role in ROLES:
        return role
    for family in ("specialist_care", "family_counseling", "hospital", "booking_centre", "gp"):
        if any(item == family or item.startswith(family + ".") for item in types):
            return family
    return "hospital" if role == "facility" else ""


def words(text: str) -> set[str]:
    return {token.strip(".,;:!?()\"'").lower() for token in text.split() if token.strip()}


def first_word(text: str) -> str:
    parts = text.split()
    return parts[0].strip(".,;:!?()\"'").lower() if parts else ""


def fields_of(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for token in text.replace(",", " ").split():
        key, eq, value = token.partition("=")
        if not eq or not key or not value:
            continue
        value = value.strip(".,;:!?")
        if key not in found and value:
            found[key] = value
    return found


# --- natural language (citizens and partner agents) -----------------------------------------------
# Citizens write in plain Italian or English, in any tone. Every reader below starts from the
# ``key=value`` tokens (they always win) and only fills a field the tokens leave out. Pure regex
# and word lists: no model call, so the 10 s fast reply stays safe.

_TEAM_AGENTS = frozenset(
    {
        "agent:h3n6kfd18r3g",  # booking_centre
        "agent:pxvd2kk2yx5j",  # family_counseling
        "agent:bmrcsmqq6at2",  # gp
        "agent:xtj1dk9x3qej",  # hospital
        "agent:q18es2d82f8r",  # specialist
    }
)
_RX_KEYED = re.compile(r"prescription_number[-_:\s]*([A-Za-z0-9][A-Za-z0-9-]{3,39})", re.IGNORECASE)
_RX_SAID = re.compile(
    r"\b(?:ricetta|impegnativa|prescrizione|prescription|rx)\b"
    r"(?:\s*(?:numero|number|codice|code|num\b\.?|nr\b\.?|n°|n\.|no\.|n\b|#|è|e'|is\b|:))*"
    r"\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9-]{3,39})",
    re.IGNORECASE,
)
_WEEKDAY_WORDS = {
    "lunedi": "mon", "monday": "mon",
    "martedi": "tue", "tuesday": "tue",
    "mercoledi": "wed", "wednesday": "wed",
    "giovedi": "thu", "thursday": "thu",
    "venerdi": "fri", "friday": "fri",
}
_NOT_WORDS = {"non", "not", "no", "tranne", "except", "escluso", "esclusi", "but", "niente", "never", "mai"}
_LANG_WORDS = {
    "inglese": "en", "english": "en",
    "italiano": "it", "italian": "it",
    "singalese": "si", "sinhala": "si", "sinhalese": "si", "cingalese": "si",
}
_EN_MARKERS = frozenset(
    "the i my please hello hi appointment have need book would like thanks thank you is "
    "want can could prescription doctor".split()
)
_IT_MARKERS = frozenset(
    "il la ho per vorrei buongiorno buonasera ciao grazie prenotare ricetta sono una un di "
    "che mi posso devo appuntamento medico salve".split()
)
_FEMALE = re.compile(r"\b(dottoressa|medico donna|dottore donna|una donna|female|woman|lady)\b")
_MALE = re.compile(r"\b(medico uomo|dottore uomo|un uomo|male doctor|a man|man doctor|male)\b")
_DISTRICT = re.compile(r"\b(?:distretto|district|zona|municipio|area)\s*(?:n\.?\s*)?(\d{1,2})\b")
_YES = re.compile(
    r"\b(si|sì|ok|okay|va bene|vabene|confermo|conferma|prenoto|prenota|prendo|scelgo|"
    r"perfetto|d'accordo|daccordo|accetto|acconsento|autorizzo|yes|yeah|sure|"
    r"confirm|book|take|choose|i'll take|fine|agree|consent|perfect)\b"
)
_NO = re.compile(
    r"\b(no|non acconsento|non autorizzo|non voglio|rifiuto|nego|nessuno|nessuna|"
    r"decline|refuse|don't|do not|none|neither)\b"
)
_ORDINALS = {
    "prima": 0, "primo": 0, "first": 0, "1st": 0, "uno": 0, "one": 0,
    "seconda": 1, "secondo": 1, "second": 1, "2nd": 1, "due": 1, "two": 1,
    "terza": 2, "terzo": 2, "third": 2, "3rd": 2, "tre": 2, "three": 2,
    "quarta": 3, "quarto": 3, "fourth": 3, "4th": 3, "quattro": 3, "four": 3,
    "quinta": 4, "quinto": 4, "fifth": 4, "5th": 4, "cinque": 4, "five": 4,
    "sesta": 5, "sesto": 5, "sixth": 5, "6th": 5, "sei": 5, "six": 5,
    "ultima": -1, "ultimo": -1, "last": -1,
}
_ORDINAL = re.compile(
    r"\b(?:la|il|l'|lo|the|option|opzione|offerta|offer|numero|number|n\.?|#)\s*"
    r"(prima|primo|first|seconda|secondo|second|terza|terzo|third|quarta|quarto|fourth|"
    r"quinta|quinto|fifth|sesta|sesto|sixth|ultima|ultimo|last|[1-6])\b"
    r"|\b(first|second|third|fourth|fifth|sixth|last|1st|2nd|3rd|4th|5th|6th)\s+(?:one|slot|option)\b"
)
# Clinical questions the booking centre must never answer (no diagnosis, no prescription).
_MEDICAL = re.compile(
    r"\b(cosa devo prendere|che farmaco|quale farmaco|medicin|farmac|antidolorific|"
    r"cosa ho|cos'ho|è grave|e' grave|is it serious|what should i take|which medicine|"
    r"painkiller|diagnos|terapia|therapy|treatment|prescrivi|prescribe me|"
    r"mi fai la ricetta|fammi la ricetta|give me a prescription|write me a prescription)"
)


def _plain(text: str) -> str:
    """Lower case without accents, apostrophes kept: ``lunedì`` -> ``lunedi``."""

    import unicodedata

    lowered = unicodedata.normalize("NFKD", text.lower().replace("’", "'"))
    return "".join(ch for ch in lowered if not unicodedata.combining(ch))


def _said_days(plain: str) -> tuple[set[str], set[str]]:
    """Weekdays named in free text: (wanted, excluded). ``non il lunedì`` excludes Monday."""

    tokens = re.findall(r"[a-z']+", plain)
    wanted: set[str] = set()
    excluded: set[str] = set()
    for index, token in enumerate(tokens):
        day = _WEEKDAY_WORDS.get(token.rstrip("'"))
        if day is None:
            continue
        before = set(tokens[max(0, index - 3) : index])
        (excluded if before & _NOT_WORDS else wanted).add(day)
    return wanted, excluded


def _citizen_fields(text: str) -> dict[str, str]:
    """``fields_of`` plus what a plain sentence says: prescription, language, days, gender, area."""

    found = fields_of(text)
    plain = _plain(text)
    if "prescription_number" not in found or not found["prescription_number"].startswith(
        "prescription_number-"
    ):
        raw = found.get("prescription_number", "")
        if raw:
            found["prescription_number"] = "prescription_number-" + raw.upper()
        else:
            hit = _RX_KEYED.search(text)
            value = hit.group(1) if hit else ""
            if not value:
                said = _RX_SAID.search(text)
                if said and any(ch.isdigit() for ch in said.group(1)):
                    value = said.group(1)
            if value:
                found["prescription_number"] = "prescription_number-" + value.strip("-").upper()
    if "lang" not in found:
        for word, code in _LANG_WORDS.items():
            if re.search(rf"\b{word}\b", plain):
                found["lang"] = code
                break
    if "days" not in found:
        wanted, excluded = _said_days(plain)
        if excluded and not wanted:
            wanted = {"mon", "tue", "wed", "thu", "fri"} - excluded
        if wanted:
            found["days"] = "/".join(sorted(wanted))
    if "gender" not in found and "gender_preference" not in found:
        if _FEMALE.search(plain):
            found["gender"] = "f"
        elif _MALE.search(plain):
            found["gender"] = "m"
    if "area.district" not in found:
        hit = _DISTRICT.search(plain)
        if hit:
            found["area.district"] = hit.group(1)
    return found


def _guess_lang(text: str) -> str:
    """``en`` when a sentence reads as English, ``it`` otherwise (the city default)."""

    tokens = set(re.findall(r"[a-z']+", _plain(text)))
    english = len(tokens & _EN_MARKERS)
    italian = len(tokens & _IT_MARKERS)
    return "en" if english >= 2 and english > italian else "it"


def _medical_question(text: str) -> bool:
    return _MEDICAL.search(_plain(text)) is not None


def _when(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _citizen(chat: list[Message]) -> str | None:
    for row in chat:
        if row.sender.startswith("citizen:"):
            return row.sender
    for row in chat:
        fields = fields_of(row.text)
        for key in ("patient", "citizen"):
            party = fields.get(key, "")
            if party.startswith("citizen:"):
                return party
    return None


def _we(chat: list[Message], agent: str, token: str, *, to: str, word: str) -> bool:
    for row in chat:
        if row.sender != agent or row.recipient != to:
            continue
        if token in row.text.split() and word in words(row.text):
            return True
    return False


def _exact(chat: list[Message], agent: str, text: str) -> bool:
    return any(row.sender == agent and row.text.strip() == text for row in chat)


async def _say(city: City, to: str, text: str) -> bool:
    try:
        await city.say(to, text)
    except CapError:
        return False
    _remember_sent(to, text)
    return True


def _remember_sent(to: str, text: str) -> None:
    """Mark the citizen as answered in this turn; keep the offers we just sent them."""

    if not to.startswith("citizen:"):
        return
    flag = _REPLIED.get()
    if flag is not None:
        flag[0] = True
    offers = _offers_in(text)
    if offers:
        _LAST_OFFERS[to] = offers
        while len(_LAST_OFFERS) > _SENT_NEEDS:
            _LAST_OFFERS.pop(next(iter(_LAST_OFFERS)))


async def _send(city: City, to: str, text: str) -> str | None:
    """Like ``_say``, but returns the refusal code (e.g. ``TARGET_NOT_VISIBLE``), None if sent."""

    try:
        await city.say(to, text)
    except CapError as error:
        return error.code or "CAP_ERROR"
    _remember_sent(to, text)
    return None


def _latest(chat: list[Message], head: str, token: str) -> Message | None:
    found: Message | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == head and token in row.text.split():
            found = row
    return found


async def _until(
    city: City,
    need_id: str | None,
    chat: list[Message],
    ready: Callable[[list[Message]], bool],
) -> list[Message]:
    """Re-read the case until ``ready`` or the short wait ends. The push may never arrive."""

    fresh = chat
    for attempt in range(_PULL_TRIES):
        try:
            fresh = await city.history(need_id=need_id)
        except CapError:
            fresh = chat
        if ready(fresh):
            return fresh
        if attempt + 1 < _PULL_TRIES:
            await asyncio.sleep(_PULL_PAUSE)
    return fresh


async def _await_line(
    city: City,
    need_id: str | None,
    chat: list[Message],
    head: str,
    token: str,
    *,
    op: str,
) -> tuple[Message, list[Message]] | None:
    """The ledger line for ``token``, or a refusal of ``op`` that was not already in ``chat``."""

    known = {row.msg_id for row in chat}
    fresh = chat
    for attempt in range(_PULL_TRIES):
        try:
            fresh = await city.history(need_id=need_id)
        except CapError:
            fresh = chat
        hit = _latest(fresh, head, token)
        if hit is not None:
            return hit, fresh
        for row in fresh:
            if row.msg_id in known or row.sender != LEDGER:
                continue
            if first_word(row.text) == "refused" and op in words(row.text):
                return row, fresh
        if attempt + 1 < _PULL_TRIES:
            await asyncio.sleep(_PULL_PAUSE)
    return None


def _has_agenda(chat: list[Message]) -> bool:
    agenda = _ledger(chat, "agenda")
    return _when("" if agenda is None else agenda.get("agenda_now", "")) is not None


def _has_procedure(chat: list[Message]) -> bool:
    reply = _ledger(chat, "prescription")
    return reply is not None and bool(reply.get("procedure"))


def _rx(need_id: str | None, msg_id: str) -> str:
    raw = (need_id or msg_id or "CASE").upper()
    suffix = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in raw).strip("-")
    if len(suffix) < 4:
        suffix = (suffix + "0000")[:4]
    number = "prescription_number-" + suffix[:40]
    return number if _RX.fullmatch(number) else "prescription_number-CASE"


def _speaks(doctor_id: str, lang: str) -> bool:
    spec = _DOCTORS.get(doctor_id.rsplit("-", 1)[-1])
    if spec is None:
        return False
    langs = spec[0]
    if lang in langs:
        return True
    # ``*`` is OUR rare language only: our h3 speaks Sinhala, not every non-it/en language.
    return "*" in langs and lang == _RARE_LANG


def fitting(
    lines: list[str],
    *,
    now: datetime,
    priority: str | None,
    lang: str,
    gender: str | None = None,
    facility: str | None = None,
    procedure: str | None = None,
) -> list[dict[str, str]]:
    """Slots a citizen can accept: future, free, in the priority window, spoken language."""

    if priority == "U":
        horizon: datetime | None = now.replace(hour=23, minute=59, second=59, microsecond=0)
    elif priority == "P":
        horizon = None
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
        reserve = slot.get("urgent_reserve", "").lower() == "true"
        if reserve and priority != "U":
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
    return False


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
    return False


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
    return found


def _district(chat: list[Message]) -> int | None:
    """The citizen's ``area.district``, if it is a number."""

    for row in chat:
        if row.sender.startswith("citizen:"):
            raw = _citizen_fields(row.text).get("area.district", "")
            if raw.isdigit():
                return int(raw)
    return None


async def _partner_peers(
    city: City, procedure: str, ours: set[str], district: int | None
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
    parts.append(token)
    sent_now = False
    limit = _MAX_PARTNERS if _lang(chat) in {"it", "en"} else _MAX_PARTNERS_RARE
    for peer in (await _partner_peers(city, procedure, ours, _district(chat)))[:limit]:
        if await _send(city, peer.agent_id, " ".join(parts)) is None:
            entry.offers.add(f"partner:{peer.agent_id}")
            sent_now = True
    if not sent_now:
        return bool(asked - ({avoid} if avoid else set()))
    notice = _PARTNER_ASK_IT if _lang(chat) == "it" else _PARTNER_ASK_EN
    if "partner-notice" not in entry.offers and not _exact(chat, agent, notice):
        if await _say(city, citizen, notice):
            entry.offers.add("partner-notice")
    return True


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


def _offer_block(
    slots: list[dict[str, str]], number: str | None, priority: str | None, lang: str
) -> str:
    """One message, one ``offer`` line per slot, kept under the citizen text limit."""

    sentence = _OFFER_IT if lang == "it" else _OFFER_EN
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
    if fixed is not None or not message.sender.startswith("citizen:"):
        await _desk_turn(city, message, chat, hold_self=hold_self, fixed=fixed)
        return
    # A citizen always gets an answer in the same turn: the review marks a message left
    # without reply as passive (-1). _say flags every message that reaches the citizen.
    flag = _REPLIED.set([False])
    try:
        await _desk_turn(city, message, chat, hold_self=hold_self, fixed=fixed)
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
    if agent_turn and fixed is None and _partner_request(message):
        await _answer_partner(city, message, chat)
        return
    if "confirm" in kind and (citizen_turn or agent_turn):
        await _on_confirm(city, message, chat, hold_self=hold_self)
        return
    if "booking.receipt" in kind and agent_turn:
        await _forward_receipt(city, message, chat)
        return
    if "cancel" in kind and agent_turn:
        await _partner_cancel(city, message, chat, fixed=fixed)
        return
    if "offer" in kind and agent_turn:
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
# Need -> the agent (another ASST's booking centre) whose confirm we forwarded.
_CONFIRMED_BY: dict[str, str] = {}
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
    r"intorpidit|insensibil|strength|weak|numb|can'?t (feel|move)|cannot (feel|move)|paraly)"
)
_RED_FLAG_RE = tuple(
    re.compile(rf"{a}.{{0,40}}{b}|{b}.{{0,40}}{a}", re.DOTALL)
    for a, b in ((_CHEST, _CHEST_BAD), (_LEGS, _LEGS_BAD))
) + (re.compile(r"\binfarto\b|heart attack|\bictus\b|\bstroke\b|non riesco a respirare|can'?t breathe"),)

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
        "Sto verificando la ricetta e i posti liberi: le mando le proposte appena pronte.",
        "I am checking your referral and the free slots: I will send you the options shortly.",
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
    "receipt": (
        "Prenotazione confermata. Il giorno della visita si presenti alla struttura con questo "
        "booking_id per registrare l'arrivo.",
        "Booking confirmed. On the day of the visit go to the facility with this booking_id "
        "to register your arrival.",
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
    text = english if lang == "en" else italian
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
    return citizen in _BOOKED or any(
        row.sender == agent and row.recipient in ("", citizen)
        and first_word(row.text) == "booking.receipt"
        for row in chat
    )


def _open_offers(chat: list[Message], agent: str, citizen: str) -> list[dict[str, str]]:
    """The offers the citizen can still pick: our latest offer message, minus cancelled ones."""

    if not citizen or citizen in _PENDING:
        return []
    latest: list[dict[str, str]] = []
    cancelled: set[str] = set()
    booked = False
    for row in chat:
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
    return [o for o in pool if o.get("offer_id") not in cancelled and o.get("offer_id")]


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


async def _no_silence(city: City, message: Message, chat: list[Message]) -> None:
    """Nothing reached the citizen this turn: tell them where their booking stands."""

    citizen = message.sender
    try:
        fresh = await city.history(need_id=message.need_id)
    except CapError:
        fresh = chat
    known = {row.msg_id for row in fresh}
    fresh = list(fresh) + [row for row in chat if row.msg_id not in known]
    agent = city.agent_id
    lang = _lang(fresh)
    key = message.need_id or message.msg_id
    number = _number(fresh)
    if _booked(fresh, agent, citizen):
        name = "booked"
    elif citizen in _PENDING:
        name = "pending"
    elif _open_offers(fresh, agent, citizen):
        name = "offers_open"
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
    await _say(city, citizen, _phrase(name, lang))


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
    lines: list[str], *, now: datetime, priority: str | None, procedure: str
) -> list[dict[str, str]]:
    """Free slots of any of our facilities inside the priority window, spread, at most six."""

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
        if not slot.get("slot_id") or not slot.get("doctor_id") or not slot.get("facility"):
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
        timed.append((at, slot))
    timed.sort(key=lambda item: (item[0], item[1]["slot_id"]))
    return _pick([slot for _, slot in timed], set())


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

    chat = await _until(city, message.need_id, chat, ready)
    world = _latest_slots(chat)
    agenda = _ledger(chat, "agenda")
    now = _when("" if agenda is None else agenda.get("agenda_now", ""))
    if world is None or now is None:
        await _say(city, partner, f"cancel {token} reason=ledger_unavailable")
        return
    slots = _desk_partner_slots(
        [line for line in world.splitlines() if "slot_id=" in line],
        now=now,
        priority=priority,
        procedure=procedure,
    )
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
        parts.append(token)
        line = " ".join(parts)
        if len("\n".join([*rows, line])) > _MAX_TEXT:
            break
        rows.append(line)
    await _say(city, partner, "\n".join(rows))


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
                                   facility=facility, number=number)
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
    if facility not in _own_facilities(chat) and not offer_id.startswith("of-"):
        # A partner ASST's slot: no citizen data leaves before the cross_asst consent (P2).
        ref = _cross_consent_ref(chat, citizen)
        if not ref:
            await _ask_cross_consent(city, chat, agent, citizen, offer_id)
            return
        consent = ref
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
) -> None:
    """Send the citizen's choice to the facility that holds the slot; tell them if it fails."""

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


async def _partner_cancel(
    city: City, message: Message, chat: list[Message], *, fixed: str | None
) -> None:
    fields = fields_of(message.text)
    offer_id = fields.get("offer_id", "")
    citizen = _citizen(chat)
    key = message.need_id or message.msg_id
    partner = _CONFIRMED_BY.get(key, "")
    if partner and message.sender != partner and offer_id:
        # Our facility refused a slot another ASST confirmed through us: tell that ASST.
        reason = fields.get("reason", "facility_cancelled")
        await _say(city, partner, f"cancel offer_id={offer_id} reason={reason}")
        return
    if citizen and offer_id and _PENDING.get(citizen) == offer_id:
        _PENDING.pop(citizen, None)
    if not offer_id and fields.get("request_id") == f"x-{key}":
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
    expired = "EXPIR" in fields.get("reason", "").upper()
    reason = "prescription_expired" if expired else "facility_cancelled"
    if (
        offer_id
        and citizen
        and not _we(chat, city.agent_id, f"offer_id={offer_id}", to=citizen, word="cancel")
    ):
        await _say(city, citizen, f"cancel offer_id={offer_id} reason={reason}")
    if expired:
        number = _our_offer(chat, city.agent_id, offer_id).get("prescription_number") or _number(chat)
        if citizen and number:
            key = message.need_id or message.msg_id
            await _ask_renewal(city, citizen, number, _lang(chat), chat, city.agent_id, key)
        return
    await _advance(city, message, chat, fixed=fixed, replacing=True)


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
