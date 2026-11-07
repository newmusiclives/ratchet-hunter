"""Alerts that reach a human: GitHub issues, which GitHub emails to everyone
watching the repository (and to anyone listed in `alert_assignees`).

When the Hunter runs on GitHub Actions it has GITHUB_TOKEN and GITHUB_REPOSITORY.
Anywhere else, alerts are written to reports/alerts.log instead.
"""
import datetime as dt
import json
import os
from pathlib import Path

import requests

API = "https://api.github.com"


class Alerts:
    def __init__(self, cfg, log_dir):
        self.token = os.environ.get("GITHUB_TOKEN")
        self.repo = os.environ.get("GITHUB_REPOSITORY")
        self.assignees = [a for a in cfg.get("alert_assignees", []) if a]
        self.log = Path(log_dir) / "alerts.log"
        self.enabled = bool(self.token and self.repo)

    def _h(self):
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json"}

    def _write_local(self, title, body):
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open("a") as f:
            f.write(f"\n=== {dt.datetime.now():%Y-%m-%d %H:%M} {title}\n{body}\n")
        return f"local:{title}"

    def open(self, title, body, labels=("hunter",)):
        """Create an alert; returns a reference used later to check whether it was closed."""
        if not self.enabled:
            return self._write_local(title, body)
        payload = {"title": title, "body": body, "labels": list(labels)}
        if self.assignees:
            payload["assignees"] = self.assignees
        r = requests.post(f"{API}/repos/{self.repo}/issues", headers=self._h(), json=payload, timeout=30)
        if r.status_code in (403, 422):   # assignee not a collaborator, or labels not allowed: plain issue
            r = requests.post(f"{API}/repos/{self.repo}/issues", headers=self._h(),
                              json={"title": title, "body": body}, timeout=30)
        r.raise_for_status()
        return str(r.json()["number"])

    def comment(self, ref, body):
        if not self.enabled or not ref or ref.startswith("local:"):
            return self._write_local(f"comment on {ref}", body)
        requests.post(f"{API}/repos/{self.repo}/issues/{ref}/comments", headers=self._h(),
                      json={"body": body}, timeout=30).raise_for_status()

    def is_closed(self, ref):
        """True once the person has closed the alert (that is how they confirm the tender)."""
        if not self.enabled or not ref or ref.startswith("local:"):
            return False
        r = requests.get(f"{API}/repos/{self.repo}/issues/{ref}", headers=self._h(), timeout=30)
        r.raise_for_status()
        return r.json().get("state") == "closed"

    def close(self, ref, comment=None):
        if not self.enabled or not ref or ref.startswith("local:"):
            return
        if comment:
            self.comment(ref, comment)
        requests.patch(f"{API}/repos/{self.repo}/issues/{ref}", headers=self._h(),
                       json={"state": "closed"}, timeout=30).raise_for_status()


def tender_instructions(deal, shares, broker_name):
    exp = deal["expiration"]
    return f"""**Action needed: tender {shares} shares of {deal['ticker']} before {exp}.**

The Hunter bought {shares} shares of **{deal['company']} ({deal['ticker']})**. The company is buying shares back at **${deal['payout']:.2f}**, and holders of fewer than 100 shares are bought in full.

**What to do (about 2 minutes):**
1. Log in to {broker_name}.
2. Find the voluntary corporate action or tender offer for {deal['ticker']}. It's often under *Corporate actions*, *Account → Elections*, or ask support to "tender all my {deal['ticker']} shares into the odd-lot tender offer".
3. Tender **all {shares} shares** and tick the **odd lot** box if one is offered.
4. Come back here and **close this issue** (button at the bottom). That tells the Hunter the tender is done.

The offer document: {deal.get('url', '')}

If this isn't closed by {exp}, the Hunter will treat the shares as not tendered and sell them at the market price after the offer ends.
"""
