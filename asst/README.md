# asst/: our 5 city agents

Official kit (rules, SDK, templates): https://github.com/projectnandaitaly/mach-hackathon-kit
Short versions for AIs: Huddle tools `get_guide overview|sdk|cli|booking_centre|gp|hospital|specialist|family_counseling`.

| folder | `card.yaml` role | capabilities | `city.yaml` role | owner (edit in Huddle plan) |
|---|---|---|---|---|
| `booking_centre` | `booking_centre` | `booking_centre` | `booking_centre` | Leo |
| `gp` | `gp` | `gp` | `gp` | Asca |
| `specialist` | `facility` | `specialist_care.cardiology` | `facility` | Riccardo |
| `hospital` | `facility` | `hospital.physiatry` | `facility` | Riccardo |
| `family_counseling` | `facility` | `family_counseling.gynecology` | `facility` | Mattia |

## One-time setup (deploy owner only)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh      # if `uv` is missing (needs Python 3.12+)
uv tool install city-cli                              # >= 0.9.83
city login                                            # ends with "Signed in as <team>"
cd asst
city init agent booking_centre
city init agent hospital
city init agent gp
city init agent specialist
city init agent family_counseling
```

In each folder replace the TODO `role` / `capabilities` in `card.yaml` and set the same `role` in `city.yaml` (table above). Then:

```bash
city card validate specialist        # repeat per folder
city deploy specialist               # ORDER: specialist, hospital, booking_centre, gp, family_counseling
city deploy hospital
city deploy booking_centre
city deploy gp
city deploy family_counseling
city status agent:gp                 # want: deployed = visible
```

Commit the five folders (never `.city/` or `~/.config/city`). Everyone else `git pull`s.

## Upgrading to the full care journey

The kit's `templates/examples/agent.py` is the full care protocol in **one file for all roles** (it reads the role from `card.yaml`). Copy it over each `asst/<role>/agent/agent.py`, then redeploy each.

## Daily loop (everyone)

1. `git pull`, then claim your task in Huddle and `lock_paths asst/<role>/**`.
2. Edit **only** `asst/<role>/agent/agent.py`.
3. Test locally: `city run <role>` from `asst/` (needs `city login`; teammates can log in too).
4. Commit + push, then tell the deploy owner (Huddle `send_message`) to run `city deploy <role>`.
5. Bad release: `city rollback agent:<role> --to <previous version>`. Never `city retire`.
