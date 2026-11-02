"""Notify on role requests even when issue-form labels are missing."""

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SENT_LABEL = "role-request-slack-notified"
ROLES = {
    "triage-request": (
        "triage request",
        "🔎",
        "Review the request, then add the requester to the triagers team in peribolos.yaml.",
    ),
    "write-request": (
        "write request",
        "✍️",
        "Two maintainers must approve on the request before opening a peribolos.yaml PR.",
    ),
    "maintainer-request": (
        "maintainer nomination",
        "🛠️",
        "Maintainer consensus is required before adding the requester to maintainers in peribolos.yaml.",
    ),
}


def classify(issue):
    roles = {label["name"] for label in issue["labels"]} & ROLES.keys()
    if len(roles) > 1:
        raise ValueError("Multiple role request labels; select one before notifying")
    if roles:
        return next(iter(roles))
    for role, (title, _, _) in ROLES.items():
        if issue["title"].lower().startswith(title + ":"):
            return role
    return None


def github_api(method, path, data=None):
    request = Request(
        "https://api.github.com/repos/fullsend-ai/org" + path,
        data=None if data is None else json.dumps(data).encode(),
        headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def send_slack(payload):
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        raise RuntimeError("SLACK_WEBHOOK_URL secret is not configured")
    request = Request(
        webhook,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            if response.read().strip() != b"ok":
                raise RuntimeError("Slack did not acknowledge the notification")
    except (HTTPError, URLError) as error:
        if isinstance(error, HTTPError):
            error.close()
        # Do not include the webhook URL in error output.
        raise RuntimeError("Slack notification failed") from None


def rollout_time():
    """Use the first-parent addition, so a merge uses deployment time, not PR authorship."""
    result = subprocess.run(
        [
            "git",
            "log",
            "-1",
            "--first-parent",
            "--diff-filter=A",
            "--format=%ct",
            "--",
            "scripts/notify_role_request.py",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return datetime.fromtimestamp(int(result.stdout.strip()), UTC)


def notify(number, api=github_api, post=send_slack, *, automatic_since=None):
    issue = api("GET", f"/issues/{number}")
    if "pull_request" in issue:
        raise ValueError("Expected an issue, not a pull request")
    if any(label["name"] == SENT_LABEL for label in issue["labels"]):
        print(f"Issue #{number}: already notified")
        return
    role = classify(issue)
    if role is None:
        print(f"Issue #{number}: not a role request")
        return
    if automatic_since is not None:
        created_at = datetime.fromisoformat(issue["created_at"])
        if created_at <= automatic_since:
            print(
                f"Issue #{number}: predates notifier rollout; check delivery before manual backfill"
            )
            return
    author = issue["user"]["login"]
    if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", author):
        raise ValueError("Invalid issue author")
    url = f"https://github.com/fullsend-ai/org/issues/{number}"
    label, emoji, context = ROLES[role]
    try:
        api("GET", f"/labels/{SENT_LABEL}")
    except HTTPError as error:
        error.close()
        if error.code != 404:
            raise
        try:
            api("POST", "/labels", {"name": SENT_LABEL, "color": "ededed"})
        except HTTPError as error:
            error.close()
            if error.code != 422:
                raise
            api("GET", f"/labels/{SENT_LABEL}")
    post(
        {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{emoji} *New {label}* from *{author}* — <{url}|View issue>",
                    },
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": context}],
                },
            ]
        }
    )
    api("POST", f"/issues/{number}/labels", {"labels": [SENT_LABEL]})
    print(f"Issue #{number}: Slack notification acknowledged and recorded")


def issue_number(event):
    value = event.get("issue", {}).get("number") or event.get("inputs", {}).get("issue_number")
    if not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ValueError("A positive issue number is required")
    return int(value)


if __name__ == "__main__":
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    since = None if os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch" else rollout_time()
    notify(issue_number(event), automatic_since=since)
