import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from executor.executor import GmailClient, WorkflowExecutor, extract_email, resolve, unresolved_tokens
from shared.events import WorkflowSpec


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.executor = WorkflowExecutor({"demo_mode": True, "dry_run": True, "download_dir": "./test-downloads"})

    def test_empty_workflow_fails(self):
        result = self.executor.execute(WorkflowSpec("", {}, [], [], {}))
        self.assertFalse(result.success)

    def test_dry_run_reports_success(self):
        spec = WorkflowSpec("Test", {}, [{"step": 1, "action": "send_slack_notification", "target": "slack", "params": {"text": "done"}}], [], {})
        result = self.executor.execute(spec)
        self.assertTrue(result.success)
        self.assertTrue(result.outputs["step_1"]["dry_run"])

    def test_execute_emits_one_trace_per_action(self):
        spec = WorkflowSpec("Test", {}, [
            {"step": 1, "action": "download_attachment", "target": "gmail", "params": {"filename": "proof.pdf"}},
            {"step": 2, "action": "lookup_customer", "target": "crm", "params": {"email": "a@b.co"}},
            {"step": 3, "action": "update_customer", "target": "crm", "params": {"email": "a@b.co", "note": "n"}},
            {"step": 4, "action": "send_slack_notification", "target": "slack", "params": {"text": "hi"}},
        ], [], {})
        result = self.executor.execute(spec)

        self.assertTrue(result.success)
        self.assertEqual(len(result.steps), 4)
        for index, step in enumerate(result.steps, start=1):
            self.assertEqual(step["step"], index)
            self.assertEqual(step["status"], "ok")
            self.assertTrue(step["summary"], f"step {index} has no human-readable summary")
            self.assertNotIn("summary", step["evidence"], "summary must not leak into evidence")
            self.assertIsInstance(step["duration_ms"], int)

    def test_download_step_emits_downloadable_artifact(self):
        spec = WorkflowSpec("Test", {}, [
            {"step": 1, "action": "download_attachment", "target": "gmail", "params": {"filename": "proof.pdf"}},
        ], [], {})
        result = self.executor.execute(spec)

        evidence = result.steps[0]["evidence"]
        self.assertTrue(evidence["filepath"].endswith("proof.pdf"))
        self.assertGreater(evidence["bytes"], 0, "proof artifact must not be empty")
        with open(evidence["filepath"], "rb") as handle:
            self.assertTrue(handle.read(5).startswith(b"%PDF"), "artifact must be a real PDF")

    def test_failure_halts_and_is_recorded(self):
        spec = WorkflowSpec("Test", {}, [
            {"step": 1, "action": "send_slack_notification", "target": "slack", "params": {"text": "one"}},
            {"step": 2, "action": "teleport", "target": "nowhere", "params": {}},
            {"step": 3, "action": "send_slack_notification", "target": "slack", "params": {"text": "three"}},
        ], [], {})
        result = self.executor.execute(spec)

        self.assertFalse(result.success)
        self.assertEqual(len(result.steps), 2, "execution must stop at the failing action")
        self.assertEqual(result.steps[0]["status"], "ok")
        self.assertEqual(result.steps[1]["status"], "failed")
        self.assertIn("teleport", result.steps[1]["summary"])


class VariableResolutionTests(unittest.TestCase):
    """The spec must describe a class of work, not one recorded run."""

    def test_extract_email_from_display_name_header(self):
        self.assertEqual(
            extract_email('"Jane Doe" <jane.doe@acme-corp.com>'),
            "jane.doe@acme-corp.com")

    def test_resolve_substitutes_nested_structures(self):
        context = {"sender_email": "real@acme.com", "subject": "Invoice 42", "attachment_name": "inv.pdf"}
        params = {
            "email": "{{sender_email}}",
            "text": "from {{sender_email}}: {{subject}} ({{ attachment_name }})",
            "list": ["{{subject}}", "literal"],
        }
        got = resolve(params, context)
        self.assertEqual(got["email"], "real@acme.com")
        self.assertEqual(got["text"], "from real@acme.com: Invoice 42 (inv.pdf)")
        self.assertEqual(got["list"], ["Invoice 42", "literal"])

    def test_resolve_leaves_unknown_tokens_and_reports_them(self):
        got = resolve({"email": "{{sender_email}}"}, {"subject": "only this"})
        self.assertEqual(got["email"], "{{sender_email}}")
        self.assertEqual(unresolved_tokens(got), ["sender_email"])

    def test_empty_context_does_not_blank_out_value(self):
        self.assertEqual(resolve({"e": "{{sender_email}}"}, {"sender_email": ""})["e"], "{{sender_email}}")

    def test_unresolved_token_fails_the_step_visibly(self):
        # demo_mode is off, dry_run on: nothing external is touched, but a spec
        # that references a variable the run never produced must not pass silently.
        executor = WorkflowExecutor({"demo_mode": False, "dry_run": True, "download_dir": "./test-downloads"})
        spec = WorkflowSpec("Test", {}, [
            {"step": 1, "action": "lookup_customer", "target": "crm", "params": {"email": "{{never_set}}"}},
        ], [], {})
        result = executor.execute(spec)
        self.assertFalse(result.success)
        self.assertIn("never_set", result.steps[0]["summary"])

    def test_demo_run_publishes_sender_into_context(self):
        executor = WorkflowExecutor({"demo_mode": True, "dry_run": True, "download_dir": "./test-downloads"})
        spec = WorkflowSpec("Test", {}, [
            {"step": 1, "action": "download_attachment", "target": "gmail", "params": {"email_from": "someone@real.co"}},
            {"step": 2, "action": "lookup_customer", "target": "crm", "params": {"email": "{{sender_email}}"}},
            {"step": 3, "action": "send_slack_notification", "target": "slack",
             "params": {"text": "handled {{subject}} from {{sender_email}}"}},
        ], [], {})
        result = executor.execute(spec)
        self.assertTrue(result.success, result.message)

        # step 1 discovered the sender; steps 2 and 3 must consume that runtime
        # value rather than any literal baked into the spec.
        self.assertEqual(result.context["sender_email"], "someone@real.co")
        self.assertIn("someone@real.co", result.steps[1]["summary"])
        self.assertIn("someone@real.co", result.steps[2]["summary"])
        self.assertNotIn("{{", result.steps[2]["summary"])


# Shaped like a real Gmail API 'format=full' response so header parsing and
# nested-attachment walking are verified without touching a live mailbox.
REAL_GMAIL_MESSAGE = {
    "id": "18f2a1b2c3d4e5f6",
    "snippet": "Please find the signed contract attached.",
    "payload": {
        "headers": [
            {"name": "From", "value": '"Jane Doe" <jane.doe@acme-corp.com>'},
            {"name": "Subject", "value": "Contract review - Acme Q3"},
            {"name": "Date", "value": "Mon, 22 Sep 2025 10:14:02 +0530"},
        ],
        "parts": [
            {"mimeType": "text/plain", "body": {"size": 120}},
            {"mimeType": "application/pdf", "filename": "contract.pdf",
             "body": {"attachmentId": "ATTACH1", "size": 48213}},
            {"mimeType": "application/vnd.ms-excel", "filename": "pricing.xlsx",
             "body": {"attachmentId": "ATTACH2", "size": 9021}},
        ],
    },
}


class GmailParsingTests(unittest.TestCase):
    def setUp(self):
        self.gmail = GmailClient("creds.json", "token.json")

    def test_describe_extracts_real_identity_fields(self):
        meta = self.gmail.describe(REAL_GMAIL_MESSAGE)
        self.assertEqual(meta["sender_email"], "jane.doe@acme-corp.com")
        self.assertEqual(meta["sender_name"], "Jane Doe")
        self.assertEqual(meta["subject"], "Contract review - Acme Q3")
        self.assertIn("Mon, 22 Sep 2025", meta["date"])
        self.assertIn("signed contract", meta["snippet"])

    def test_saves_every_attachment_with_a_traceable_name(self):
        executor = WorkflowExecutor({"demo_mode": True, "dry_run": True, "download_dir": "./test-downloads"})

        def fake_download(message_id, attachment_id, filename, save_dir):
            path = os.path.join(save_dir, filename)
            with open(path, "wb") as handle:
                handle.write(b"%PDF-1.4 fake")
            return path

        executor.gmail.download_attachment = fake_download
        saved = executor._save_attachments(REAL_GMAIL_MESSAGE, "Contract review - Acme Q3")

        self.assertEqual(len(saved), 2, "both attachments must be saved")
        names = [name for _, name in saved]
        self.assertTrue(any(n.endswith("contract.pdf") for n in names), names)
        self.assertTrue(any(n.endswith("pricing.xlsx") for n in names), names)
        # The subject is embedded so a judge can trace file -> email.
        self.assertTrue(all("Contract_review" in n for n in names), names)
        for path, _ in saved:
            self.assertTrue(os.path.exists(path))

    def test_message_without_attachment_reports_clearly(self):
        message = {"id": "x", "payload": {"headers": [
            {"name": "From", "value": "a@b.com"}, {"name": "Subject", "value": "hi"}],
            "parts": [{"mimeType": "text/plain", "body": {"size": 10}}]}}
        executor = WorkflowExecutor({"demo_mode": True, "dry_run": True, "download_dir": "./test-downloads"})
        self.assertEqual(executor._save_attachments(message, "hi"), [])

    def test_many_attachments_do_not_break_unpacking(self):
        # A newsletter with 20 attachments must not be mistaken for a tuple return.
        message = {"id": "big", "payload": {"headers": [
            {"name": "From", "value": "news@example.com"},
            {"name": "Subject", "value": "Weekly digest"}],
            "parts": [{"mimeType": "image/png", "filename": f"img{i}.png",
                       "body": {"attachmentId": f"A{i}", "size": 100}} for i in range(20)]}}
        executor = WorkflowExecutor({"demo_mode": True, "dry_run": True, "download_dir": "./test-downloads"})

        def fake_download(message_id, attachment_id, filename, save_dir):
            path = os.path.join(save_dir, filename)
            with open(path, "wb") as handle:
                handle.write(b"x")
            return path

        executor.gmail.download_attachment = fake_download
        saved = executor._save_attachments(message, "Weekly digest")

        self.assertEqual(len(saved), 20)
        self.assertIsInstance(saved, list)
        for path, name in saved:
            self.assertIsInstance(path, str)
            self.assertIsInstance(name, str)


if __name__ == "__main__":
    unittest.main()
