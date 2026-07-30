"""Structured JSONL run logging, uploaded to a public URL.

Two backends, chosen with LOG_BACKEND:
  "github" (default) -- commits to a public repo, served via raw.githubusercontent.com.
                        Permanent URLs. Needs GITHUB_TOKEN + GITHUB_REPO.
  "gcs"              -- uploads to a public GCS bucket. Needs credentials that the
                        host can actually obtain (blocked on projects that enforce
                        constraints/iam.disableServiceAccountKeyCreation).
"""

import base64
import datetime
import json
import os
import tempfile
import uuid

import requests

BACKEND = os.environ.get("LOG_BACKEND", "github").strip().lower()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")  # "owner/repo"
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_LOG_DIR = os.environ.get("GITHUB_LOG_DIR", "logs")

BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", "")


class RunLogger:
    """One instance per incoming question. Appends JSON lines to a temp file."""

    def __init__(self, chat_id):
        self.chat_id = str(chat_id)
        self.run_id = uuid.uuid4().hex
        self.object_name = f"{self.chat_id}-{self.run_id}.jsonl"
        fd, self.path = tempfile.mkstemp(prefix=f"run-{self.run_id}-", suffix=".jsonl")
        os.close(fd)

    def log(self, step, **content):
        """Append one structured line: step type, timestamp, content."""
        record = {
            "step": step,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "run_id": self.run_id,
            "chat_id": self.chat_id,
        }
        record.update(content)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def upload(self):
        """Upload the log and return its public HTTPS URL."""
        if BACKEND == "gcs":
            return self._upload_gcs()
        return self._upload_github()

    def _upload_github(self):
        if not (GITHUB_TOKEN and GITHUB_REPO):
            raise RuntimeError("LOG_BACKEND=github needs GITHUB_TOKEN and GITHUB_REPO")
        with open(self.path, "rb") as fh:
            payload = base64.b64encode(fh.read()).decode("ascii")

        target = f"{GITHUB_LOG_DIR}/{self.object_name}"
        resp = requests.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{target}",
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            json={
                "message": f"log: run {self.run_id} (chat {self.chat_id})",
                "content": payload,
                "branch": GITHUB_BRANCH,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return (
            f"https://raw.githubusercontent.com/{GITHUB_REPO}/"
            f"{GITHUB_BRANCH}/{target}"
        )

    def _upload_gcs(self):
        from google.cloud import storage
        from google.oauth2 import service_account

        raw = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "").strip()
        if raw:
            info = json.loads(raw)
            creds = service_account.Credentials.from_service_account_info(info)
            client = storage.Client(project=info.get("project_id"), credentials=creds)
        else:
            client = storage.Client()

        blob = client.bucket(BUCKET_NAME).blob(self.object_name)
        blob.upload_from_filename(self.path, content_type="application/x-ndjson")
        return f"https://storage.googleapis.com/{BUCKET_NAME}/{self.object_name}"
