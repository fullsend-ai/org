"""Mocked Slack notifier regressions: python3 -m unittest discover -s scripts."""

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import notify_role_request as notifier


def issue(title="Write request: durandom", labels=()):
    return {
        "title": title,
        "labels": [{"name": name} for name in labels],
        "user": {"login": "durandom"},
        "created_at": "2026-10-30T16:44:25Z",
    }


class NotificationTests(unittest.TestCase):
    def test_issue_and_manual_dispatch_inputs(self):
        self.assertEqual(notifier.issue_number({"issue": {"number": 34}}), 34)
        self.assertEqual(notifier.issue_number({"inputs": {"issue_number": "34"}}), 34)

    def test_invalid_issue_numbers_are_rejected(self):
        for value in (None, 0, -1, "1/labels", "1.5", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                notifier.issue_number({"inputs": {"issue_number": value}})

    def notify(self, request, api=None, post=None, **kwargs):
        api = api or Mock(return_value=request)
        post = post or Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            notifier.notify(34, api, post, **kwargs)
        return api, post

    def test_legacy_requests_are_not_automatically_renotified(self):
        since = datetime(2026, 10, 31, tzinfo=UTC)
        for labels in ((), ("write-request",)):
            with self.subTest(labels=labels):
                api, post = self.notify(issue(labels=labels), automatic_since=since)
                post.assert_not_called()
                self.assertEqual(api.call_count, 1)

    def test_legacy_missed_request_can_be_manually_backfilled(self):
        _, post = self.notify(issue(), automatic_since=None)
        post.assert_called_once()

    def test_new_requests_can_notify_after_rollout(self):
        _, post = self.notify(issue(), automatic_since=datetime(2026, 10, 30, tzinfo=UTC))
        post.assert_called_once()

    def test_rollout_boundary_is_conservative(self):
        since = datetime.fromisoformat(issue()["created_at"])
        _, post = self.notify(issue(), automatic_since=since)
        post.assert_not_called()

    def test_rollout_time_uses_first_parent_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {
                **os.environ,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
                "GIT_AUTHOR_DATE": "2026-10-28T00:00:00Z",
                "GIT_COMMITTER_DATE": "2026-10-28T00:00:00Z",
            }

            run = subprocess.run

            def git(*args):
                return run(
                    ["git", *args], cwd=root, env=env, check=True, capture_output=True, text=True
                )

            git("init", "-b", "main")
            git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "Initial")
            git("checkout", "-b", "feature")
            (root / "scripts").mkdir()
            (root / "scripts/notify_role_request.py").touch()
            git("add", ".")
            git("-c", "commit.gpgsign=false", "commit", "-m", "Add notifier")
            git("checkout", "main")
            env["GIT_COMMITTER_DATE"] = "2026-10-31T00:00:00Z"
            git("-c", "commit.gpgsign=false", "merge", "--no-ff", "feature", "-m", "Deploy")
            # Keep the tested command unchanged while supplying the temporary checkout as cwd.
            with patch.object(
                notifier.subprocess, "run", side_effect=lambda *a, **k: run(*a, cwd=root, **k)
            ):
                self.assertEqual(notifier.rollout_time(), datetime(2026, 10, 31, tzinfo=UTC))
                (root / "scripts/notify_role_request.py").write_text("# Later update\n")
                env["GIT_COMMITTER_DATE"] = "2026-11-01T00:00:00Z"
                git("add", ".")
                git("-c", "commit.gpgsign=false", "commit", "-m", "Update notifier")
                self.assertEqual(notifier.rollout_time(), datetime(2026, 10, 31, tzinfo=UTC))

    def test_missing_rollout_history_fails_closed(self):
        with patch.object(notifier.subprocess, "run", return_value=Mock(stdout="")):
            with self.assertRaises(ValueError):
                notifier.rollout_time()

    def test_missing_label_regression(self):
        api, post = self.notify(issue())
        text = post.call_args.args[0]["blocks"][0]["text"]["text"]
        self.assertIn("New write request", text)
        self.assertIn("https://github.com/fullsend-ai/org/issues/34", text)
        api.assert_called_with("POST", "/issues/34/labels", {"labels": [notifier.SENT_LABEL]})

    def test_all_template_titles_work_without_labels(self):
        for title, _, _ in notifier.ROLES.values():
            with self.subTest(title=title):
                _, post = self.notify(issue(title + ": durandom"))
                post.assert_called_once()

    def test_label_added_after_creation_works_without_template_title(self):
        _, post = self.notify(issue("Request access", ["triage-request"]))
        self.assertIn("New triage request", post.call_args.args[0]["blocks"][0]["text"]["text"])

    def test_ordinary_issues_do_not_notify(self):
        for title in (
            "Bug report",
            "Write request notification broken",
            "Maintainer template outdated",
        ):
            with self.subTest(title=title):
                api, post = self.notify(issue(title))
                post.assert_not_called()
                self.assertEqual(api.call_count, 1)

    def test_repeated_event_or_manual_dispatch_does_not_duplicate(self):
        api, post = self.notify(issue(labels=[notifier.SENT_LABEL]))
        post.assert_not_called()
        self.assertEqual(api.call_count, 1)

    def test_failed_slack_delivery_is_not_marked_sent(self):
        api = Mock(return_value=issue())
        with self.assertRaisesRegex(RuntimeError, "delivery failed"):
            self.notify(issue(), api, Mock(side_effect=RuntimeError("delivery failed")))
        self.assertFalse(any(call.args[0] == "POST" for call in api.call_args_list))

    def test_missing_marker_label_is_created_before_delivery(self):
        api = Mock(side_effect=[issue(), HTTPError("", 404, "", {}, None), {}, {}])
        self.notify(issue(), api)
        self.assertEqual(api.call_args_list[2].args[:2], ("POST", "/labels"))

    def test_concurrent_marker_creation_is_tolerated(self):
        api = Mock(
            side_effect=[
                issue(),
                HTTPError("", 404, "", {}, None),
                HTTPError("", 422, "", {}, None),
                {},
                {},
            ]
        )
        _, post = self.notify(issue(), api)
        post.assert_called_once()

    def test_ambiguous_roles_fail_without_notifying(self):
        post = Mock()
        with self.assertRaisesRegex(ValueError, "Multiple role"):
            self.notify(issue(labels=["triage-request", "write-request"]), post=post)
        post.assert_not_called()

    def test_pull_requests_are_rejected(self):
        request = issue()
        request["pull_request"] = {}
        with self.assertRaisesRegex(ValueError, "not a pull request"):
            self.notify(request)

    def test_slack_failure_does_not_expose_webhook(self):
        with (
            patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://example.com/private"}),
            patch.object(notifier, "urlopen", side_effect=URLError("https://example.com/private")),
            self.assertRaisesRegex(RuntimeError, "^Slack notification failed$") as caught,
        ):
            notifier.send_slack({})
        self.assertTrue(caught.exception.__suppress_context__)

    def test_slack_must_acknowledge_delivery(self):
        response = Mock()
        response.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b"not_ok")))
        response.__exit__ = Mock(return_value=False)
        with (
            patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://example.com/private"}),
            patch.object(notifier, "urlopen", return_value=response),
            self.assertRaisesRegex(RuntimeError, "did not acknowledge"),
        ):
            notifier.send_slack({})


if __name__ == "__main__":
    unittest.main()
