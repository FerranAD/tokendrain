# Codex integration notes

Current setup and protocol documentation: [Codex authentication](openai-auth.md), [guest protocol](guest-protocol.md), and [execution lifecycle](architecture.md).

The retired SIWC/OAuth implementation is removed. Codex auth.json import uses host-side Codex refresh and runtime-only access tokens in guests. Only final-answer messages produce checkpoints; commentary stays activity. Transport recovery and credential renewal preserve the last valid model report.

Validation covers credential encryption, serialized refresh, guest rotation, final-answer parsing, bounded interruption, and persistent thread recovery. Live provider/GitHub-account tests remain opt-in.
