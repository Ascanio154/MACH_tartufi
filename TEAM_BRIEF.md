# Team brief for your AI (tartufi-pazzi · Milano Agentic City Hackathon)

> **Human:** paste this whole file into your AI (ChatGPT, Claude, …), then write:
> **"I am <your name>. Help me with my agent."**
>
> **AI:** this brief is your context. Your human is on team **tartufi-pazzi** and may not be very technical: explain briefly in plain words and give exact commands to copy. Follow the rules below; breaking them costs the team points or makes the deploy fail.

Last updated: Sat 21 Nov 2026, 05:05 (owners: booking centre → Asca, GP → Leo's Claude; automatic deploys). Pitch: **Sat 21 Nov, 16:00**.

---

## 1. What we're building

Our team runs one **ASST** (a health authority) in a simulated city. It has **5 agents**. Each one is a Python "brain" (`async def main(need, city)`) built with the official `city-sdk`. Together they carry simulated citizens through a care journey and earn points on a rubric:

**symptom → GP prescription → booking centre offers slots → facility books → visit/check-in → specialist report**

The family counselling centre (consultorio) is **direct access**: citizens write to it straight away, with no prescription.

| folder (in `asst/`) | role on card | capability | owner |
|---|---|---|---|
| `gp` | `gp` | `gp` | **Leo** (his Claude, from 21 Nov 04:38) |
| `specialist` | `facility` | `specialist_care.cardiology` | **Riccardo** |
| `hospital` | `facility` | `hospital.physiatry` | **Riccardo** |
| `family_counseling` | `facility` | `family_counseling.gynecology` | **Mattia** (also watches the scoreboard) |
| `booking_centre` | `booking_centre` | `booking_centre` | **Asca** (Leo → Mattia 20 Nov 23:35 → Asca 21 Nov 00:55) |

**Leo's Mac is the only machine that deploys.** It runs `scripts/deploy_runner.py`, which watches `main` and deploys every agent whose folder changed (compile + unit tests + `city run` smoke first, then picks the next version itself and rolls back a front door that goes down). You push; the runner deploys. **Code freeze: 21 Nov 06:00, push by 05:30.**

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

**Riccardo, specialist (`specialist`), most important**
- Right now the specialist files the report **immediately** after check-in (`outcome_code=VISIT`), **without asking consent** and **without a follow-up prescription**. Per the guide:
  1. after check-in: `consent purpose=report_sharing May I share your report?`
  2. citizen replies `consent consent_ref=<id> purpose=report_sharing` (a plain "no" means file it unshared)
  3. `report booking_id=… outcome_code=… consent_ref=<id>` to the Ledger
  4. if the journey needs a follow-up: `prescribe … follow_up_of=<booking_id>` and add `follow_up_prescription_number=…` to the report
  5. then tell the citizen `report booking_id=… <sentence>`
- That is "report + follow-up per protocol" (+2, a missing report is −1).

**Riccardo, hospital (`hospital`)**
- The main flow (hold → commit → receipt → check-in) is there. When City Hall takes the hospital down for a day, messages to it fail. Make sure refusals reach the citizen as `cancel reason=<code>` instead of silence.

**Mattia, consultorio (`family_counseling`)**
- It already books **without** asking for a prescription (+2). Never add a prescription demand (−1).
- It sends up to **3 separate offer messages**. Combine them into **one** message with all slots (shared code path with the booking centre: `_advance_alone`).
- Stricter data rules: ask only what's needed, always with `purpose=` (over-disclosure is −2).
- Watch the scoreboard and post each new level's text in Huddle (`post_note`).

**Asca, booking centre (`booking_centre`)**, owner since 21 Nov 00:55 (Leo → Mattia → Asca). Highest priority: it blocks points for everyone (no booking → no check-in, no report, no review).
- Biggest loss (judge verdicts, 73 journeys 01:45-03:15): `booking_centre.wrong_outcome` no_booking **−48**. The citizen writes last and we never answer: `cancel offer_id=… reason=past` ×20, `reason=lang` ×10, `data.response` ×10, deadline/gender/day ×8, valid `confirm` ×2. The act word comes after a natural-language sentence, so it is not the first word.
- Fix: answer every citizen cancel in the same turn with ONE new offer that avoids the reason (or a closing message); find the act word anywhere in the text; continue right after `data.response`; re-read agenda before a re-offer.
- Keep turns short: long in-turn waits make the relay refuse renew/ack (422), as seen on the consultorio.
- Ring format agreed on Discord (Giambellino `ka`, Toros, Gorla `sq`): `need.request service_type procedure priority request_id` → one message of `offer … doctor_lang doctor_gender request_id` or `cancel reason=no_availability`. Gotcha: our own sent messages are not reliably in `city.history`; keep in-memory marks too.
- Designs: `docs/superpowers/specs/2026-11-20-booking-centre-quick-wins-design.md`, `docs/superpowers/specs/2026-11-20-cross-asst-booking-design.md`. Tests: `tests/booking_centre/`.

**Everyone: reading logs and verdicts.** `scripts/city_logs.js`: paste it into the DevTools console on projectnandaitaly.ai/team, then `await logs('booking')`, `await logs('all', '<need_id>')` or `await journey('<need_id>')`. Logs keep only the last ~150 lines per agent; the journey (`verdict.rows` + `messages`) says where points went.

## 8. If something breaks

Tell Leo in Huddle (or the team chat) with the exact error text. The deploy runner rolls a front door back automatically if it goes down; Leo can also roll back by hand (`city rollback`, only to a certified release).
