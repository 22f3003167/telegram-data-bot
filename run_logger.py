"""Structured JSONL run logging with upload to a public GCS bucket."""

import datetime
import json
import os
import tempfile
import uuid

from google.cloud import storage
from google.oauth2 import service_account

BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", "")


def _client():
    """GCS client. Uses inline service-account JSON on Render, ADC locally."""
    raw = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(info)
        return storage.Client(project=info.get("project_id"), credentials=creds)
    return storage.Client()


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
        bucket = _client().bucket(BUCKET_NAME)
        blob = bucket.blob(self.object_name)
        blob.upload_from_filename(self.path, content_type="application/x-ndjson")
        return f"https://storage.googleapis.com/{BUCKET_NAME}/{self.object_name}"
