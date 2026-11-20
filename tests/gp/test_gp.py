"""GP brain: protocol, red flags, model sentence with fallback, renewal.

Run from the repo root with the city-cli Python (it ships city_sdk):
    ~/.local/share/uv/tools/city-cli/bin/python -m unittest tests.gp.test_gp -v
"""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import unittest
from pathlib import Path

from city_sdk.city import LEDGER, CapError, Message, understood

_BRAIN = Path(__file__).resolve().parents[2] / "asst" / "gp" / "agent" / "agent.py"
_spec = importlib.util.spec_from_file_location("gp_brain", _BRAIN)
gp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gp)
gp._PULL_PAUSE = 0
gp._PULL_TRIES = 2

AGENT = "agent:gp"
CIT = "citizen:c1"
NEED = "N-2026-11-02-890923000099"
OLD = "prescription_number-N-2026-11-02-OLD1"
GOOD = "Capisco la sua preoccupazione, la ricetta è pronta: contatti il centro prenotazioni."
_ids = itertools.count(1)

# Real citizen messages from the 20 Nov wave, with what the judge scored as right.
REAL = [
    ("Cuore che batte forte. Serve un controllo, sono di fretta.", "VIS-CARD", "B"),
    ("raga ciao, lowkey il mio cuore che batte è strano, tipo a mille. boh, vorrei un check, fr.", "VIS-CARD", "B"),
    ("Buongiorno. Io ho mal di schiena per alcune settimane. Potrei visita?", "VIS-FIS", "B"),
    ("Gentile dottore, sono un po' in ansia perché ho mal di schiena da un po' di settimane e mi farebbe piacere un suo parere.", "VIS-FIS", "B"),
    ("Allora, pare che il mio \"mal di schiena\" si stia godendo una lunga vacanza qui da settimane", "VIS-FIS", "B"),
    ("Gentile dottore, spero stia bene. Sento il cuore che batte un po' troppo forte, quasi come quando ero giovane e correvo dietro a mio marito... La ringrazio di cuore!", "VIS-CARD", "B"),
    ("Ho le palpitazioni da ieri", "VIS-CARD", "B"),
    ("Mi fa male il ginocchio quando cammino", "VIS-FIS", "D"),
]
RED = [
    "Buongiorno. Da stamattina ho forte dolore al petto.",
    "raga ma tipo da stamattina ho un dolore al petto assurdo e lowkey non capisco che succede boh 😬",
    "Mi scusi se la disturbo, da stamattina ho un forte dolore al petto. Faccio ciò che mi dirà.",
    "Ho mal di schiena e non sento più le gambe",
]


def msg(sender: str, text: str, recipient: str = AGENT) -> Message:
    return Message(
        type="chat", sender=sender, msg_id=f"m-{next(_ids)}", need_id=NEED,
        body={"text": text}, recipient=recipient,
    )


class FakeCity:
    def __init__(self, llm=GOOD, ledger=None, wait=0.0):
        self.agent_id = AGENT
        self.address = ""
        self.rows: list[Message] = []
        self.sent: list[tuple[str, str]] = []
        self._llm, self._ledger, self._wait = llm, ledger or {}, wait
        self.llm_calls = 0

    async def say(self, to, text, *, status=None):
        self.sent.append((to, text))
        self.rows.append(msg(AGENT, text, to))
        if to == LEDGER:
            for key, answer in self._ledger.items():
                if key in text:
                    self.rows.append(msg(LEDGER, answer))
        return "id"

    async def history(self, peer=None, *, need_id=None):
        return list(self.rows)

    async def llm(self, messages, *, need_id=None):
        self.llm_calls += 1
        await asyncio.sleep(self._wait)
        if isinstance(self._llm, Exception):
            raise self._llm
        return {"content": self._llm}


def turn(city: FakeCity, sender: str, text: str) -> None:
    message = msg(sender, text)
    city.rows.append(message)
    asyncio.run(gp.handle("gp", city, message, list(city.rows)))


def to(city: FakeCity, who: str) -> list[str]:
    return [text for dest, text in city.sent if dest == who]


class Protocol(unittest.TestCase):
    def test_real_messages_get_the_protocol_prescription(self):
        for text, procedure, priority in REAL:
            with self.subTest(text=text[:40]):
                city = FakeCity()
                turn(city, CIT, text)
                self.assertEqual(city.sent[0][0], LEDGER, "ledger is written first")
                self.assertIn(f"procedure={procedure} priority={priority}", city.sent[0][1])
                (said,) = to(city, CIT)
                read = understood(msg(AGENT, said))
                self.assertEqual(read.type, "prescription")
                self.assertEqual(read.body["procedure"], procedure)
                self.assertEqual(read.body["priority"], priority)

    def test_red_flags_escalate_and_stop(self):
        for text in RED:
            with self.subTest(text=text[:40]):
                city = FakeCity()
                turn(city, CIT, text)
                self.assertEqual(to(city, LEDGER), [])
                (said,) = to(city, CIT)
                self.assertTrue(said.startswith("escalate.emergency reason=red_flag"))
                self.assertEqual(city.llm_calls, 0)
                turn(city, CIT, "ok grazie")
                self.assertEqual(len(city.sent), 1, "nothing else goes out after an escalation")

    def test_unknown_text_is_asked_once(self):
        city = FakeCity()
        turn(city, CIT, "Buongiorno, non sto bene")
        turn(city, CIT, "boh")
        self.assertEqual(to(city, CIT), [gp._ASK_SYMPTOM])

    def test_same_message_twice_is_one_prescription(self):
        city = FakeCity()
        turn(city, CIT, REAL[0][0])
        turn(city, CIT, REAL[0][0])
        self.assertEqual(len(to(city, LEDGER)), 1)
        self.assertEqual(len(to(city, CIT)), 1)


class Sentence(unittest.TestCase):
    def said(self, **kw) -> str:
        city = FakeCity(**kw)
        turn(city, CIT, REAL[3][0])
        (said,) = to(city, CIT)
        self.assertEqual(understood(msg(AGENT, said)).type, "prescription")
        return said

    def test_model_sentence_follows_the_tokens(self):
        self.assertTrue(self.said().endswith("priority=B " + GOOD))

    def test_unsafe_model_output_falls_back(self):
        bad = [
            "Prenda ibuprofene 400mg e si riposi, la ricetta è pronta.",
            "La ricetta è pronta, priority=U per lei, contatti il centro.",
            "Your prescription is ready, please confirm with the booking centre.",
            "Probabilmente la diagnosi è una lombalgia, la ricetta è pronta.",
            "ok",
            "x" * 700,
            "",
        ]
        for line in bad:
            with self.subTest(line=line[:30]):
                self.assertTrue(self.said(llm=line).endswith(gp._rx_note("VIS-FIS", "B")))

    def test_model_error_or_slow_model_falls_back(self):
        self.assertTrue(
            self.said(llm=CapError("UPSTREAM_TIMEOUT", "x")).endswith(gp._rx_note("VIS-FIS", "B"))
        )
        old, gp._LLM_WAIT = gp._LLM_WAIT, 0.05
        try:
            self.assertTrue(self.said(wait=1.0).endswith(gp._rx_note("VIS-FIS", "B")))
        finally:
            gp._LLM_WAIT = old

    def test_ledger_announcement_waits_for_the_citizen_turn(self):
        number = gp._rx(NEED, "m")
        gp._WRITING.add(number)
        try:
            city = FakeCity()
            city.rows.append(msg(CIT, REAL[0][0]))
            turn(city, LEDGER, f"prescribed prescription_number={number} patient={CIT}")
            self.assertEqual(city.sent, [])
        finally:
            gp._WRITING.discard(number)


class Renewal(unittest.TestCase):
    def test_known_number_is_renewed_for_the_same_visit(self):
        line = f"prescription prescription_number={OLD} procedure=VIS-CARD priority=B patient={CIT} state=expired"
        city = FakeCity(ledger={f"prescription prescription_number={OLD}": line})
        turn(city, CIT, f"need.request prescription_number={OLD}")
        asked, wrote = to(city, LEDGER)
        self.assertEqual(asked, f"prescription prescription_number={OLD}")
        self.assertTrue(wrote.startswith(f"prescribe patient={CIT} procedure=VIS-CARD priority=B"))
        self.assertNotIn(OLD, wrote)
        (said,) = to(city, CIT)
        read = understood(msg(AGENT, said))
        self.assertEqual((read.type, read.body["procedure"]), ("prescription", "VIS-CARD"))
        self.assertNotEqual(read.body["prescription_number"], OLD)

    def test_unknown_or_foreign_number_is_not_renewed(self):
        for line in (
            f"refused prescription NOT_FOUND prescription_number={OLD}",
            f"prescription prescription_number={OLD} procedure=VIS-CARD priority=B patient=citizen:other",
        ):
            with self.subTest(line=line[:30]):
                city = FakeCity(ledger={f"prescription prescription_number={OLD}": line})
                turn(city, CIT, f"Mi rinnova la ricetta {OLD}?")
                self.assertEqual(len(to(city, LEDGER)), 1)
                self.assertEqual(to(city, CIT), [gp._ASK_SYMPTOM])

    def test_thanks_quoting_our_own_number_is_not_a_renewal(self):
        city = FakeCity()
        turn(city, CIT, REAL[0][0])
        number = understood(msg(AGENT, to(city, CIT)[0])).body["prescription_number"]
        turn(city, CIT, f"grazie, ho ricevuto prescription_number={number}")
        self.assertEqual(len(city.sent), 2)


if __name__ == "__main__":
    unittest.main()
