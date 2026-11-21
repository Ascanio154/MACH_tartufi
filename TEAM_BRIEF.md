# Team brief for your AI (tartufi-pazzi · Milano Agentic City Hackathon)

> **Human:** paste this whole file into your AI (ChatGPT, Claude, …), then write:
> **"I am <your name>. Help me with my agent."**
>
> **AI:** this brief is your context. Your human is on team **tartufi-pazzi** and may not be very technical: explain briefly in plain words and give exact commands to copy. Follow the rules below; breaking them costs the team points or makes the deploy fail.

Last updated: Sat 21 Nov 2026, 11:10 (only Leo and Asca still working: Asca = booking centre, Leo = GP, hospital, specialist, consultorio). Pitch: **Sat 21 Nov, 16:00**.

---

## 1. What we're building

Our team runs one **ASST** (a health authority) in a simulated city. It has **5 agents**. Each one is a Python "brain" (`async def main(need, city)`) built with the official `city-sdk`. Together they carry simulated citizens through a care journey and earn points on a rubric:

**symptom → GP prescription → booking centre offers slots → facility books → visit/check-in → specialist report**

The family counselling centre (consultorio) is **direct access**: citizens write to it straight away, with no prescription.

| folder (in `asst/`) | role on card | capability | owner |
|---|---|---|---|
| `gp` | `gp` | `gp` | **Leo** (his Claude, from 21 Nov 04:38) |
| `specialist` | `facility` | `specialist_care.cardiology` | **Leo** (from Riccardo, 21 Nov ~11:00) |
| `hospital` | `facility` | `hospital.physiatry` | **Leo** (from Riccardo, 21 Nov ~11:00) |
| `family_counseling` | `facility` | `family_counseling.gynecology` | **Leo** (from Mattia, 21 Nov ~11:00) |
| `booking_centre` | `booking_centre` | `booking_centre` | **Asca** (Leo → Mattia 20 Nov 23:35 → Asca 21 Nov 00:55) |

**Leo's Mac is the only machine that deploys.** It runs `scripts/deploy_runner.py`, which watches `main` and deploys every agent whose folder changed (compile + unit tests + `city run` smoke first, then picks the next version itself and rolls back a front door that goes down). You push; the runner deploys. Deploy log: GitHub issue #1.

**Since 21 Nov ~11:00 only Leo (with his Claude) and Asca are working.** Riccardo and Mattia stopped; their agents are Leo's now.

## 2. Current state (already done)

- All 5 agents are **live in the city** (versions change often: check the console's Agenti page). We are at Levels 3-8 of the game.
- They all started from the official kit's **full care-journey brain**: the same file copied into each folder. The brain works out its role from `card.yaml` (a `facility` agent picks its job from the capability prefix). GP code is in `_gp`, booking in `_desk` / `_advance_alone`, facilities in `_facility` and the `_on_*` handlers, the consultorio in `_family`.
- Each folder has **its own copy**. Editing `asst/gp/agent/` changes **only** the GP, even if you touch shared helper functions. City Hall refuses big files, so gp, booking_centre and family_counseling are split: `agent.py` loads `<name>_part*.py` next to it (same module). Edit any file inside your own `agent/` folder.
- Already working (tested): the GP prescribes from the protocol table and escalates red flags (including a plain "I have chest pain"). The booking centre checks prescriptions with the Ledger. Facilities record check-ins.

## 3. Links

- **Team repo (private):** https://github.com/Ascanio154/MACH_tartufi, branch **`main`**. Code is in `asst/<folder>/agent/agent.py`. No access? Ask Asca (the owner) to add your GitHub username.
- **Official kit:** https://github.com/projectnandaitaly/mach-hackathon-kit. The rules (Italian, the source of truth) are in `docs/kit/e19-rules.md`.
- **Huddle (our team board):** https://huddle-hub.leomach.workers.dev, open with `/?key=<your-key>`. Leo gave each person their own key.
- **Scoreboard:** https://projectnandaitaly.ai (the column "Il Token della Sera" announces each new level). **Team console:** https://projectnandaitaly.ai/team (the Journeys tab shows where we lose points).

## 4. Connect your AI to Huddle (do this first)

Huddle is where we split work: tasks, "I'm editing this folder" locks, messages, decisions, and short rule guides for every role. It does **not** store code; code lives on GitHub.

Your personal MCP URL (replace `YOUR_KEY` and the label):
`https://huddle-hub.leomach.workers.dev/mcp?key=YOUR_KEY&agent=<name>-<tool>` (e.g. `asca-chatgpt`)

| Tool | How |
|---|---|
| **ChatGPT** | Settings → Apps & Connectors → Advanced settings → turn on **Developer mode** → Create → paste the MCP URL → Authentication: **No authentication** |
| **Claude Desktop / claude.ai** | Settings → Connectors → Add custom connector → paste the MCP URL |
| **Claude Code** | `claude mcp add --transport http huddle "<MCP URL>"` |

Then tell your AI: **"Use Huddle: call get_briefing and follow its rules."**
Never commit your key and never paste it into a chat message. It belongs only in the connector settings.

**AI, once connected:** start with `get_briefing`, then `get_guide overview`, `get_guide sdk` and the guide for your human's role (`gp`, `specialist`, `hospital`, `family_counseling`, `booking_centre`). `claim_task` before working, keep `set_status` current, `post_note` findings, `log_decision` for anything teammates depend on, check `inbox`.

## 5. How we work (every change)

1. **Get the latest code:** `git pull` (or clone the repo the first time). Always pull before starting **and** right before pushing.
2. In Huddle: `claim_task`, then `lock_paths asst/<your-folder>/**`.
3. Edit **only** files inside `asst/<your-folder>/agent/`.
4. **Test locally** (optional, needs the tools below): from the `asst/` folder run `city run <your-folder>`.
5. **Commit and push** to branch `main`, then `release_paths`.
6. The deploy runner on Leo's Mac picks up your push automatically. Tell the team in Huddle what you pushed (`send_message all`), and check the console that the new version went live.

**Never run `city deploy` from your own computer**, even with the right roles set: the 5 live agents are tied to the `agent_id` in the `card.yaml` files in this repo, and a deploy from another machine can create a second set of agents. **Can't push?** Ask Asca to add your GitHub username; until then, send Leo your finished `agent.py` and he'll commit and deploy it.

**Local testing tools (optional):** `curl -LsSf https://astral.sh/uv/install.sh | sh`, then `uv tool install city-cli`, then `city login` (it should end with "Signed in as tartufi-pazzi"). Useful test lines inside `city run` (choose **citizen** first):
- GP: `/need symptom=SYM-PALPITATIONS` (should prescribe) and `I have chest pain` (should escalate)
- booking centre: `book prescription_number=prescription_number-LOCAL`. It should ask the Ledger; the local Ledger replying "not found" is expected.
- facilities: `checkin booking_id=bk-local-1`. It should write `checkin` to the Ledger.
- The numbered menu shortcuts (`/need rx=…`, `/checkin bk-…`) are written for the basic starter brain, so they often show "no actions" with our brain. That's normal.
- The local Ledger doesn't simulate holds, commits or check-ins. The full journey only runs in the real city.

## 6. Rules (from the kit; breaking them costs points or fails the deploy)

- Edit **only** your `agent/` folder. **Never** touch `Dockerfile`, `card.yaml` or `city.yaml` (the deploy runner sets versions itself; editing `card.yaml` causes conflicts).
- Talk to the city **only** through `city_sdk.city` (`city.find`, `city.say`, `city.history`, `city.llm`) plus the Python standard library. No HTTP, sockets or extra packages.
- Messages are chat text: an **act word + `key=value` tokens**, e.g. `offer offer_id=o-1 slot_id=S-12 doctor_id=D-3 agenda_date=2026-11-18`. No JSON. **Put all slots in ONE offer message.**
- **Write the Ledger first** (`city.say(LEDGER, "...")`), **then** tell the citizen. Never send a booking receipt without a Ledger commit (−2 "phantom booking").
- **No clinical advice.** Apply the protocol table only. `clinical(text)` means `raise OutOfScope()`. A red flag or emergency means `escalate.emergency reason=red_flag`, then stop (a missed escalation is −2).
- Ask for personal data only with a purpose: `data.request purpose=booking fields=age`. Never leak other patients' ids. Share no citizen data with another ASST before consent.
- **Never retry a 4xx error** (e.g. `TARGET_NOT_VISIBLE` when the hospital is down). Catch `CapError` and carry on.
- Keep `city.llm` **out of the fast path**: replying to another agent within ~10 s earns +1. The current brain uses no LLM at all.
- Never run `city deploy` or `city retire` yourself. Only Leo deploys.

## 7. What to improve next (by owner, highest points first)

Each item below was checked against the current code.

**Leo, GP (`gp`)**, handed over from Asca's session on 21 Nov at 04:38.
- Live: protocol table + red flags (incl. level-8 breathing/fainting as instant regex), P1 renewal, "Ho già avvisato io il centro prenotazioni" in every prescription. `cc53232`: no model call when keywords find the symptom (the model sentence cost 2-3 min under load); the model only classifies free text the keywords miss (4 s cap).
- Known: ~20% `protocol_mismatch` in the verdicts, but the same text gets opposite verdicts (the judge uses the citizen's hidden profile), so the mapping stays as is. Details in `.deploy/gp-wrong.md` on Leo's Mac.

**Leo, specialist (`specialist`)** and **hospital (`hospital`)**, from Riccardo on 21 Nov ~11:00.
- Live: answer partner ASSTs' `need.request` with one offer message (up to 3 world pages); natural-language check-in; the specialist files the report even when the citizen doesn't answer the consent question (20 s), no CTRL-CARD follow-up (`b2426f7`: the judge marked those reports not ok); visible collaboration quoted in the citizen's messages.
- Ledger waits: up to 40 s, one read per second (`73c65fe`: the Ledger answers hold/commit in 15-20 s; renew/ack stay 200).
- `b479f63`: a confirm for a slot another citizen is already booking gets `cancel offer_id=… reason=taken` to the booking centre at once (before: queued behind the slot lock, the losers heard nothing for 12-17 min). Tests: `tests/facility/`.

**Leo, consultorio (`family_counseling`)**, from Mattia on 21 Nov ~11:00.
- Direct access, never asks for a prescription (+2; demanding one is −1). One offer message, level-8 red flags, `lang=` token wins over a language named in the text, trust ring against impostors.
- `22ba9cf`: Ledger wait ~20 s (was 3 s → `ledger_timeout`), offers keep 8 agenda hours of margin (were past at the confirm), a confirm on a past slot gets new offers in the same turn. Tests: `tests/family_counseling/`.

**Asca, booking centre (`booking_centre`)**, owner since 21 Nov 00:55 (Leo → Mattia → Asca). Highest priority: it blocks points for everyone (no booking → no check-in, no report, no review).
- Fixed overnight/this morning: `reason=past` re-offers (agenda time), deadline from the case's first message, `lang=` wins, real cancel reasons, an offered slot stays with that citizen (`10d2aa8`), 20 s turn cap.
- Biggest loss after the restart (80 journeys, ~08:30-09:40): no_booking **−51**. 32 journeys end on "Purtroppo non ci sono ancora posti adatti…" while the judge counts 104-130 acceptable slots; 9 end on `facility_cancelled` for 18 Dec slots offered on the 22nd. Probable cause: the slot dates/agenda "now" after the ~20-day agenda jump in the pause (check by logging agenda_now next to the first/last slot dates read). Also refusals for day (17) and gender (10).
- Every message takes ~3.5 real min (= 3.5 agenda hours): keep margins and reservations in minutes, never end on "no slots" (relaxed alternative or partner slots).
- Ring format agreed on Discord (Giambellino `ka`, Toros, Gorla `sq`): `need.request service_type procedure priority request_id` → one message of `offer … doctor_lang doctor_gender request_id` or `cancel reason=no_availability`. Gotcha: our own sent messages are not reliably in `city.history`; keep in-memory marks too.
- Designs: `docs/superpowers/specs/2026-11-20-booking-centre-quick-wins-design.md`, `docs/superpowers/specs/2026-11-20-cross-asst-booking-design.md`. Tests: `tests/booking_centre/`.

**Everyone: reading logs and verdicts.** `scripts/city_logs.js`: paste it into the DevTools console on projectnandaitaly.ai/team, then `await logs('booking')`, `await logs('all', '<need_id>')` or `await journey('<need_id>')`. Logs keep only the last ~150 lines per agent; the journey (`verdict.rows` + `messages`) says where points went.

## 8. If something breaks

Tell Leo in Huddle (or the team chat) with the exact error text. The deploy runner rolls a front door back automatically if it goes down; Leo can also roll back by hand (`city rollback`, only to a certified release).
