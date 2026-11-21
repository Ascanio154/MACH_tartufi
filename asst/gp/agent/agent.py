"""Care-protocol example. ``city init`` does not copy this file.

The scaffold brain is ``templates/agent/agent/agent.py``. This one is the full care
protocol (prescribe, offer, hold, commit, check-in, report), one inbound message per
step. The role is ``role`` in ``card.yaml``. Ledger writes are chat to ``city:ledger``;
citizens read the act word and ``key=value`` tokens (ADR-110).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
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
    (
        "SYM-BACK",
        (
            "mal di schiena", "schiena", "lombalgia", "lombar", "lombi", "sciatic",
            "colpo della strega", "back pain", "backache", "back ache", "my back", "lower back",
            "back hurt", "back is killing",
        ),
    ),
    ("SYM-KNEE", ("ginocchi", "knee", "menisc", "rotula", "crociato")),
    (
        "SYM-PALPITATIONS",
        (
            "cuore che batte", "cuore batte", "cuore a mille", "cuore mi va", "cuore va a",
            "cuore impazzit", "cuore in gola", "batticuore", "palpitazion", "tachicardi",
            "battito accelerat", "battito irregolar", "battiti", "extrasistol",
            "heart racing", "racing heart", "heart is racing", "heart pounding",
            "pounding heart", "palpitation", "skips a beat", "skipping beats", "skips beats",
            "flutter", "heart beating", "heartbeat", "heart beats fast",
        ),
    ),
    (
        "SYM-FOLLOWUP",
        (
            "controllo del cuore", "controllo al cuore", "controllo per il cuore",
            "controllo cardiolog", "visita cardiolog", "routine heart check", "heart check",
            "heart follow", "cardiac check", "cardiology check",
        ),
    ),
)
_RED_FLAGS = (
    "perdita di forza alle gambe",
    "loss of strength in the legs",
    "dolore al petto",
    "chest pain",
)
# Body part near a warning word, either order, within a short window. A missed escalation
# is -2; an extra one costs at most that case's prescription, so these lean wide.
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
) + (re.compile(r"\binfarto\b|heart attack|\bictus\b|\bstroke\b"),)
_ASK_SYMPTOM = (
    "Mi dispiace per il disturbo, voglio aiutarla. Mi descrive meglio cosa sente, "
    "dove e da quanto tempo? / I'm sorry you're unwell. Could you tell me what you feel, "
    "where, and since when?"
)
_DEADLINE_DAYS = {"B": 2, "D": 5}
# The model writes only the sentence; the act word and tokens always come from code.
# ponytail: one fixed wait; the gateway took 11-12 s in tests, tune if turns get cut.
_LLM_WAIT = 20.0
# The LAST act word in a text is read as the act, so the sentence must not hold one. Words
# that only start like an act ("offered", "reports", "consenso") are refused too: the city's
# own reader is not ours and may count them.
_ACT_IN_TEXT = re.compile(
    r"\b(offer|confirm|cancel|consent|report|hold|check-?in)|"
    r"(need|data)\.re|booking\.receipt|case\.closed",
    re.IGNORECASE,
)
# A placeholder the model left unfilled, like "[CODICE]".
_PLACEHOLDER = re.compile(r"[\[\]{}<>]")
# Journey 000149: the model wrote "code: 12345". The real number is in the tokens before the
# sentence; any digits run or code word in the model's text is invented, so it falls back.
_INVENTED_CODE = re.compile(r"\d{3,}|\b(code|codice|codes|codici|ref|riferimento)\s*[:#]",
                            re.IGNORECASE)
_EN_WORDS = frozenset(
    {"the", "my", "i", "have", "and", "please", "would", "doctor", "is", "been", "you", "a"}
)
_IT_WORDS = frozenset(
    {"il", "la", "ho", "che", "di", "un", "mi", "sono", "per", "dottore", "e", "da"}
)
_BANNED = re.compile(
    r"=|citizen:|agent:|prescription_number|escalate|\bmg\b|farmac|medicinal|compress|pastigli|"
    r"antidolorif|antinfiammator|paracetamol|ibuprofen|tachipirina|aspirin|painkiller|\bpills?\b|"
    r"\bdrugs?\b|medication|"
    # Clinical names for what the citizen described in plain words read as a diagnosis.
    r"tachicardi|tachycardi|aritmi|arrhythmi|lombalgi|lumbago|sciatic|artros|arthr|ernia|hernia|"
    r"infiammazion|inflammation",
    re.IGNORECASE,
)
_RX_IN_TEXT = re.compile(r"prescription_number-[A-Z0-9-]{4,40}")
# Prescription numbers a citizen turn of this process is still answering.
_WRITING: set[str] = set()
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
    # A GP still escalates a red flag that also asks for a dose: a missed escalation is -2.
    if clinical(text) and not (role == "gp" and _emergency(message)):
        raise OutOfScope()
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
    except CapError as err:
        _LOG.warning("say.failed to=%s code=%s", to.split(":")[0], getattr(err, "code", ""))
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
    if any(flag in lowered for flag in _RED_FLAGS):
        return True
    return any(pattern.search(lowered) for pattern in _RED_FLAG_RE)


def _symptom(message: Message) -> str | None:
    body = understood(message).body
    for key in ("symptom_code", "symptom"):
        code = body.get(key)
        if isinstance(code, str) and code in _PROTOCOLS:
            return code
    # A bare code in free text ("rispondi SYM-KNEE") is not a symptom: "knee" must not match it.
    lowered = re.sub(r"sym-[a-z]+", " ", message.text.lower())
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


# --- the rest of the brain ----------------------------------------------------------------------
# City Hall refuses an oversized brain file, so the code lives in the files below, next to this
# one. Each runs in THIS module's namespace, in order: one brain, exactly as before the split.
_PARTS = ("gp_part1.py", "gp_part2.py")


def _load_parts() -> None:
    folder = Path(__file__).resolve().parent
    for name in _PARTS:
        source = folder / name
        exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), globals())


_load_parts()
