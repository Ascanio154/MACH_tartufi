# Rules for AI agents working in this repo (Claude, ChatGPT, Codex, …)

We are a team at the **Milano Agentic City Hackathon**. The real task: one ASST with **5 city agents** (booking_centre, hospital, gp, specialist, family_counseling), each a Python brain built with the official `city-sdk` / `city-cli`.
Official kit: https://github.com/projectnandaitaly/mach-hackathon-kit (`docs/kit/e19-rules.md` is the source of truth).

**Read `TEAM_BRIEF.md` first: current state, owners, workflow and next steps.**

## Coordinate through Huddle (MCP server, connection steps in TEAM_BRIEF.md)
1. **Start every session with `get_briefing`.** Then read `get_guide overview`, `get_guide sdk` and the guide of the role you work on.
2. **`claim_task` before you start working.** Never work on a task owned by someone else. No task for it? `create_task` with `claim: true`.
3. **`lock_paths asst/<role>/**` before editing a brain**, `release_paths` when you push.
4. **`log_decision`** for anything others depend on (message formats between our agents, scope cuts). **`post_note`** for findings (Ledger quirks, rubric surprises, scoreboard level text).
5. Check **`inbox`**, keep **`set_status`** current, move tasks to `review`/`done` with a short `note`.

## City agent rules (from the kit; breaking them costs points or fails deploy)
- Edit **only** `asst/<role>/agent/agent.py`. Never touch `Dockerfile`, never change `city.yaml` beyond `role`, never add `ADD`/`RUN`.
- Talk to the city **only** through `city_sdk.city` (`find`, `say`, `history`, `city.llm`) + the Python stdlib. No HTTP, sockets or signing in the brain.
- Messages are chat text: **act word + `key=value` tokens** (e.g. `offer offer_id=o-1 slot_id=S-12 …`). All slots in one offer message.
- Write the Ledger (`city.say(LEDGER, "...")`) **before** telling the citizen. Never send a receipt without a commit.
- **No clinical advice**: apply the protocol table; `clinical(text)` → `raise OutOfScope()`; emergency → `escalate.emergency reason=red_flag` and stop.
- Ask personal data only with a purpose (`data.request purpose=… fields=…`); don't leak other patients' ids; no cross-ASST citizen data before consent.
- **Never retry a 4xx** (catch `TARGET_NOT_VISIBLE` etc.). Catch `CapError` from `city.llm` and continue without the model. Keep LLM calls out of the hot path (10 s fast-reply bonus).
- Test with `city run <role>` from `asst/`. Only the **deploy owner** runs `city deploy`. Never `city retire`. Never commit `.city/` or `~/.config/city`.

## Layout
- `asst/`: the 5 agent projects (created with `city init`), see `asst/README.md`. **Their `card.yaml` files carry the `agent_id` of the 5 live agents: never replace or re-init them.**
- Huddle (team board) runs at https://huddle-hub.leomach.workers.dev; its source lives in Leo's repo (`jmctsv4v49-hue/leo`, folder `hub/`).
