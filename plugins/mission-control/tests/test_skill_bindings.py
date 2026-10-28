from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestration.bindings import (MAX_BYTES, WARN_BYTES, build_bundle,
                                    load_run_skills, payload_report, skill_payload, validate_bindings)
from orchestration.cli import main
from orchestration.configuration import load_configuration, load_configuration_snapshot
from orchestration.doctor import inspect_bindings
from orchestration.failures import ValidationError
from orchestration.io import atomic_write_json, load_data
from orchestration.native import claim_task, session_review
from orchestration.provider_registry import ProviderRegistry
from orchestration.review import apply_review
from orchestration.runs import sources_match
import test_native_session as native_fixtures
import test_native_cli as cli_fixtures
from test_native_session import task


class BindingTests(unittest.TestCase):
    # Reuse the existing native workspace fixture without inheriting its test cases.
    setUp = native_fixtures.NativeSessionTests.setUp
    start = native_fixtures.NativeSessionTests.start
    finish = native_fixtures.NativeSessionTests.finish

    def skill(self, name="ui", text="Follow the UI procedure.\n") -> Path:
        directory = self.workspace / ".claude/skills" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(text, encoding="utf-8")
        self.bundle["methodology"].setdefault("file_skills", {})[name] = str(directory.relative_to(self.workspace))
        return directory

    def graph(self, names=None) -> dict:
        one = task("TASK-1", "one.txt")
        if names is not None:
            one["skills"] = names
        return {"schema_version": "1.0", "workflow": {"id": "bindings", "objective": "Fixture"},
                "parallelization": {"enabled": False, "max_workers": 1, "require_non_overlapping_scope": True},
                "tasks": [one]}

    def frozen(self, graph=None) -> dict:
        return build_bundle(ROOT, self.workspace, self.bundle["methodology"], graph)

    def test_local_reference_graph_is_frozen_deduplicated_and_portable(self):
        directory = self.skill(text="[Rules](../../rules.md#principles) [Architecture](architecture.md) [web](https://example.invalid/a.md)\n")
        self.skill("second", "[Rules](../../rules.md)\n")
        (self.workspace / ".claude/rules.md").write_text("Shared rulebook fixture\n")
        (directory / "architecture.md").write_text("[Cycle](SKILL.md) [Rules](../../rules.md)\n")
        frozen = self.frozen(self.graph(["ui", "second"]))
        payload = skill_payload(frozen, ["ui", "second"])
        self.assertEqual(payload.count("Shared rulebook fixture"), 1)
        self.assertEqual(len(frozen["files"]), 4)
        self.assertIn("https://example.invalid/a.md", payload)
        self.assertEqual(frozen["diagnostics"], [])
        self.assertNotIn(str(self.workspace), payload)

    def test_paths_symlinks_missing_resources_and_unsupported_links(self):
        directory = self.skill(text="[Outside](../../../../outside.md)\n")
        with self.assertRaisesRegex(ValidationError, "escapes"):
            self.frozen()
        (directory / "SKILL.md").write_text("[Missing](missing.md)\n")
        with self.assertRaisesRegex(ValidationError, "missing"):
            self.frozen()
        outside = self.workspace.parent / (self.workspace.name + "-outside.md")
        outside.write_text("outside\n")
        self.addCleanup(outside.unlink)
        (directory / "escape.md").symlink_to(outside)
        (directory / "SKILL.md").write_text("[Escape](escape.md)\n")
        with self.assertRaisesRegex(ValidationError, "escapes"):
            self.frozen()
        (directory / "SKILL.md").write_text("[Helper](helper.py) ![Image](asset.png) [Anchor](#local)\n")
        report = inspect_bindings(ROOT, self.workspace, self.bundle["methodology"])
        self.assertEqual(report["status"], "valid")
        self.assertIn("unsupported resource", report["diagnostics"][0])
        self.assertEqual(len(report["diagnostics"]), 2)

    def test_angle_and_reference_links_with_spaces(self):
        directory = self.skill(text="[Notes](<notes with spaces.md>) [Reference][rules]\n\n[rules]: ../../rules.md#principles\n")
        (directory / "notes with spaces.md").write_text("Named note fixture\n")
        (self.workspace / ".claude/rules.md").write_text("Reference definition fixture\n")
        payload = skill_payload(self.frozen(), ["ui"])
        self.assertIn("Named note fixture", payload)
        self.assertIn("Reference definition fixture", payload)

    def test_static_contract_rejects_unknown_duplicate_and_recursive_assignments(self):
        self.skill()
        for graph in [self.graph(["absent"]), self.graph(["ui", "ui"])]:
            with self.subTest(graph=graph), self.assertRaises(ValidationError):
                self.frozen(graph)
        for gate in ["implementation", "review", "invented"]:
            methodology = dict(self.bundle["methodology"], gate_skills={gate: []})
            with self.subTest(gate=gate), self.assertRaises(ValidationError):
                validate_bindings(methodology)
        for source in ["/tmp/skill", "../skill", "other-plugin:skill"]:
            with self.subTest(source=source), self.assertRaises(ValidationError):
                validate_bindings(dict(self.bundle["methodology"], file_skills={"bad": source}))
        with self.assertRaisesRegex(ValidationError, "recursive"):
            validate_bindings(dict(self.bundle["methodology"], gate_skills={"intake": [
                {"skill": "mission-control:mission-control", "when": "always"}]}))
        with self.assertRaisesRegex(ValidationError, "unknown"):
            validate_bindings(dict(self.bundle["methodology"], reviewer_skills=["absent"]))

    def test_unselected_missing_skill_does_not_block_and_own_plugin_reference_works(self):
        self.bundle["methodology"]["file_skills"] = {"unused": ".claude/skills/missing", "discipline": "mission-control:code-discipline"}
        frozen = self.frozen(self.graph(["discipline"]))
        self.assertIn("discipline", frozen["skills"])
        self.assertNotIn("unused", frozen["skills"])
        self.assertEqual(inspect_bindings(ROOT, self.workspace, self.bundle["methodology"])["status"], "invalid")

    def test_assembled_byte_limits_and_one_warning_per_shared_payload(self):
        self.skill(text="é" * WARN_BYTES)
        graph = self.graph(["ui"])
        graph["tasks"].append(dict(task("TASK-2", "two.txt"), skills=["ui"]))
        frozen = self.frozen(graph)
        report = payload_report(frozen, graph)
        self.assertEqual(len(report["warnings"]), 1)
        self.assertEqual(report["warnings"][0]["uses"], ["TASK-1", "TASK-2"])
        self.assertGreater(report["warnings"][0]["bytes"], WARN_BYTES)
        # The boundary is on assembled UTF-8, including headings, not source length.
        key = frozen["skills"]["ui"]["source"]
        frozen["files"][key] = ""
        overhead = len(skill_payload(frozen, ["ui"]).encode())
        frozen["files"][key] = "x" * (MAX_BYTES - overhead)
        self.assertEqual(len(skill_payload(frozen, ["ui"]).encode()), MAX_BYTES)
        frozen["files"][key] += "x"
        with self.assertRaisesRegex(ValidationError, "limit"):
            skill_payload(frozen, ["ui"])

    def test_native_frozen_delivery_repeat_claim_and_deleted_live_sources(self):
        directory = self.skill(text="UNIQUE_BOUND_INSTRUCTION\n")
        graph_task = dict(task("TASK-1", "one.txt"), skills=["ui"])
        runtime = self.start([graph_task])
        self.assertTrue(sources_match(load_data(runtime.manifest_path)))
        shutil.rmtree(directory)
        first = claim_task(ROOT, runtime, "TASK-1")
        second = claim_task(ROOT, runtime, "TASK-1")
        self.assertTrue(first["launch_allowed"])
        self.assertFalse(second["launch_allowed"])
        self.assertEqual(first["prompt"].count("UNIQUE_BOUND_INSTRUCTION"), 1)
        self.assertIn("ui", load_run_skills(runtime.root)["skills"])
        self.assertIn("file_skills", load_configuration_snapshot(runtime.configuration_path, ROOT / ".ai/orchestration/schemas")["methodology"])
        with self.assertRaisesRegex(ValidationError, "corrupt"):
            (runtime.root / "skills.json").write_text("{}")
            load_run_skills(runtime.root)

    def test_untracked_skill_cli_executors_and_review_use_same_frozen_text(self):
        self.skill(text="BOUND_WORKER_MARKER\n")
        self.bundle["methodology"]["reviewer_skills"] = ["ui"]
        frozen = self.frozen(self.graph(["ui"]))
        registry = ProviderRegistry(ROOT, self.bundle["providers"], skill_bundle=frozen)
        executors = registry.create_executors(["claude", "cursor"])
        graph_task = dict(task("TASK-1", "one.txt"), skills=["ui"])
        route = {"profile": "fixture", "timeout_seconds": 10, "isolation": "read_only"}
        responses = {
            "claude": {"structured_output": {"summary": "done", "outcome": "completed"}},
            "cursor": {"type": "result", "result": {"summary": "done", "outcome": "completed"}},
        }
        for name, executor in executors.items():
            output = subprocess.CompletedProcess([], 0, json.dumps(responses[name]), "")
            with patch(f"orchestration.executors.{name}.run_process", return_value=(output, 1)) as run:
                result = executor.execute(graph_task, route, {}, self.workspace, 1)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(run.call_args.args[0][-1].count("BOUND_WORKER_MARKER"), 1)
                executor.execute(task("TASK-2", "two.txt"), route, {}, self.workspace, 1)
                self.assertNotIn("BOUND_WORKER_MARKER", run.call_args.args[0][-1])
        reviewer = registry.create_reviewer("claude-review", self.bundle["profiles"])
        review = {"schema_version": "1.0", "status": "pass", "summary": "fine", "issues": []}
        output = subprocess.CompletedProcess([], 0, json.dumps({"structured_output": review}), "")
        with patch("orchestration.review.run_process", return_value=(output, 1)) as run:
            self.assertEqual(reviewer.review(self.graph(["ui"]), [], self.workspace, 1), review)
            command = run.call_args.args[0]
            self.assertIn("Read,Grep,Glob", command)
            self.assertEqual(command[-1].count("BOUND_WORKER_MARKER"), 1)

    def test_native_reviewer_and_generated_repairs_preserve_skill_order(self):
        self.skill(text="TASK_BOUND_MARKER\n")
        self.skill("design", "REVIEW_BOUND_MARKER\n")
        self.bundle["methodology"]["reviewer_skills"] = ["design"]
        runtime = self.start([dict(task("TASK-1", "one.txt"), skills=["ui"])])
        claim = claim_task(ROOT, runtime, "TASK-1")
        (self.workspace / "one.txt").write_text("updated\n")
        self.finish(runtime, claim, agent="task-agent")
        reviewer = session_review(ROOT, runtime)
        self.assertEqual(reviewer["prompt"].count("REVIEW_BOUND_MARKER"), 1)
        review = {"schema_version": "1.0", "status": "remediation_required", "summary": "repair",
                  "issues": [{"id": "REVIEW-UI-1", "severity": "low", "affected_task": "TASK-1",
                              "description": "Repair fixture", "acceptance_criteria": ["Repaired"]}]}
        session_review(ROOT, runtime, {"claim_id": reviewer["claim_id"], "agent_id": "fresh-review",
                                      "review": review}, worker_stopped=True, stop_evidence="review returned")
        graph = load_data(runtime.root / "task-graph.yaml")
        repair = graph["tasks"][-1]
        self.assertEqual(repair["skills"], ["ui"])
        ticket = claim_task(ROOT, runtime, repair["id"])
        self.assertIn("TASK_BOUND_MARKER", ticket["prompt"])
        self.assertNotIn("REVIEW_BOUND_MARKER", ticket["prompt"])
        # CLI and native share apply_review; absence remains absence.
        graph["tasks"][0].pop("skills")
        updated, _ = apply_review(graph, review, self.bundle["profiles"], self.bundle["routing_matrix"],
                                  allowed_executors=["claude"])
        self.assertNotIn("skills", updated["tasks"][-1])

    def test_legacy_run_does_not_gain_live_bindings(self):
        runtime = self.start()
        self.skill(text="NEW_LIVE_SKILL\n")
        self.assertIsNone(load_run_skills(runtime.root))
        self.assertNotIn("NEW_LIVE_SKILL", claim_task(ROOT, runtime, "TASK-1")["prompt"])


class BindingCliTests(unittest.TestCase):
    setUp = cli_fixtures.NativeCliTests.setUp
    cli = cli_fixtures.NativeCliTests.cli

    def configure(self):
        directory = self.workspace / ".claude/skills/ui"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text("CLI_FROZEN_GUIDANCE\n")
        methodology_path = self.config.parent / "methodologies/disciplined.yaml"
        methodology = load_data(methodology_path)
        methodology.update(file_skills={"ui": ".claude/skills/ui"}, reviewer_skills=["ui"])
        atomic_write_json(methodology_path, methodology)
        config = load_data(self.config)
        config.update(execution_strategy="hybrid-default")
        config["control_plane"]["review_profile"] = "claude-review"
        atomic_write_json(self.config, config)
        self.graph["tasks"][0]["skills"] = ["ui"]
        atomic_write_json(self.change / "orchestration/task-graph.yaml", self.graph)
        self.cli("route", "--change", str(self.change), "--config", str(self.config))
        return directory

    def test_doctor_without_graph_and_dry_run_leave_no_runtime(self):
        self.configure()
        with patch("orchestration.cli.diagnose_configured", return_value={"providers": {}}):
            report = self.cli("doctor", "--config", str(self.config))
        self.assertEqual(report["skill_bindings"]["status"], "valid")
        report = self.cli("run", "--change", str(self.change), "--workspace", str(self.workspace),
                          "--config", str(self.config), "--dry-run")
        self.assertIn("skill_bindings", report)
        self.assertFalse((self.workspace / ".ai-runtime").exists())
        # A schema-valid binding references an absent file: doctor explains it, not a provider call.
        shutil.rmtree(self.workspace / ".claude/skills/ui")
        with patch("orchestration.cli.diagnose_configured", return_value={"providers": {}}), redirect_stdout(StringIO()):
            self.assertEqual(main(["doctor", "--config", str(self.config)]), 2)

    def test_cli_run_reopen_resume_uses_frozen_bundle_and_skips_completed_task(self):
        directory = self.configure()
        (self.workspace / "second.txt").write_text("base\n")
        subprocess.run(["git", "add", "second.txt"], cwd=self.workspace, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "-qm", "second fixture"], cwd=self.workspace, check=True, capture_output=True)
        second = copy.deepcopy(self.graph["tasks"][0])
        second.update(id="TASK-2", depends_on=["TASK-1"], scope={"allowed": ["second.txt"], "forbidden": []})
        self.graph["tasks"].append(second)
        atomic_write_json(self.change / "orchestration/task-graph.yaml", self.graph)
        self.cli("route", "--change", str(self.change), "--config", str(self.config))
        calls = []

        def worker(command, workspace, *args):
            prompt = command[-1]
            self.assertEqual(prompt.count("CLI_FROZEN_GUIDANCE"), 1)
            identity = "TASK-2" if "Task: TASK-2" in prompt else "TASK-1"
            calls.append(identity)
            failed = identity == "TASK-2" and calls.count(identity) == 1
            if not failed:
                (workspace / ("second.txt" if identity == "TASK-2" else "file.txt")).write_text(identity + "\n")
            response = {"type": "result", "result": {"summary": "fixture", "outcome": "failed" if failed else "completed"}}
            return subprocess.CompletedProcess([], 0, json.dumps(response), ""), 1

        review = {"schema_version": "1.0", "status": "pass", "summary": "verified", "issues": []}
        with patch("orchestration.executors.cursor.run_process", side_effect=worker), patch(
            "orchestration.review.run_process", return_value=(subprocess.CompletedProcess(
                [], 0, json.dumps({"structured_output": review}), ""), 1)) as reviewer:
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["run", "--change", str(self.change), "--workspace", str(self.workspace),
                                       "--no-worktrees", "--config", str(self.config)]), 2)
            failed = json.loads(output.getvalue())
            self.assertEqual(failed["tasks"]["TASK-1"]["status"], "completed")
            run_id = failed["run_id"]
            self.cli("reopen", run_id, "TASK-2", "--workspace", str(self.workspace),
                     "--reason", "Fixture remaining task", "--checkpoint", "Complete remaining work")
            shutil.rmtree(directory)
            completed = self.cli("resume", run_id, "--workspace", str(self.workspace))
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(calls, ["TASK-1", "TASK-2", "TASK-2"])
            self.assertIn("CLI_FROZEN_GUIDANCE", reviewer.call_args.args[0][-1])
            report_path = self.workspace / ".ai-runtime" / self.graph["workflow"]["id"] / run_id / "run-report.md"
            self.assertIn("Frozen skill assignments", report_path.read_text())


if __name__ == "__main__":
    unittest.main()
