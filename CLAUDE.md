# Turnaround

Repo orientation, invariants and commands live in **[`AGENTS.md`](AGENTS.md)**.
Read that first; it is kept vendor-neutral so every tool sees the same guidance.

The deployed agent must remain **Gemini/Vertex only** — no non-Google AI SDK may
appear on the runtime import path. Coding assistants are a development tool and
are fine; a runtime dependency on one is not, and a test enforces it.
