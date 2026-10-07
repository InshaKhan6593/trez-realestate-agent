"""Tests never send traces to the real Langfuse project: the keys in .env are
blanked before anything loads them (load_dotenv does not override variables
that are already set). test_tracing.py records spans with its own in-memory client.

Scripted model tests script one extractor reading per turn; the combining of
several readings is tested on its own (test_agent_graph.py)."""

import os

os.environ["LANGFUSE_PUBLIC_KEY"] = ""
os.environ["LANGFUSE_SECRET_KEY"] = ""
os.environ["EXTRACTOR_READINGS"] = "1"
