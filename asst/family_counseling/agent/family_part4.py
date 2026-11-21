"""Direct-access booking safety; loaded after the existing consultorio helpers."""

from __future__ import annotations

# Keep the existing partner protocol; revalidate new citizen writes here.
_family_send_base = _family_send
_family_turn_base = _family_turn
_FAMILY_WITHDRAW = re.compile(
    r"\b(non prenotare|non voglio|non acconsento|non autorizzo|rinuncio|annulla|annullo|"
    r"ho cambiato idea|rifiuto|cancel|changed my mind|do not book|don't book|"
    r"do not consent|withdraw)\b"
)
_FAMILY_RESTART = re.compile(
    r"\b(vorrei|voglio|posso|prenot|appuntamento|visita|i want|i need|appointment|book a)"
)


class _FamilyWriteStopped(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


def _family_withdraws(row: Message) -> bool:
    fields = _family_fields(row)
    return (_family_head(row) == "cancel"
            or fields.get("granted", fields.get("accepted", "")).lower() == "false"
            or bool(_FAMILY_WITHDRAW.search(_fc_plain(row.text)))
            or _fc_plain(row.text).strip(" .!?") in {"no", "no grazie", "no thanks"})


def _family_withdrawn(state: _FamilyCase, citizen: str) -> bool:
    stopped = False
    for row in state.rows:
        if row.sender != citizen:
            continue
        if _family_withdraws(row):
            stopped = True
        elif (first_word(row.text) in {"need.request", "confirm"}
              or _FAMILY_RESTART.search(_fc_plain(row.text))):
            stopped = False
    return stopped


def _family_reoffer_requested(state: _FamilyCase, citizen: str) -> bool:
    row = next((row for row in reversed(state.rows) if row.sender == citizen), None)
    return bool(row is not None and _family_head(row) == "cancel"
                and _family_fields(row).get("offer_id")
                and _family_fields(row).get("reason") in {
                    "deadline", "language", "lang", "days", "gender", "gender_preference",
                    "slot_expired", "no_availability", "doctor", "date", "time"})


def _family_selection(state: _FamilyCase, citizen: str) -> tuple[dict[str, str], str]:
    """A choice and its booking consent, invalidated by a later refusal/change."""
    selected: dict[str, str] = {}
    consent = ""
    shown: list[dict[str, str]] = []
    for row in state.rows:
        if row.recipient == citizen and row.sender.startswith("agent:"):
            offers = _offer_rows(row.text)
            if offers:
                shown = offers
                if selected.get("offer_id") not in {offer.get("offer_id") for offer in shown}:
                    selected, consent = {}, ""
            if _family_head(row) == "booking.receipt":
                shown = []
            continue
        if row.sender != citizen:
            continue
        fields, head = _family_fields(row), _family_head(row)
        if _family_withdraws(row):
            selected, consent = {}, ""
            continue
        purpose = fields.get("purpose", "booking")
        ref = fields.get("consent_ref", "")
        if purpose != "booking" or ref.lower() in {"none", "null", "false", "true"}:
            ref = ""
        if head == "confirm":
            selected, consent = fields, ref
        elif head in {"consent", "data.response"} and purpose == "booking":
            if selected and fields.get("offer_id", selected.get("offer_id")) == selected.get("offer_id"):
                consent = ref
        elif head not in {"consent", "data.response", "checkin"} and shown:
            picked = _fc_choice(row.text, shown)
            if picked is not None:
                selected = {key: picked[key] for key in ("offer_id", "slot_id", "facility") if key in picked}
                consent = ref
            elif _fc_plain_prefs(row.text):
                selected, consent = {}, ""
    return selected, consent


async def _family_stop_note(city: City, state: _FamilyCase, citizen: str, reason: str) -> None:
    english = _family_preferences(state.rows, citizen).get("lang") == "en"
    phrases = {
        "choice_withdrawn": (
            "Rispetto il tuo ripensamento: non confermo questa scelta. Puoi scrivermi se vuoi cercare altre date.",
            "I respect your change of mind and will not confirm this choice. Write to me if you want to look for other dates."),
        "choice_changed": (
            "Ho ricevuto la nuova scelta. Non confermo il posto precedente; verifico prima la pratica in corso per evitare due prenotazioni.",
            "I received your new choice. I will not confirm the previous slot; the pending request must be checked first to avoid two bookings."),
        "already_booked": (
            "La prenotazione risulta già confermata. Il tuo messaggio non l'ha annullata: conserva la ricevuta e contatta la struttura indicata per verificarne la modifica.",
            "Your booking is already confirmed. This message has not cancelled it: keep the receipt and contact the facility shown there to check how it can be changed."),
        "commit_pending": (
            "Un'operazione di prenotazione era già stata inviata. Sto verificando l'esito: non posso ancora dichiarare la prenotazione annullata né iniziarne un'altra.",
            "A booking operation had already been sent. Its outcome is being checked; I cannot say the booking is cancelled or start another one yet."),
        "slot_expired": (
            "L'orario proposto è ormai passato e non ho prenotato quel posto: ti mando subito le nuove disponibilità, senza ricetta.",
            "The offered time has passed and I have not booked that slot: I am sending you the new availability right away, without a referral."),
    }
    if reason not in phrases:
        await _family_note(city, state, citizen, reason)
        return
    text = phrases[reason][english]
    if reason == "choice_withdrawn" and getattr(state, "told_booking", False):
        text += _FAMILY_TOLD_DESK[english]
    await _family_send_base(city, state, citizen,
                           f"data.response purpose=booking reason={reason}\n{text}")


# Said to the citizen ONLY after the notice to our booking centre was actually sent.
_FAMILY_TOLD_DESK = (
    " Ho già avvisato io il centro prenotazioni che il posto potrebbe liberarsi per altre persone.",
    " I have already told the booking centre myself that the slot may be free for other people.")


async def _family_slot_free(city: City, state: _FamilyCase, citizen: str, message: Message) -> None:
    """A refused offer is a hint to recheck, never proof that capacity was released."""
    oid = _family_fields(message).get("offer_id", "")
    offer = _our_offer(state.rows, city.agent_id, oid)
    slot = offer.get("slot_id", "")
    if (not slot or offer.get("facility") != city.agent_id
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", slot)):
        return
    sent = await _family_send_base(city, state, _OUR_BOOKING_CENTRE,
                           f"notice availability_check procedure=CONS-GIN slot_id={slot}\n"
                           "Una persona ha rinunciato all'offerta. Verificate la disponibilità sul Ledger prima di proporre il posto.")
    # The review reads only the citizen's messages: the next note to them says we told the desk.
    state.told_booking = sent is not None


async def _family_send(
    city: City, state: _FamilyCase, to: str, text: str, *, repeat: bool = False
) -> Message | None:
    """Revalidate the latest citizen intent immediately before a new booking write."""
    op = first_word(text)
    guarded = (to == LEDGER and op in {"hold", "commit"}) or (
        to.startswith("agent:") and op == "confirm")
    citizen = next((row.sender for row in state.rows if row.sender.startswith("citizen:")), None)
    if to.startswith("agent:") and op == "need.request" and citizen is not None and "lang" not in fields_of(text):
        language = _family_preferences(state.rows, citizen).get("lang", "it")
        if re.fullmatch(r"[a-z]{2,3}", language):
            text += f" lang={language}"
    existing = next((row for row in state.rows if row.sender == city.agent_id
                     and row.recipient == to and row.text == text), None)
    if not guarded or citizen is None or existing is not None or (to, text) in state.failures:
        return await _family_send_base(city, state, to, text, repeat=repeat)
    key = "family_write:" + _family_cross_hash(text)
    marker = _family_cross_marker(state, city.agent_id, key) if to == LEDGER else ""
    if marker:
        raise _FamilyWriteStopped("booking_refused" if marker == "failed" else "commit_pending")
    if _family_write_uncertain(state, city.agent_id):
        raise _FamilyWriteStopped("commit_pending")
    try:
        await _family_validate_write(city, state, citizen, to, text)
    except _FamilyWriteStopped:
        if to.startswith("agent:") and op == "confirm":
            # This guard ran BEFORE any send attempt: clear only that unsent marker.
            key = "family_forward:" + _family_cross_hash(text)
            await _family_send_base(city, state, LEDGER, f"remember key={key} value=aborted")
        raise
    if to == LEDGER:
        # Persist intent before the write. A crash/timeout without its mailbox row
        # must not become a new msg_id (and another booking) after restart.
        if await _family_send_base(city, state, LEDGER, f"remember key={key} value=started") is None:
            raise _FamilyWriteStopped("ledger_timeout")
    _LOG.info("family.write.validated op=%s", op)
    sent = await _family_send_base(city, state, to, text, repeat=repeat)
    if sent is None and to == LEDGER:
        code = state.failures.get((to, text), "UPSTREAM_TIMEOUT")
        try:
            permanent = 400 <= CapError(code).http_status() < 500
        except CapError:
            permanent = False  # an unknown transport outcome is not proof of rejection
        if permanent:
            await _family_send_base(city, state, LEDGER, f"remember key={key} value=failed")
        else:
            raise _FamilyWriteStopped("commit_pending")
    return sent


def _family_write_uncertain(state: _FamilyCase, agent: str) -> bool:
    writes = {_family_cross_hash(row.text) for row in state.rows
              if row.sender == agent and row.recipient == LEDGER and _family_head(row) in {"hold", "commit"}}
    keys = {_family_fields(row).get("key", "") for row in state.rows
            if row.sender == agent and row.recipient == LEDGER and first_word(row.text) == "remember"
            and _family_fields(row).get("key", "").startswith("family_write:")}
    return any(_family_cross_marker(state, agent, key) == "started" and key.split(":", 1)[1] not in writes
               for key in keys)


async def _family_validate_write(city: City, state: _FamilyCase, citizen: str, to: str, text: str) -> None:
    op = first_word(text)
    try:
        _family_merge(state, await city.history(need_id=city.inbound.need_id))
    except CapError:
        raise _FamilyWriteStopped("history_unavailable") from None
    if _family_withdrawn(state, citizen):
        raise _FamilyWriteStopped("choice_withdrawn")
    if any(row.sender == citizen and _emergency(row) for row in state.rows):
        raise _FamilyWriteStopped("emergency")
    selected, consent = _family_selection(state, citizen)
    offer = _our_offer(state.rows, city.agent_id, selected.get("offer_id", ""))
    if not selected or not offer:
        raise _FamilyWriteStopped("choice_changed")
    if any(selected.get(key, offer.get(key)) != offer.get(key) for key in ("slot_id", "facility")):
        raise _FamilyWriteStopped("invalid_offer")
    fields = fields_of(text)
    if op != "commit" and fields.get("citizen") != citizen:
        raise _FamilyWriteStopped("invalid_offer")
    slot = fields.get("slot_id", "")
    if op == "commit":
        request = _family_request(state, city.agent_id, "hold")
        held = _family_reply(state, city.agent_id, request, "held") if request else None
        if held is None or _family_fields(held).get("hold_id") != fields.get("hold_id"):
            raise _FamilyWriteStopped("ledger_timeout")
        slot = _family_fields(request).get("slot_id", "")
    if slot != offer.get("slot_id"):
        raise _FamilyWriteStopped("choice_changed")
    if to == LEDGER:
        if offer.get("facility") != city.agent_id:
            raise _FamilyWriteStopped("invalid_offer")
        if not consent or fields.get("consent_ref") != consent:
            raise _FamilyWriteStopped("consent_missing")
        if op == "hold":
            # A local choice needs the same fresh-clock check as an external one.
            clock = await _family_read(city, state, "agenda", "agenda", refresh=True, shared=False)
            now = _when(_family_fields(clock).get("agenda_now", "")) if clock else None
            at = _when(offer.get("agenda_date", offer.get("at", "")))
            if now is None or at is None:
                raise _FamilyWriteStopped("ledger_timeout")
            if at <= now:
                raise _FamilyWriteStopped("slot_expired")
            # Clock polling can itself receive a cancellation or another choice.
            latest, latest_ref = _family_selection(state, citizen)
            if _family_withdrawn(state, citizen):
                raise _FamilyWriteStopped("choice_withdrawn")
            if latest.get("offer_id") != selected.get("offer_id") or latest_ref != consent:
                raise _FamilyWriteStopped("choice_changed")
    else:
        ref, declined = _family_cross_consent(state, city.agent_id, citizen, offer["offer_id"], to)
        if declined or not ref or ref != fields.get("cross_asst_consent_ref"):
            raise _FamilyWriteStopped("cross_consent_declined" if declined else "consent_missing")
        if offer.get("facility") != to:
            raise _FamilyWriteStopped("invalid_offer")


async def _family_turn(city: City, message: Message, state: _FamilyCase) -> None:
    citizen = next((row.sender for row in state.rows if row.sender.startswith("citizen:")), None)
    if citizen is None:
        return
    if _family_write_uncertain(state, city.agent_id) or _family_cross_uncertain(state, city.agent_id):
        await _family_stop_note(city, state, citizen, "commit_pending")
        return
    if _family_withdrawn(state, citizen):
        # A commit already sent must be reconciled; never claim it was cancelled.
        forwards = _family_cross_forwards(state, city.agent_id)
        if forwards:
            request = forwards[-1]
            responses = [row for row in state.rows[state.rows.index(request) + 1:]
                         if _family_cross_response(row, request)]
            if any(_family_head(row) == "booking.receipt" and _family_fields(row).get("booking_id")
                   and _family_fields(row).get("ledger_ref") for row in responses):
                await _family_cross_event(city, state, citizen, message)
                reason = "already_booked"
            else:
                reason = "choice_withdrawn" if any(_family_head(row) == "cancel" for row in responses) else "commit_pending"
            await _family_stop_note(city, state, citizen, reason)
            return
        commit = _family_request(state, city.agent_id, "commit")
        if commit is not None:
            reply = _family_reply(state, city.agent_id, commit, "committed")
            if reply is not None and _family_head(reply) == "committed":
                await _family_receipt(city, state, citizen)
                await _family_stop_note(city, state, citizen, "already_booked")
            elif reply is None:
                await _family_stop_note(city, state, citizen, "commit_pending")
            else:
                await _family_stop_note(city, state, citizen, "choice_withdrawn")
        else:
            if message.sender == citizen and _family_withdraws(message):
                await _family_slot_free(city, state, citizen, message)
            if message.sender == citizen and _family_reoffer_requested(state, citizen):
                await _family_offers(city, state, citizen, message, refresh=True)
            else:
                await _family_stop_note(city, state, citizen, "choice_withdrawn")
        return
    try:
        await _family_turn_base(city, message, state)
    except _FamilyWriteStopped as stopped:
        _LOG.info("family.write.stopped reason=%s", stopped.reason)
        if stopped.reason == "emergency":
            await _say(city, citizen, _emergency_line(message))
        else:
            await _family_stop_note(city, state, citizen, stopped.reason)
            if stopped.reason == "choice_withdrawn" and _family_reoffer_requested(state, citizen):
                await _family_offers(city, state, citizen, message, refresh=True)
            elif stopped.reason == "slot_expired":
                # The citizen asked for a visit: new dates in the same turn, not "ask me again".
                await _family_offers(city, state, citizen, message, refresh=True)
