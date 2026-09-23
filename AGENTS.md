# vroxy_dispatch

**This file is `AGENTS.md`; `CLAUDE.md` is a symlink to it.**

Long-running ActionCable agent that answers dispatch-enabled rooms
and the dogfood feedback loop. Sibling of `vroxy_web` under
`/home/ubuntu/code/vroxy/`. Parent workspace rules in
`../AGENTS.md` apply too.

This file is what every harness on this box reads when it works
here — Claude Code, Cursor Agent, Codex, Gemini CLI, OpenCode,
Amp, Copilot CLI. Rules below apply no matter which
`DISPATCH_ENGINE` is live.

## Skills in this repo

| Skill | When |
| --- | --- |
| `restart_dispatch` | Any time this process must reload — engine flip, forced restart, hung cable. **Never** a bare `systemctl restart` from inside a turn. |

Real files live under `.claude/skills/<name>/` (Claude Code /
Cursor) and are mirrored under `.agents/skills/<name>/` (OpenCode,
Amp, Gemini workspace skill roots). Symlink each into
`~/.claude/skills/<name>` so a session whose cwd is `vroxy_web`
(the usual room work dir) still finds them.

## Restarting yourself

Use the **`restart_dispatch`** skill. Read it before you touch
systemd — every harness, not only Claude.

The failure mode it exists to prevent: `systemctl restart` from a
room turn kills the unit cgroup, which kills the running harness
(`claude` / `cursor-agent` / `codex` / `gemini` / `opencode` /
`amp` / `copilot`) mid-reply. The room then sees
"A restart interrupted me…". The skill arms a delayed
`systemd-run` timer outside the cgroup instead — same path
`feedback_agent.schedule_restart` uses for self-update after a
shipped edit.

Prefer letting self-update fire after the turn when you only
changed this checkout. Force a restart with the skill when the
operator asks, when flipping `DISPATCH_ENGINE`, or when the live
process is wedged.

## Stream parsers

`feedback_agent.HARNESS_SPECS` drives every engine except the
bespoke Claude and Codex runners. Progress lines (tool names,
thinking) only look right when the parser matches the CLI's real
stream — verify against a live run before flipping
`verified: True`.

## Quick reference

| Task | How |
| --- | --- |
| Safe restart | `restart_dispatch` skill |
| Flip harness | edit `/etc/default/vroxy-dispatch` `DISPATCH_ENGINE=…`, then `restart_dispatch` |
| Self-update after shipping | automatic via `restart_if_self_updated` once the turn ends |
| Install / pull all units | `./install.sh --update` |
