# Changelog

## 0.1.0

- Initial port from `vroxy_dispatch/feedback_agent.py`.
- Adapted to ctovibe_web's tenant-scoped `AdminFeedbackChannel`:
  service auth via `Tenant`-owned `ApiToken` (`platform:dispatch` or
  `full` scope); one dispatch process = one tenant; hashid chat/
  message ids on the wire; heartbeat cached under
  `ctovibe_dispatch:heartbeat:<tenant.id>` with a 60 s TTL.
- Handles `feedback.created`, `feedback.followup`, `approve.requested`.
- Streamed Claude output forwarded as live tool chips via `progress`;
  falls back to `claude-chat` (non-streamed) if the stream flag
  breaks.
- Inline-ship path bumps `version_bump.sh` when present (matches
  ctovibe_web layout); PR path branches + `gh pr create`s.
- `bin/claude-chat` copied verbatim from vroxy_dispatch (project-
  agnostic).
- 8 unit tests (prompt build + proposal parse).
