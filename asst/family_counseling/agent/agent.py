"""Care-protocol example. ``city init`` does not copy this file.

The scaffold brain is ``templates/agent/agent/agent.py``. This one is the full care
protocol (prescribe, offer, hold, commit, check-in, report), one inbound message per
step. The role is ``role`` in ``card.yaml``. Ledger writes are chat to ``city:ledger``;
citizens read the act word and ``key=value`` tokens (ADR-110).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from city_sdk.city import LEDGER, CapError, City, Message, Need, OutOfScope, clinical, understood

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
# Level 8: is_emergency may be false and the alarm only in the free text. Same patterns as the
# GP and the booking centre: a body part near a warning word, plus breathing trouble and fainting.
_CHEST = r"\b(petto|torace|sterno|chest)\b"
_CHEST_BAD = (
    r"\b(dolor|male?\b|peso\b|pesante|oppress|opprim|stringe|stretta|schiacci|brucia|"
    r"fitt[ae]\b|morsa|costri|pain|hurt|ach(e|es|ing)\b|tight|pressure|crush|heavy|squeez)"
)
_LEGS = r"\b(gamb[ae]|legs?)\b"
_LEGS_BAD = (
    r"\b(forza|debol|non (le )?sento|non riesco a muover|non si muov|cedono|paralizz|"
    r"intorpidit|addormentat|insensibil|"
    r"strength|weak|numb|can'?t (feel|move)|cannot (feel|move)|giving way|give way|paraly)"
)
_RED_FLAG_RE = tuple(
    re.compile(rf"{a}.{{0,40}}{b}|{b}.{{0,40}}{a}", re.DOTALL)
    for a, b in ((_CHEST, _CHEST_BAD), (_LEGS, _LEGS_BAD))
) + (re.compile(r"\binfarto\b|heart attack|\bictus\b|\bstroke\b|"
               r"non riesco a respirare|fatico a respirare|fatica a respirare|manca il respiro|"
               r"can'?t breathe|cannot breathe|sto per svenire|sono svenut|\bsvenut[oa]\b|"
               r"perso conoscenza|passed out|\bfainted\b"),)
# No act word in these sentences: understood() reads the last act word of a text as the act.
_EMERGENCY_IT = ("Questa è un'emergenza: chiami subito il 112 o vada al pronto soccorso più vicino. "
                 "Non fisso un appuntamento al consultorio.")
_EMERGENCY_EN = ("This is an emergency: call 112 now or go to the nearest emergency room. "
                 "I am not booking a consultorio appointment.")


def _emergency_line(message: Message) -> str:
    """The escalation with a human sentence in the citizen's language (lang= token first)."""
    lang = fields_of(message.text).get("lang", "").lower() or _fc_guess_lang(message.text)
    sentence = _EMERGENCY_EN if lang == "en" else _EMERGENCY_IT
    return f"escalate.emergency reason=red_flag {sentence}"
_DEADLINE_DAYS = {"B": 2, "D": 5}
_RX = re.compile(r"^prescription_number-[A-Z0-9-]{4,40}$")
_LOG = logging.getLogger("care")
# The Relay accepts a ledger reply and then the agent rejects the push, so the reply sits in
# the case history and never opens a turn. Wait briefly and read it back from history.
_PULL_TRIES = 3  # Leo: few history reads per turn, or renew/ack hit 422 and the message comes back
_PULL_PAUSE = 1.0
_PARTNER_TRIES = 4  # partner replies are slower: one window of about 3 s
# Needs whose sent offers this process remembers; the oldest is forgotten first.
_SENT_NEEDS = 4096


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


_ACK_AFTER = 4.0
_PARTNER_ACK = "Ricevuto, verifico la disponibilità del consultorio e vi rispondo in un solo messaggio."
# Check-in: welcome, what happens now and next. No clinical content, no other patient's data.
_FAMILY_WELCOME_IT = (
    "Benvenuta/o al consultorio: il tuo arrivo è registrato, grazie. Accomodati in sala "
    "d'attesa, ti chiamiamo noi. Se hai esigenze di accessibilità o preferisci un'operatrice, "
    "dillo pure allo sportello. Dopo il colloquio, se ti serve un altro appuntamento, "
    "scrivimi: per il consultorio non serve la ricetta."
)
_FAMILY_WELCOME_EN = (
    "Welcome to the family counselling centre: your arrival is registered, thank you. Please "
    "take a seat in the waiting room, we will call you. If you have accessibility needs or "
    "prefer a female staff member, just tell the desk. After the meeting, if you need another "
    "appointment, write to me: no referral is needed here."
)


async def main(need: Need, city: City) -> None:
    message = city.inbound
    if message is None:
        return

    text = message.text or need.summary
    role = role_of()

    if (
        role == "family_counseling"
        and message.sender.startswith("citizen:")
        and (
            getattr(need, "is_emergency", False) is True
            or _emergency(message)
        )
    ):
        await _say(city, message.sender, _emergency_line(message))
        return

    if clinical(text):
        raise OutOfScope()

    if role == "family_counseling":
        street = _own_street(city)
        if street and _asks_address(message):
            await _say(city, message.sender, f"address={street}")
            return
        await _family(city, message, [])
        return

    case = await city.history(need_id=message.need_id)
    await handle(role, city, message, case)


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

def _offer_rows(text: str) -> list[dict[str, str]]:
    if first_word(text) != "offer":
        return []

    blocks = re.split(r"(?<!\S)offer_id=", text)[1:]
    return [fields_of("offer_id=" + block) for block in blocks]


# --- natural language (level 3: citizens write free text) ---------------------------------------
# Tokens always win; these only fill what a plain sentence says. Regex only, no model call.

_FC_ARRIVED = re.compile(
    r"\b(sono arrivat[oa]|sono qui|sono in (sala|accettazione|struttura|reception)|"
    r"sono presente|eccomi|sono all'ingresso|registro (il mio )?arrivo|check-?in|"
    r"i'?m here|i am here|i have arrived|i'?ve arrived|just arrived|i'?m at the)"
)
_FC_DAYS = {
    "lunedi": "mon", "monday": "mon", "martedi": "tue", "tuesday": "tue",
    "mercoledi": "wed", "wednesday": "wed", "giovedi": "thu", "thursday": "thu",
    "venerdi": "fri", "friday": "fri",
}
_FC_NOT = {"non", "not", "no", "tranne", "except", "escluso", "but", "mai", "never"}
_FC_LANGS = {"inglese": "en", "english": "en", "italiano": "it", "italian": "it"}
_FC_EN = frozenset("the i my please hello hi appointment have need book would like thanks "
                   "thank you is want can could".split())
_FC_IT = frozenset("il la ho per vorrei buongiorno buonasera ciao grazie prenotare sono una un "
                   "di che mi posso devo appuntamento salve visita".split())
_FC_FEMALE = re.compile(r"\b(dottoressa|ginecologa|medico donna|una donna|female|woman|lady)\b")
_FC_MALE = re.compile(r"\b(ginecologo uomo|medico uomo|dottore uomo|un uomo|male doctor|a man)\b")
_FC_YES = re.compile(
    r"\b(si|sì|ok|okay|va bene|vabene|confermo|prenoto|prendo|scelgo|perfetto|d'accordo|"
    r"accetto|yes|yeah|sure|confirm|book|take|choose|fine|perfect)\b"
)
_FC_NO = re.compile(r"\b(no|nessuno|nessuna|non voglio|rifiuto|none|neither|don't|do not)\b")
_FC_ORDINALS = {
    "prima": 0, "primo": 0, "first": 0, "1st": 0, "seconda": 1, "secondo": 1, "second": 1,
    "2nd": 1, "terza": 2, "terzo": 2, "third": 2, "3rd": 2, "quarta": 3, "quarto": 3,
    "fourth": 3, "4th": 3, "quinta": 4, "quinto": 4, "fifth": 4, "5th": 4, "sesta": 5,
    "sesto": 5, "sixth": 5, "6th": 5, "ultima": -1, "ultimo": -1, "last": -1,
}
_FC_ORDINAL = re.compile(
    r"\b(?:la|il|l'|lo|the|option|opzione|offerta|numero|number|n\.?|#)\s*"
    r"(prima|primo|first|seconda|secondo|second|terza|terzo|third|quarta|quarto|fourth|"
    r"quinta|quinto|fifth|sesta|sesto|sixth|ultima|ultimo|last|[1-6])\b"
    r"|\b(first|second|third|fourth|fifth|sixth|last|1st|2nd|3rd|4th|5th|6th)\s+(?:one|slot|option)\b"
)


def _fc_plain(text: str) -> str:
    lowered = unicodedata.normalize("NFKD", text.lower().replace("\u2019", "'"))
    return "".join(ch for ch in lowered if not unicodedata.combining(ch))


def _fc_said_days(plain: str) -> tuple[set[str], set[str]]:
    tokens = re.findall(r"[a-z']+", plain)
    wanted: set[str] = set()
    excluded: set[str] = set()
    for index, token in enumerate(tokens):
        day = _FC_DAYS.get(token)
        if day:
            before = set(tokens[max(0, index - 3):index])
            (excluded if before & _FC_NOT else wanted).add(day)
    return wanted, excluded


def _fc_plain_prefs(text: str) -> dict[str, str]:
    """Language, days and doctor gender a plain sentence states."""
    plain = _fc_plain(text)
    found: dict[str, str] = {}
    for word, code in _FC_LANGS.items():
        if re.search(rf"\b{word}\b", plain):
            found["lang"] = code
            break
    wanted, excluded = _fc_said_days(plain)
    if excluded and not wanted:
        wanted = {"mon", "tue", "wed", "thu", "fri"} - excluded
    if wanted:
        found["days"] = "/".join(sorted(wanted))
    if _FC_FEMALE.search(plain):
        found["gender"] = "f"
    elif _FC_MALE.search(plain):
        found["gender"] = "m"
    return found


def _fc_guess_lang(text: str) -> str:
    tokens = set(re.findall(r"[a-z']+", _fc_plain(text)))
    english, italian = len(tokens & _FC_EN), len(tokens & _FC_IT)
    return "en" if english >= 2 and english > italian else "it"


def _fc_arrived(text: str) -> bool:
    return _FC_ARRIVED.search(_fc_plain(text)) is not None


def _fc_choice(text: str, offers: list[dict[str, str]]) -> dict[str, str] | None:
    """The offer a plain sentence picks ("la seconda", "ok mercoledì alle 10"), else None."""
    if not offers:
        return None
    tokens = {token.strip(".,;:!?()\"'") for token in text.replace("=", " ").split()}
    for offer in offers:
        if offer.get("offer_id") in tokens or (offer.get("slot_id") and offer.get("slot_id") in tokens):
            return offer
    plain = _fc_plain(text)
    hit = _FC_ORDINAL.search(plain)
    if hit:
        word = hit.group(1) or hit.group(2)
        index = int(word) - 1 if word.isdigit() else _FC_ORDINALS.get(word, 99)
        if -1 <= index < len(offers):
            return offers[index]
    yes = _FC_YES.search(plain) is not None
    if _FC_NO.search(plain) and not yes:
        return None
    wanted, _ = _fc_said_days(plain)
    clock = (re.search(r"\b(?:alle|ore|at|h)\s*(\d{1,2})(?:[:.](\d{2}))?\b", plain)
             or re.search(r"\b(\d{1,2})[:.](\d{2})\b", plain))
    if wanted or clock:
        picks = []
        for offer in offers:
            at = _when(offer.get("agenda_date", offer.get("at", "")))
            day = (offer.get("agenda_day") or "").lower()[:3]
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
    explanations = {
        "offer": (
            "Ti aiuto a trovare un appuntamento. Per il consultorio "
            "non serve ricetta: scegli tra queste disponibilità.",
            "I can help you find an appointment. No prescription is "
            "needed for family counseling: choose one of these options.",
        ),
        "booking.receipt": (
            "La prenotazione è confermata. Conserva questi riferimenti "
            "per registrare il tuo arrivo.",
            "Your appointment is booked. Keep these references "
            "for your arrival.",
        ),
        "checkin": (
            "Il tuo arrivo è stato registrato. Grazie.",
            "Your arrival has been registered. Thank you.",
        ),
        "cancel": (
            "C'è un problema con questo appuntamento. "
            "Verifico le altre disponibilità.",
            "There is a problem with this appointment. "
            "I will check the other available options.",
        ),
        "escalate.emergency": (
            "La situazione richiede assistenza urgente. Chiama il 112.",
            "This needs urgent assistance. Call 112 now.",
        ),
    }

    head = first_word(text)
    if to.startswith("citizen:") and head in explanations:
        inbound = city.inbound
        rows = [inbound] if inbound is not None else []

        if (
            head != "escalate.emergency"
            and inbound is not None
            and inbound.need_id
        ):
            try:
                rows = await city.history(need_id=inbound.need_id)
            except CapError:
                pass

        english = _lang(rows) == "en"
        explanation = explanations[head][1 if english else 0]

        if head == "offer" and fields_of(text).get("waiting_list") == "true":
            explanation = (
                "This is a waiting-list proposal; no appointment is booked yet."
                if english else
                "Questa proposta riguarda la lista d'attesa; "
                "l'appuntamento non è ancora fissato."
            )

        if len(text) + len(explanation) + 1 <= 2000:
            text = text + "\n" + explanation

    try:
        await city.say(to, text)
    except CapError as err:
        _LOG.info(
            "care.say.failed",
            extra={"code": getattr(err, "code", "UNKNOWN")},
        )
        return False
    return True


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
    return "*" in langs and lang not in {"it", "en"}


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


# --- the rest of the brain ----------------------------------------------------------------------
# City Hall refuses a brain file of thousands of lines, so the code lives in the files below,
# next to this one. Each runs in THIS module's namespace, in order: one brain, as before.
_PARTS = ('family_part1.py', 'family_part2.py', 'family_part3.py', 'family_part4.py')


def _load_parts() -> None:
    folder = Path(__file__).resolve().parent
    for name in _PARTS:
        source = folder / name
        exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), globals())


_load_parts()
