"""Vercel serverless entrypoint. @vercel/python serves the exported WSGI `app`."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import app  # noqa: E402,F401  - Vercel looks for `app`
