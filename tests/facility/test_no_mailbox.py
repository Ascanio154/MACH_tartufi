"""The deploy's custody.ack probe (a need.request from agent:t05/osp-x with no need_id and no
chat text) must not make our brains read the whole mailbox: the check waits 5 s for the renew,
and hospital 1.0.22 failed it three times (Sat 21/11 12:56-13:02).

    ~/.local/share/uv/tools/city-cli/bin/python -m unittest tests.facility.test_no_mailbox -v
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from city_sdk.city import Message, Need

_ROOT = Path(__file__).resolve().parents[2] / "asst"


def _load(folder: str):
    spec = importlib.util.spec_from_file_location(f"{folder}_nomail", _ROOT / folder / "agent" / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._PULL_PAUSE = 0
    return module


class ProbeCity:
    def __init__(self, agent_id: str, inbound: Message) -> None:
        self.agent_id = agent_id
        self.address = ""
        self.inbound = inbound
        self.reads: list[str | None] = []

    async def say(self, to, text, *, status=None):
        return "ok"

    async def history(self, peer=None, *, need_id=None):
        self.reads.append(need_id)
        return [self.inbound]

    async def find(self, *a, **k):
        return []

    async def llm(self, *a, **k):
        raise RuntimeError("no model")


class NoMailboxRead(unittest.IsolatedAsyncioTestCase):
    async def test_custody_probe_reads_no_mailbox(self) -> None:
        for folder, agent in (("hospital", "agent:xtj1dk9x3qej"), ("specialist", "agent:q18es2d82f8r"),
                              ("family_counseling", "agent:pxvd2kk2yx5j")):
            with self.subTest(folder=folder):
                module = _load(folder)
                probe = Message(type="need.request", sender="agent:t05/osp-x", msg_id="probe-1",
                                need_id=None, body={"service": "lab", "summary": "visit", "district": 8},
                                recipient=agent)
                city = ProbeCity(agent, probe)
                await module.main(Need(service="lab", district=8, summary="visit", message=probe), city)
                self.assertNotIn(None, city.reads, f"{folder} read the whole mailbox")


if __name__ == "__main__":
    unittest.main()
