"""Ingest latency benchmark harness (ADR 013).

Sibling to ``tests/eval/latency/`` (chat-path latency) and
``tests/eval/conversations/`` (conversation-eval quality). Times each
stage of the document-ingest pipeline (PDF extract → section detect →
chunk → embed → entity extract) plus end-to-end wall clock against a
fixed fixture so the bottleneck is visible per-stage and improvements
are gated under the same paired-bootstrap CI as the chat harness.
"""
