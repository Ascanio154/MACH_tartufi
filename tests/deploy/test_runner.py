import importlib.util
import unittest
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "deploy_runner.py"
_SPEC = importlib.util.spec_from_file_location("deploy_runner", _PATH)
dr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dr)


class Versions(unittest.TestCase):
    def test_next_version_above_highest_seen(self):
        self.assertEqual(dr.next_version("1.0.5", "1.0.7", "1.0.6"), "1.0.8")

    def test_next_version_ignores_blank_and_down_marker(self):
        self.assertEqual(dr.next_version("", "1.0.3"), "1.0.4")
        self.assertEqual(dr.next_version("1.0.0", "1.0.9"), "1.0.10")

    def test_next_version_compares_numerically(self):
        self.assertEqual(dr.next_version("1.0.10", "1.0.9"), "1.0.11")


class Changes(unittest.TestCase):
    def test_only_brain_changes_count(self):
        paths = ["asst/gp/agent/agent.py", "asst/hospital/card.yaml", "TEAM_BRIEF.md",
                 "asst/specialist/agent/agent.py", "tests/gp/test_gp.py"]
        self.assertEqual(dr.changed_agents(paths), ["gp", "specialist"])


class Outcomes(unittest.TestCase):
    def test_promoted(self):
        self.assertEqual(dr.classify("✓  Status promoted  agent:x@1.0.5")[0], "promoted")

    def test_quota_waits_the_stated_minutes(self):
        out = "✗  QUOTA_EXCEEDED  a team can deploy once every 2 minutes; try again in 2 min"
        self.assertEqual(dr.classify(out), ("quota", 130))

    def test_timeout_and_failure(self):
        self.assertEqual(dr.classify("✗  UPSTREAM_TIMEOUT  deployment x is still running")[0], "timeout")
        self.assertEqual(dr.classify("✗  agent ingress route did not become ready")[0], "failed")


class Queue(unittest.TestCase):
    def test_newest_commit_wins_and_keeps_place(self):
        queue = []
        dr.enqueue(queue, "gp", "aaa")
        dr.enqueue(queue, "hospital", "bbb")
        dr.enqueue(queue, "gp", "ccc")
        self.assertEqual([(j["agent"], j["sha"]) for j in queue], [("gp", "ccc"), ("hospital", "bbb")])

    def test_finished_job_does_not_absorb_a_new_one(self):
        queue = [{"agent": "gp", "sha": "aaa", "status": "live"}]
        dr.enqueue(queue, "gp", "bbb")
        self.assertEqual(len(queue), 2)


if __name__ == "__main__":
    unittest.main()
