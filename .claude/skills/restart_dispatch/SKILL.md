---
name: restart_dispatch
description: Safely restart a vroxy_dispatch systemd unit without killing the turn mid-reply. Use whenever dispatch needs to reload itself (engine flip, shipped self-update, hung cable, operator ask) — never a bare systemctl restart from inside the unit.
user-invocable: true
---

Restart the dispatch agent from **outside its own cgroup**.

A bare `systemctl restart` issued from a room turn kills the whole
unit cgroup — including the running `claude`/`cursor-agent`/`codex`
process and the shell that asked for the restart. That is how a
reply dies mid-sentence and the room sees
"A restart interrupted me…". Always arm a delayed `systemd-run`
timer instead. Same recipe `feedback_agent.schedule_restart` uses
for self-update.

## When to use

- You just shipped a change under `vroxy_dispatch/` and need the
  live process on the new code (self-update usually does this after
  the turn — prefer that; only run this skill if you must force it).
- Flipping `DISPATCH_ENGINE`, fixing a hung cable, or an operator
  asked you to restart.
- Never for unrelated work. Never from inside a proposal worktree
  as a substitute for finishing the answer first.

## Steps

1. **Finish the room reply first** when you are mid-turn. Arm the
   timer as the last action; do not keep tooling after it.

2. **Pick the unit.** Prefer `$VROXY_DISPATCH_UNIT` from the
   process environment. Defaults on this box:
   - dogfood / vroxy → `vroxy-dispatch-feedback-agent.service`
   - template installs → `vroxy-dispatch@<id>.service`
   Confirm with `systemctl is-active <unit>` before arming.

3. **Byte-compile** so a SyntaxError cannot crash-loop systemd:

   ```bash
   python3 -m py_compile feedback_agent.py
   ```

   Run from `/home/ubuntu/code/vroxy/vroxy_dispatch`. If it fails,
   say so in the room and do **not** restart.

4. **Optional restart notice** so the next process can post
   "✅ Back up on … — was …". Write
   `$VROXY_DISPATCH_STATE_DIR/restart-notice.json` (default
   `~/.cache/vroxy-dispatch/restart-notice.json`):

   ```json
   {"room_id":"<hashid>","reply_to":"<message hashid or null>",
    "from_version":"<AGENT_VERSION now>","to_version":"<expected>",
    "at":<unix epoch>}
   ```

5. **Arm the delayed restart** (passwordless sudo):

   ```bash
   UNIT="${VROXY_DISPATCH_UNIT:-vroxy-dispatch-feedback-agent.service}"
   DELAY="${VROXY_DISPATCH_RESTART_DELAY:-30}"
   sudo -n systemd-run --on-active="${DELAY}s" \
     --unit="vroxy-dispatch-restart-once-$RANDOM" --collect \
     systemctl restart "$UNIT"
   ```

   Mid-turn: use **30–45s** so the reply can leave the wire.
   Post-reply / self-update style: **5s** is enough
   (`VROXY_DISPATCH_RESTART_DELAY` default in the agent).

6. **Tell the room** one short line, e.g.
   `🔄 Restart armed in ${DELAY}s — back on the new build shortly.`
   Then stop. Do not call `systemctl restart` yourself.

## Hard no

- `systemctl restart …` from inside the unit (or any child of it)
- `systemctl kill`, `kill` on the service PID, or restarting while
  py_compile is red
- Restarting a *different* workspace's unit than the one that owns
  this turn (`vroxy-dispatch@arubamu` vs the dogfood unit)

## Fallback

If `sudo -n systemd-run` fails, say so in the room and ask an
operator to restart. Do **not** `os._exit` from a room turn — that
path belongs to `schedule_restart` inside `feedback_agent.py` after
the answer is already posted.
