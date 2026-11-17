# Badminton tournament planner

A web interface (in Docker) over two command-line programs:

| Program | Does | Reads | Writes |
|---|---|---|---|
| `tournament_setup.py` | Proposes a format for each series (pools → knockout, or a single pool for small series), checks it fits the day, builds pools and draws | `event.toml` | `tournament.json` |
| `scheduler.py` | Orders and numbers every match, gives each team its convocation time, checks the plan with a Monte Carlo simulation | `tournament.json` | `schedule_out/` |

A "team" is whatever plays a match: one player in singles, a pair in doubles.

## Web interface (Docker)

```
docker compose up -d --build        # then open http://<host>:8000
```

The interface has three tabs:

- **1 · Tournament setup.** *Generate an example…* creates FFBaD-style series: one per discipline (SH, SD, DH, DD, MX) and ranking group (e.g. `SH R4/R5`, `MX P12/NC`, from N1 down to NC, 1 to 3 rankings per series). It fills them with invented teams, and popular levels and disciplines get more teams. Or import an event config (.toml, .json, .yaml) or fill in the fields. Importing fills the fields so you can adjust them before going further. *Propose formats* lists the proposals per series. Click a row to choose another format, and the others adapt to the end time if that option is ticked. *Generate tournament* saves the result. Download it as `tournament.json` or send it to step 2.
- **2 · Schedule.** Load a tournament: import a `tournament.json`, pick a setup run, or arrive from step 1. Event values are filled from the tournament and can be overridden. Every optimizer, objective (free wait, tiers, steps, spreads, weights) and simulation setting is a field. *Import / Export settings* saves them as JSON. *Run schedule* queues the run. While it runs you see a progress bar, the live log and the best objective found so far. **Stop and keep best** ends the optimization at once and still produces every result from the best plan found up to then; **Cancel** discards the run. When it finishes you get the statistics, court use per hour, the simulated day, the timeline with convocations, and every output file.
- **History.** Every setup and schedule run is kept: open it to see or re-use it, rename it, download its files as a zip, delete it, or select several schedule runs to compare them side by side (best value of each row highlighted).

Notes:

- **Queue and cores.** Runs execute one at a time, each using every CPU the container gets. Limit it with `cpus:` in `docker-compose.yml` if the host runs other services.
- **Storage.** The history lives in the `planner-data` volume (`/data/runs/<id>/` in the container: inputs, outputs, log). Back up or prune that volume as you like.
- **Password.** Set `APP_PASSWORD` in `docker-compose.yml` to put the whole site behind one shared password (a login page, no user accounts). The session lasts 14 days. Changing the password logs everyone out. Wrong attempts are slowed down. Leave it empty for an open instance on a trusted network. When the site is exposed, put it behind your reverse proxy with HTTPS so the password doesn't travel in clear.
- **Running without Docker.** `pip install -r requirements.txt`, then `uvicorn webapp.server:app --host 0.0.0.0 --port 8000`. Set `DATA_DIR` to choose where runs are stored (default `./data`).
- **Translation.** All interface text is in `webapp/static/i18n.js`. Add a translated copy of the `en` block (e.g. `fr`) and a language menu appears. The generated files (CSV, statistics, timeline page) are still in English.

## Command line

The two programs also run on their own, which is what the web interface calls underneath.

### Install

Python 3.11 or newer (TOML support is built in). Optional extras, all included in `requirements.txt`:

```
pip install ortools    # CP-SAT solver, used to polish the heuristic plan when installed
pip install pyyaml     # only if you prefer YAML config files
```

### Quick start

```
python tournament_setup.py event_example.toml           # proposals + tournament.json
python scheduler.py tournament.json --simulate 300      # plan + convocations + simulation
```

Open `schedule_out/schedule.html` for a court-by-court view of the day; the CSV files open in Excel or LibreOffice (use `--csv-sep ";"` for a French-locale Excel).

## 1. Describe the event

See `event_example.toml` (comments explain every field). The essentials:

```toml
[event]
start_time = "08:30"
end_time = "20:00"        # optional, used to check the formats fit
courts = 9
match_duration = 30       # mean, including warm-up and court changeover
min_rest = 20             # TIME_DELTA: end of a match -> earliest start of the team's next match
convocation_lead = 30     # convocation = first match time - 30 min
convocation_rounding = 5  # convocations rounded down to 5 min

[[series]]
name = "A"
teams = 24                # or a list of names, best seed first
```

Per series you can also set `match_duration`, or constrain the format with `pools = 6`, `qualifiers = 1` or `single_pool = true/false`.

## 2. Choose formats: `tournament_setup.py`

```
python tournament_setup.py event.toml [-o tournament.json] [--pick B=2] [--top 8] [--no-fit]
```

For every series it lists the possible formats, best first:

```
Series B  (18 teams)
    #  format                                               matches  per team court time min length penalty
 *  1  4 pools (2x5 + 2x4), top 2 -> KO of 8 (draw 8)            39       3-7      19h30       6h20    2.01
    2  5 pools (3x4 + 2x3), top 2 -> KO of 10 (draw 16, 6 byes)      33       2-7      16h30       5h30    2.76
```

- *per team*: guaranteed matches (pool) to the matches of a finalist.
- *court time*: matches × mean duration. *min length*: the shortest possible duration of the series even with unlimited courts (rounds + rests).
- *penalty* ranks the formats using `[setup.weights]`: distance from the preferred pool size, from the preferred number of qualifiers, and the share of byes. Pools whose size gives fewer than `min_matches_per_team` matches are excluded.

**Fitting the day.** With an `end_time`, the program estimates the day as `court time / (courts × planning_efficiency)` (or the longest series, if longer). If it does not fit, it swaps the series whose shorter format loses the least quality per minute saved, until it fits. If even the shortest formats cannot fit, it keeps the preferred ones and tells you how many courts you would need. `--no-fit` turns this off. `--pick B=2` forces proposal 2 for series B.

**Pools and draw.** Teams are taken in seeding order and spread over the pools in snake order (1st seed in P1, 2nd in P2, ...). Pool winners take the top seeds of the knockout draw, so byes go to them; second places go into the half of the draw away from their pool winner, so two teams from one pool can only meet again as late as possible. Pool matches follow the round-robin circle order (the top two seeds of a pool meet in the last round).

The draw is a proposal: `tournament.json` is plain JSON, so you can rename teams, move teams between pools, or edit the knockout slots before scheduling.

## 3. Schedule: `scheduler.py`

```
python scheduler.py tournament.json [--time-limit 60] [--simulate 500] [-o schedule_out]
```

### Rules it follows

- Every match gets a unique number, increasing with the planned start time. On the day, a free court takes the lowest-numbered match that can be played. Which court a match is on does not matter (the court column is only for display).
- A team always gets at least `min_rest` minutes between the end of a match and the start of its next one.
- The players of a knockout match are unknown when the plan is made, so its rest is guaranteed against **every** match that could feed it: the whole pool, or the previous knockout match.
- Pool rounds are started in order (round 1, then 2, ...). `--free-round-order` lifts this.

### What it optimizes

A *gap* is the time between the end of a team's match and the start of its next one. Three kinds: between two pool matches, from the pool to the first knockout match, and between knockout rounds. The optimizer minimizes:

```
  cost of every pool gap                                                  (see below)
+ ko_wait_weight × cost of the expected wait before / between KO matches  (default 1)
+ wait tiers and steps on every gap (KO entries: on the worst case)
+ makespan_weight × length of the day                                     (default: number of teams)
+ overtime_weight × minutes past end_time                                 (default: 10 × makespan weight)
+ pool_spread_weight   × minutes a pool lasts beyond its shortest span    (default 2)
+ series_spread_weight × minutes a series' pool phase is stretched        (default 5)
```

**Cost of a gap.** Gaps up to `--free-wait` (default 60 min) all cost nothing: 30 or 55 minutes make no difference. Beyond that, each minute costs 1, plus an extra weight for each minute past each tier threshold. With the default `--wait-tiers 60:2,90:6,120:20,180:100`:

| Gap | Cost | Cost of one more minute |
|---|---|---|
| up to 60 min | 0 | 0 |
| 90 min | 90 | 9 |
| 120 min | 360 | 29 |
| 179 min | 2,071 | 29 |
| 180 min | 2,100 | 129 |
| 240 min | 9,840 | 129 |

For scale, with 78 teams one minute of day length costs 78, so a 4-hour wait costs as much as making the day about 2 hours longer. For a knockout entry, nobody knows in advance who will qualify. So the base cost uses the expected wait (the average over the pool's teams), while the tiers and steps use the **worst case**: the team that finished its pool earliest.

**Steps (optional).** `--wait-steps 120:300,180:1000` adds a fixed cost once to every gap longer than each threshold: here 300 for any gap over 2 hours, 1,000 more over 3 hours. Where the tiers fight the *length* of long gaps, steps fight their *number*. The two combine, and `--wait-steps none` is the default. A pure step function (cost constant within a band) is a bad idea on its own: a 125-minute and a 179-minute gap would cost the same, so the optimizer would have no reason to shorten one into the other.

`--free-wait 0 --wait-tiers none` goes back to a plain sum of all waiting minutes.

**Compactness.** A pool's shortest span is its rounds back to back with the practical minimum gap (pool of 4: 3 rounds = 30+30+30+30+30 = 150 min). A series' pool phase can't be shorter than its longest pool, or than its pool court time divided by the courts. Every minute beyond that is penalized, which keeps each series in one block instead of using its matches to fill holes all day. Raise `--series-spread-weight` for tighter blocks.

**End time.** With an `end_time` in the event, running past it costs 10 times a normal minute of day length. The day can still overrun if that's unavoidable. `--overtime-weight 0` ignores the end time.

**Why an idle court can reduce waiting.** Say team X's first match could start at 9:00 on a free court, but its second match can't happen before 10:30 because its pool-mates are busy. Starting at 9:00 means X waits an hour in the hall. Leaving the court free and starting X at 9:40 means X is convoked 40 minutes later and doesn't wait. The heuristic solver never leaves a usable court idle. CP-SAT may do it when the weights say it's worth it. On the day, the table can still call that match early if both teams are there.

### Solvers

- `heuristic`: builds a first plan by packing pools so that each team plays every other slot, then improves the match priority order by simulated annealing. Several independent runs go in parallel (`--jobs`, default = CPU count up to 8) and the best is kept. No dependencies.
- `cpsat` (needs `ortools`): starts from the full heuristic plan and improves it with Google's CP-SAT. It gains the most on machines with many cores and longer time limits (`--time-limit 300`).
- `auto` (default): `heuristic` for half the time, then `cpsat` for the rest if OR-Tools is installed. It keeps whichever plan is better.

The heuristic's starting plans try every way of arranging the series (which ones play side by side, which follow each other) for up to 5 series, and a sample beyond that.

Runs are time-limited, so results vary from run to run, sometimes by a few percent of the objective. More cores and a longer `--time-limit` give better and steadier plans. Keep the best `plan.json` and compare plans with `--plan`.

### Output files (`schedule_out/`)

- `schedule.csv`: one line per match: number, planned start/end, court, series, stage, round, pool, the two teams (or slots like `1st P3`, `W#57` = winner of match 57).
- `convocations.csv`: one line per team: convocation time, first match, all its pool matches, idle time, when its pool ends, and its next match if it finishes 1st or 2nd of its pool.
- `statistics.txt` / `statistics.json`: the schedule statistics described below (plus the simulation, if run). The text version is also printed to the console.
- `team_stats.csv`: one line per team: idle time, longest gap, time on site if out after the pools, wait before its first knockout match, and the worst case as a finalist (final end, time on site, waiting, share of time playing).
- `schedule.html`: rating and key figures, court use per hour, timeline per court (hover a match for details), per-series table and convocations.
- `plan.json`: the plan, reloadable with `--plan` (for example, to simulate it again with other settings or recompute its statistics without re-optimizing).

Every plan is checked before it is written: court count, rests, stage order, round order, and a replay of the on-the-day rule with mean durations must reproduce it exactly.

### Understanding what the weights do

Weights are hard to judge in isolation, so the interface gives three views:

1. **Exchange rates** (live, under the objective settings). Every weight is converted into the common unit "one minute of gap beyond the free wait". With 78 teams, for instance: 1 min of day length = 78, 1 min of series stretch = 5, one more minute on a 150-min gap = 29. So "10 min shorter day" is worth the same as "one 150-min gap growing by 27 min". If that trade-off feels wrong, the weight is wrong.
2. **Where the cost goes** (each result, and a mini bar per run in the comparison). This shows how the objective splits between day length beyond its minimum, gaps, knockout waits, tiers, steps, spreads and overtime. A term that takes a large share is what drives the plan, and changing its weight has the most effect. The command-line report has the same "Cost split" line.
3. **Sweep** (Schedule tab). Queue several runs that differ in one setting: a weight, all tiers ×k, all steps ×k, the number of courts, or the time limit. Then compare them side by side in History. This shows what each value really buys (e.g. series spread 0 → 20: compactness +19% → +15%) and costs. Runs are time-limited and vary a little, so look for clear trends rather than single differences.

### Statistics and wait rating

```
  WAIT RATING    C (fair): 17.2 min of avoidable wait per gap (beyond the practical minimum of 30 min)
  All gaps       any wait between two matches of a team: mean 44, median 40, P90 80, worst 150 min
  Long gaps      > 60 min: 68 | > 90 min: 10 | > 120 min: 2 | > 180 min: 0   (of 198; KO entries counted at their worst case)
  Pool gaps      between pool matches: mean 42, median 30, P90 80, worst 90 min (120 gaps)
  Before KO      pool -> first KO match: expected mean 58 min; worst case for a team (own last pool match -> first KO match) 150 min
  Between KO     between knockout rounds: mean 37, median 30, worst 90 min (35 gaps)
  Pool idle      per team over its pool phase: mean 64, median 60, P90 150, worst 210 min
  Compactness    pools last +6% longer than their shortest span (worst pool +33%); series pool phases +33% longer than their minimum
```

- **All gaps** covers every gap of every kind. Knockout entries use the expected wait in the mean and the worst case in "worst".
- **Long gaps** counts the gaps over each tier threshold, with knockout entries counted at their worst case. It shows directly what the non-linear penalty is fighting.
- **Before KO / Between KO** separate the wait for a qualifier to play its first knockout match from the gaps between knockout rounds.
- **Wait rating.** The *practical minimum gap* is the shortest gap possible when courts run in step: with 30-minute matches and 20 minutes of rest, a team can't play again before the next 30-minute slot, so 30 min. Everything above that is *avoidable wait*. Its average over all gaps gives the grade: A ≤ 5 min, B ≤ 15, C ≤ 30, D ≤ 45, E above. Some of it can't be removed in practice, such as the bye round of a pool of 5, or a pool winner with a bye waiting for the round before it. Use the grade to compare plans of the same event, not as an absolute score.
- **Compactness**: how much longer pools and series' pool phases last than they could, in % of their minimum (a pool of 4 needs 150 min; +20% means 180 min).
- **Courts.** Average use, when the last stretch with every court busy ends, idle court time, how much of it falls in the last two hours, and use per clock hour.
- **Time on site** runs from the convocation, for a team knocked out in the pools and for a team going all the way to the final (on its slowest path through the draw). The *playing share* is the part of that time spent on court.
- **Longest gaps** names the five worst pool gaps with their match numbers.
- **Per series**: pool phase (from-to) and its stretch, mean and worst gap, worst wait before and between knockout rounds, longest stay, and a rating.

With `--simulate`, the main figures are computed for each simulated day and shown next to the plan: mean, P90 and worst case.

### Stopping early

While `scheduler.py` optimizes, it prints the best objective found every 10 seconds (heuristic) or at each improvement (CP-SAT). To stop and keep the best plan so far, create the file `STOP` in the output folder (`touch schedule_out/STOP`), or the path given with `--stop-file`. The optimizer finishes within a second or so and writes every output as usual, and the solver line says "stopped early". Creating the file again during the simulation ends the simulation early too, after at least 10 simulated days.

### What-if

All event values can be overridden on the command line without re-running the setup:

```
python scheduler.py tournament.json --courts 8 --rest 25 --duration 32 --lead 45 --start 08:00
```

### Simulation

`--simulate N` replays the day N times with random match durations (lognormal, same mean, `--duration-cv 0.25` by default). Matches are called in number order when both teams are present (after their convocation) and rested. `--max-advance M` stops a match being called more than M minutes before its planned time (default: no limit; `0` = never early). It reports the end of day (mean, P90, worst), how far starts drift from the plan, waits in practice, and how long teams really wait between their convocation and first match. That last figure is useful for choosing `convocation_lead`.

## Limits of this version, and what comes next

- **Players in several teams.** Not handled yet. The scheduler already enforces rest per entity listed in `teams` of each match. Supporting players in several series means listing each team's players in the config and making those players the entities, so that clashes and rests are enforced across series. The solvers don't change.
- One continuous day, no multi-day events. Teams eat during their waits, so no lunch break is planned.
- Single elimination only after pools: no consolation bracket, no third-place match.
