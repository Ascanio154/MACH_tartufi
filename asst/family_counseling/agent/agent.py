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
_DEADLINE_DAYS = {"B": 2, "D": 5}
_RX = re.compile(r"^prescription_number-[A-Z0-9-]{4,40}$")
_LOG = logging.getLogger("care")
# The Relay accepts a ledger reply and then the agent rejects the push, so the reply sits in
# the case history and never opens a turn. Wait briefly and read it back from history.
_PULL_TRIES = 24
_PULL_PAUSE = 0.25
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
        await _say(
            city,
            message.sender,
            "escalate.emergency reason=red_flag",
        )
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


# --- medico di base ----------------------------------------------------------------------------


def _emergency(message: Message) -> bool:
    if understood(message).body.get("is_emergency") is True:
        return True
    lowered = message.text.lower()
    return any(flag in lowered for flag in _RED_FLAGS)


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


def _offered(chat: list[Message], agent: str) -> set[str]:
    found: set[str] = set()
    for row in chat:
        if row.sender != agent:
            continue
        for offer in _offer_rows(row.text):
            slot = offer.get("slot_id")
            if slot:
                found.add(slot)
    return found


def _withdrawn(chat: list[Message], agent: str) -> set[str]:
    by_offer: dict[str, str] = {}
    for row in chat:
        if row.sender != agent:
            continue
        for offer in _offer_rows(row.text):
            offer_id = offer.get("offer_id")
            slot = offer.get("slot_id")
            if offer_id and slot:
                by_offer[offer_id] = slot

    dropped: set[str] = set()
    for row in chat:
        if first_word(row.text) != "cancel":
            continue
        offer_id = fields_of(row.text).get("offer_id", "")
        slot = by_offer.get(offer_id)
        if slot:
            dropped.add(slot)
    return dropped


def _number(chat: list[Message]) -> str | None:
    for row in reversed(chat):
        if row.sender.startswith("citizen:"):
            number = fields_of(row.text).get("prescription_number")
            if number:
                return number
    return None


def _lang(chat: list[Message]) -> str:
    for row in chat:
        if row.sender.startswith("citizen:"):
            lang = fields_of(row.text).get("lang", "")
            if lang:
                return lang
    return "it"


def _gender(chat: list[Message]) -> str | None:
    for row in chat:
        if not row.sender.startswith("citizen:"):
            continue
        raw = fields_of(row.text).get("gender_preference") or fields_of(row.text).get("gender")
        if raw in {"m", "f"}:
            return raw
    return None


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


def _our_offer(
    chat: list[Message], agent: str, offer_id: str
) -> dict[str, str]:
    for row in reversed(chat):
        if row.sender != agent:
            continue
        for offer in _offer_rows(row.text):
            if offer.get("offer_id") == offer_id:
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
    kind = words(message.text)
    if "confirm" in kind and (
        message.sender.startswith("citizen:") or message.sender.startswith("agent:")
    ):
        await _on_confirm(city, message, chat, hold_self=hold_self)
        return
    if "booking.receipt" in kind and message.sender.startswith("agent:"):
        await _forward_receipt(city, message, chat)
        return
    if "cancel" in kind and message.sender.startswith("agent:"):
        await _partner_cancel(city, message, chat, fixed=fixed)
        return
    if message.sender == LEDGER or message.sender.startswith("citizen:"):
        await _advance(city, message, chat, fixed=fixed, replacing=False)


async def _advance(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    fixed: str | None,
    replacing: bool,
) -> None:
    key = message.need_id or message.msg_id
    gate = _GATES.get(key)
    if gate is None:
        gate = _GATES[key] = _Gate()
    gate.turns += 1
    try:
        async with gate.lock:
            await _advance_alone(city, message, chat, key, fixed=fixed, replacing=replacing)
    finally:
        gate.turns -= 1
        if gate.turns == 0 and _GATES.get(key) is gate:
            del _GATES[key]


async def _advance_alone(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
    *,
    fixed: str | None,
    replacing: bool,
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
        reply = _ledger(chat, "prescription")
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
        number = _number(chat)
        if fixed is None and number and not procedure:
            if _pass == 2:
                return
            if not any(
                row.sender == agent and f"prescription_number={number}" in row.text.split()
                for row in chat
            ):
                await _say(city, LEDGER, f"prescription prescription_number={number}")
            if not _exact(chat, agent, "agenda"):
                await _say(city, LEDGER, "agenda")
            chat = await _until(
                city,
                message.need_id,
                chat,
                lambda rows: _has_procedure(rows) and _has_agenda(rows),
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
    sent = _sent(key)
    if replacing:
        trigger = fields_of(message.text).get("offer_id") or message.msg_id
        if sent.trigger != trigger:
            sent.trigger = trigger
            sent.again = set()
        taken = _withdrawn(chat, agent) | sent.again
    else:
        taken = _offered(chat, agent) | sent.offers
    chosen = [
        slot
        for slot in fitting(
            [line for line in world.splitlines() if "slot_id=" in line],
            now=now,
            priority=priority,
            lang=_lang(chat),
            gender=_gender(chat),
            facility=agent if fixed == "CONS-GIN" else None,
            procedure=procedure,
        )
        if slot.get("slot_id") not in taken
    ]
    if chosen:
        batch: list[dict[str, str]] = []
        lines: list[str] = []

        for slot in chosen[:3]:
            line = _offer_line(slot, number, priority)
            if len("\n".join(lines + [line])) > 2000:
                break
            batch.append(slot)
            lines.append(line)

        if lines and await _say(city, citizen, "\n".join(lines)):
            for slot in batch:
                sent.offers.add(slot["slot_id"])
                if replacing:
                    sent.again.add(slot["slot_id"])
        return
    if taken and not replacing:
        return
    page, pages = _page(world)
    if page < pages and page < _MAX_PAGE and not _asked(chat, agent, procedure, page + 1):
        await _say(city, LEDGER, f"world procedure={procedure} page={page + 1}")


async def _on_confirm(
    city: City, message: Message, chat: list[Message], *, hold_self: bool
) -> None:
    agent = city.agent_id
    fields = fields_of(message.text)
    offer_id = fields.get("offer_id", "")
    consent = fields.get("consent_ref", "")
    offer = _our_offer(chat, agent, offer_id) if offer_id else {}
    slot = fields.get("slot_id") or offer.get("slot_id", "")
    facility = fields.get("facility") or offer.get("facility", "")
    if not slot or not consent:
        return
    citizen = (
        message.sender
        if message.sender.startswith("citizen:")
        else fields.get("citizen", "")
    )
    number = fields.get("prescription_number") or offer.get("prescription_number")
    book_here = hold_self or message.sender.startswith("agent:") or facility == agent
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
    if _we(chat, agent, f"slot_id={slot}", to=facility, word="confirm"):
        return
    parts = [
        "confirm",
        f"offer_id={offer_id or offer.get('offer_id', '')}",
        f"slot_id={slot}",
        f"citizen={citizen}",
        f"consent_ref={consent}",
        f"facility={facility}",
    ]
    if number:
        parts.append(f"prescription_number={number}")
    await _say(city, facility, " ".join(parts))


async def _forward_receipt(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    booking_id = fields.get("booking_id", "")
    ledger_ref = fields.get("ledger_ref", "")
    citizen = _citizen(chat)
    if not booking_id or not ledger_ref or not citizen:
        return
    token = f"booking_id={booking_id}"
    if _we(chat, city.agent_id, token, to=citizen, word="booking.receipt"):
        return
    await _say(city, citizen, f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref}")


async def _partner_cancel(
    city: City, message: Message, chat: list[Message], *, fixed: str | None
) -> None:
    fields = fields_of(message.text)
    offer_id = fields.get("offer_id", "")
    citizen = _citizen(chat)
    if (
        offer_id
        and citizen
        and not _we(chat, city.agent_id, f"offer_id={offer_id}", to=citizen, word="cancel")
    ):
        await _say(city, citizen, f"cancel offer_id={offer_id} reason=facility_cancelled")
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
    """Serialize the whole direct-access journey, including Ledger push and polling."""
    key = (city.agent_id, message.need_id or message.msg_id)
    state = _FAMILY_CASES.setdefault(key, _FamilyCase())
    _FAMILY_CASES.move_to_end(key)
    state.turns += 1
    try:
        async with state.lock:
            _family_merge(state, chat)
            try:
                _family_merge(state, await city.history(need_id=message.need_id))
            except CapError:
                if message.sender.startswith("citizen:"):
                    await _family_note(city, state, message.sender, "history_unavailable")
                return  # Never write from an incomplete case after a process restart.
            _family_merge(state, [message])
            if not any(row.sender.startswith("citizen:") for row in state.rows):
                # Preserve facility-to-desk bookings. A peer's citizen= token never
                # overrides the real sender in a direct-access citizen journey.
                await _facility(city, message, state.rows, reports=False, hold_self=False)
                return
            await _family_turn(city, message, state)
    finally:
        state.turns -= 1
        # Do not evict a case with a running or queued turn.
        if len(_FAMILY_CASES) > _SENT_NEEDS:
            for old_key, old in list(_FAMILY_CASES.items()):
                if not old.turns:
                    del _FAMILY_CASES[old_key]
                    if len(_FAMILY_CASES) <= _SENT_NEEDS:
                        break


class _FamilyCase:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.turns = 0
        # Only actual mailbox messages and accepted sends (with real msg_id values).
        self.rows: list[Message] = []
        self.failures: dict[tuple[str, str], str] = {}


_FAMILY_CASES: OrderedDict[tuple[str, str], _FamilyCase] = OrderedDict()
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_NAMES = {"lun": "mon", "mar": "tue", "mer": "wed", "gio": "thu", "ven": "fri",
              **{day: day for day in _WEEKDAYS}}


def _family_merge(state: _FamilyCase, rows: list[Message]) -> None:
    known = {row.msg_id for row in state.rows}
    for row in rows:
        if row.msg_id not in known:
            state.rows.append(row)
            known.add(row.msg_id)


def _family_fields(row: Message) -> dict[str, str]:
    # Text is the wire format; structured fields also support the local chat shortcuts.
    result = {key: str(value) for key, value in understood(row).body.items()
              if isinstance(value, (str, int, bool)) and key != "text"}
    result.update(fields_of(row.text))
    for key in ("days", "languages"):
        match = re.search(r"(?<!\S)" + key + r"=(\S+)", row.text)
        if match:
            result[key] = match.group(1).rstrip(".;!?")
    return result


def _family_head(row: Message) -> str:
    head = first_word(row.text)
    if (row.sender.startswith("citizen:") and _fc_arrived(row.text)
            and understood(row).type == "need.request"):
        return "checkin"
    if head in {"agenda", "slots", "world", "rules", "held", "committed", "checked_in", "refused",
                "hold", "commit", "checkin", "offer", "confirm", "consent", "cancel",
                "data.request", "data.response", "booking.receipt", "escalate.emergency"}:
        return head
    return understood(row).type


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
    if message.sender == LEDGER and _family_head(message) not in {
        "agenda", "slots", "world", "rules", "held", "committed", "refused", "checked_in"
    }:
        return
    if any(row.sender == city.agent_id and _family_head(row) == "escalate.emergency" for row in state.rows):
        return
    checkin = next((row for row in reversed(state.rows)
                    if row.sender == citizen and _family_head(row) == "checkin"), None)
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
    phrase = ("Your arrival has been registered. Thank you."
              if _family_preferences(state.rows, citizen).get("lang", "it") == "en" else
              "Il tuo arrivo è stato registrato. Grazie.")
    await _family_send(city, state, citizen, f"checkin booking_id={booking}\n{phrase}")


def _family_days(value: str) -> set[str]:
    value = "".join(c for c in unicodedata.normalize("NFKD", value.lower()) if not unicodedata.combining(c))
    return {_DAY_NAMES[part[:3]] for part in re.split(r"[/,;|\s]+", value) if part[:3] in _DAY_NAMES}


def _family_preferences(rows: list[Message], citizen: str) -> dict[str, str]:
    prefs: dict[str, str] = {}
    for row in rows:
        if row.sender == citizen:
            fields = {**_fc_plain_prefs(row.text), **_family_fields(row)}
            for key in ("lang", "days", "gender", "gender_preference"):
                if key in fields:
                    prefs[key] = fields[key].lower()
    if "lang" not in prefs:
        first = next((row.text for row in rows if row.sender == citizen), "")
        if first:
            prefs["lang"] = _fc_guess_lang(first)
    return prefs


async def _family_read(
    city: City, state: _FamilyCase, command: str, head: str, *, refresh: bool
) -> Message | None:
    request = _family_request(state, city.agent_id, first_word(command), **fields_of(command))
    if request is not None and not refresh:
        return await _family_wait(city, state, request, head)
    request = await _family_send(city, state, LEDGER, command, repeat=True)
    return None if request is None else await _family_wait(city, state, request, head)


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
    agenda = await _family_read(city, state, "agenda", "agenda", refresh=refresh)
    now = _when("" if agenda is None else _family_fields(agenda).get("agenda_now", ""))
    if now is None:
        await _family_note(city, state, citizen, "ledger_timeout")
        return
    lines: list[str] = []
    for page in range(1, _MAX_PAGE + 1):
        command = f"world procedure=CONS-GIN facility={city.agent_id} page={page}"
        world = await _family_read(city, state, command, "slots", refresh=refresh)
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
        if (not sid or not doc or at is None or at <= now or sid in dropped
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
        if _family_head(row) == "cancel":
            fields = _family_fields(row)
            offers = {key: value for key, value in offers.items()
                      if not (value["facility"] == row.sender
                              and fields.get("offer_id", value["partner_offer_id"]) == value["partner_offer_id"]
                              and fields.get("slot_id", value["slot_id"]) == value["slot_id"])}
            continue
        for fields in _offer_rows(row.text):
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
                                "facility": row.sender, "procedure": "CONS-GIN", "at": at.isoformat(), "day": day}
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
        for target in sorted(peers)[:_FAMILY_MAX_PARTNERS]:
            # Public criteria only: never citizen, prescription, address, language or profile.
            query = (f"need.request service_type={_FAMILY_CAPABILITY} procedure=CONS-GIN "
                     f"request_id=rq-{key.split(':', 1)[1]}")
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
    for attempt in range(min(_PULL_TRIES, 8)):
        try:
            _family_merge(state, await city.history(need_id=message.need_id))
        except CapError:
            await _family_note(city, state, citizen, "history_unavailable")
            return
        answered = {row.sender for row in state.rows if row.sender in requests
                    and _family_head(row) in {"offer", "cancel"}}
        if answered == set(requests):
            break
        if attempt + 1 < min(_PULL_TRIES, 8):
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
    offers.sort(key=lambda offer: (bool(days) and offer["day"] not in days, offer["at"], offer["offer_id"]))
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


def _family_cross_consent(
    state: _FamilyCase, agent: str, citizen: str, offer_id: str, facility: str
) -> tuple[str, bool]:
    request = next((row for row in reversed(state.rows) if row.sender == agent and row.recipient == citizen
                    and _family_head(row) == "data.request" and _family_fields(row).get("purpose") == "cross_asst"
                    and _family_fields(row).get("offer_id") == offer_id
                    and _family_fields(row).get("facility") == facility), None)
    if request is None:
        return "", False
    for row in reversed(state.rows[state.rows.index(request) + 1:]):
        if row.sender != citizen:
            continue
        fields = _family_fields(row)
        if fields.get("offer_id", offer_id) != offer_id or fields.get("facility", facility) != facility:
            continue
        lowered = row.text.lower().strip()
        denied = (fields.get("granted", fields.get("accepted", "")).lower() == "false"
                  or re.search(r"\b(no|non acconsento|non autorizzo|rifiuto|decline|refuse|do not consent)\b", lowered))
        if denied and fields.get("purpose", "cross_asst") == "cross_asst":
            return "", True
        if _family_head(row) in {"consent", "data.response", "confirm"} and fields.get("purpose") == "cross_asst":
            ref = fields.get("consent_ref", "")
            if ref.lower() not in {"", "none", "false", "null"}:
                return ref, False
    return "", False


def _family_cross_forwards(state: _FamilyCase, agent: str) -> list[Message]:
    targets = _family_cross_requests(state, agent)
    return [row for row in state.rows if row.sender == agent and row.recipient in targets
            and _family_head(row) == "confirm" and _family_fields(row).get("purpose") == "cross_asst"]


def _family_cross_response(row: Message, request: Message) -> bool:
    fields, wanted = _family_fields(row), _family_fields(request)
    return (row.sender == request.recipient
            and _family_head(row) in {"booking.receipt", "cancel"}
            and all(fields.get(key, wanted.get(key)) == wanted.get(key) for key in ("offer_id", "slot_id", "facility")))


def _family_cross_uncertain(state: _FamilyCase, agent: str) -> bool:
    sent = {_family_cross_hash(row.text) for row in _family_cross_forwards(state, agent)}
    keys = {_family_fields(row).get("key", "") for row in state.rows if row.sender == agent
            and row.recipient == LEDGER and first_word(row.text) == "remember"
            and _family_fields(row).get("key", "").startswith("family_forward:")}
    return any(_family_cross_marker(state, agent, key) == "started" and key.split(":", 1)[1] not in sent
               for key in keys)


async def _family_cross_confirm(
    city: City, state: _FamilyCase, citizen: str, offer: dict[str, str], selected: dict[str, str]
) -> None:
    oid, target = offer["offer_id"], offer["facility"]
    shown = _our_offer(state.rows, city.agent_id, oid)
    if (not shown or any(selected.get(key, shown.get(key)) != shown.get(key) for key in ("slot_id", "facility"))):
        await _family_note(city, state, citizen, "invalid_offer")
        return
    consent, declined = _family_cross_consent(state, city.agent_id, citizen, oid, target)
    if declined:
        await _family_note(city, state, citizen, "cross_consent_declined")
        return
    if not consent:
        english = _family_preferences(state.rows, citizen).get("lang") == "en"
        phrase = ("May I share your citizen identifier and selected appointment with this facility to book it? Without permission I will not send them."
                  if english else
                  "Posso condividere con questa struttura il tuo identificativo cittadino e l'appuntamento scelto per prenotarlo? Senza consenso non li invio.")
        await _family_send(city, state, citizen,
                           f"data.request purpose=cross_asst fields=consent_ref offer_id={oid} facility={target}\n{phrase}")
        return
    # Refresh the clock after consent: do not send a now-expired offer to the partner.
    clock = await _family_read(city, state, "agenda", "agenda", refresh=True)
    now = _when("" if clock is None else _family_fields(clock).get("agenda_now", ""))
    if now is None:
        await _family_note(city, state, citizen, "ledger_timeout")
        return
    if _when(offer["at"]) <= now:
        await _family_send(city, state, citizen, f"cancel offer_id={oid} reason=slot_expired")
        await _family_cross_search(city, state, citizen, city.inbound, now)
        return
    command = (f"confirm offer_id={offer['partner_offer_id']} slot_id={offer['slot_id']} facility={target} "
               f"citizen={citizen} consent_ref={consent} purpose=cross_asst")
    key = "family_forward:" + _family_cross_hash(command)
    if _family_cross_marker(state, city.agent_id, key):
        await _family_note(city, state, citizen, "partner_pending")
        return
    if await _family_send(city, state, LEDGER, f"remember key={key} value=started") is None:
        await _family_note(city, state, citizen, "ledger_timeout")
        return
    if await _family_send(city, state, target, command) is None:
        code = state.failures.get((target, command), "UPSTREAM_TIMEOUT")
        try:
            permanent = 400 <= CapError(code).http_status() < 500
        except CapError:
            permanent = True
        if permanent:
            await _family_send(city, state, LEDGER, f"remember key={key} value=failed")
            await _family_send(city, state, citizen, f"cancel offer_id={oid} reason=facility_unavailable")
            await _family_cross_search(city, state, citizen, city.inbound, now)
            return
    await _family_note(city, state, citizen, "partner_pending")


async def _family_cross_event(city: City, state: _FamilyCase, citizen: str, message: Message) -> bool:
    forwards = _family_cross_forwards(state, city.agent_id)
    for request in reversed(forwards):
        responses = [row for row in state.rows[state.rows.index(request) + 1:]
                     if _family_cross_response(row, request)]
        receipt = next((row for row in reversed(responses) if _family_head(row) == "booking.receipt"
                        and _family_fields(row).get("booking_id") and _family_fields(row).get("ledger_ref")), None)
        if receipt is not None:
            fields = _family_fields(receipt)
            if message.sender == citizen and _family_head(message) == "checkin":
                phrase = ("Your appointment is at the facility shown here; use these references to check in there."
                          if _family_preferences(state.rows, citizen).get("lang") == "en" else
                          "L'appuntamento è presso la struttura indicata qui: usa questi riferimenti per registrare lì il tuo arrivo.")
                await _family_send(city, state, citizen,
                    f"data.response purpose=checkin reason=external_facility facility={request.recipient} "
                    f"booking_id={fields['booking_id']}\n{phrase}")
            else:
                await _family_send(city, state, citizen,
                    f"booking.receipt booking_id={fields['booking_id']} ledger_ref={fields['ledger_ref']} facility={request.recipient}")
            return True
        cancel = next((row for row in responses if _family_head(row) == "cancel"), None)
        if cancel is not None:
            original = _family_fields(request)
            oid = "fx-" + _family_cross_hash(request.recipient + "|" + original['offer_id'] + "|" + original['slot_id'])
            reason = _family_fields(cancel).get("reason", "facility_cancelled")
            reason = reason if re.fullmatch(r"[A-Za-z0-9_]{1,64}", reason) else "facility_cancelled"
            await _family_send(city, state, citizen, f"cancel offer_id={oid} reason={reason}")
            continue
        # A successful send with no definitive reply is uncertain. Never book another slot.
        await _family_note(city, state, citizen, "partner_pending")
        return True
    if _family_cross_uncertain(state, city.agent_id):
        await _family_note(city, state, citizen, "partner_pending")
        return True
    if message.sender in _family_cross_requests(state, city.agent_id):
        clock = await _family_read(city, state, "agenda", "agenda", refresh=True)
        now = _when("" if clock is None else _family_fields(clock).get("agenda_now", ""))
        if now is None:
            await _family_note(city, state, citizen, "ledger_timeout")
        else:
            await _family_cross_search(city, state, citizen, message, now)
        return True
    return False




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
        _LOG.info(
            "care.held.noconsent",
            extra={"need_id": fields.get("need_id", "")[:48]},
        )
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
        if row.sender == agent:
            for offer in _offer_rows(row.text):
                if offer.get("slot_id") == slot:
                    offer_id = offer.get("offer_id", offer_id)
        elif f"slot_id={slot}" in row.text.split():
            offer_id = fields_of(row.text).get("offer_id", offer_id)
    if (
        target
        and offer_id
        and not _we(chat, agent, f"offer_id={offer_id}", to=target, word="cancel")
    ):
        reason = fields_of(message.text).get("code", "booking_refused")
        await _say(
            city,
            target,
            f"cancel offer_id={offer_id} reason={reason}",
        )
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
