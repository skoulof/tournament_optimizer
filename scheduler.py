#!/usr/bin/env python3
"""
scheduler.py - order and number every match of a badminton tournament, give each
team its convocation time, and stress-test the plan with random match durations.

    python scheduler.py tournament.json                      # optimize + write outputs
    python scheduler.py tournament.json --simulate 500       # + Monte Carlo check
    python scheduler.py tournament.json --courts 8 --rest 25 --time-limit 120
    python scheduler.py tournament.json --plan out/plan.json --simulate 1000   # re-check a saved plan

Model
  * Matches get unique increasing numbers in planned start order. On the day, a
    free court takes the lowest-numbered match that can be played: previous stage
    finished, both teams rested and present.
  * Every team gets at least `min_rest` minutes between the end of a match and the
    start of its next one. Knockout players are unknown in advance, so the rest is
    guaranteed against every match that could feed the slot (the whole pool, or the
    previous knockout match).
  * The optimizer minimizes
        every gap between two pool matches of a team: nothing up to free_wait (60 min), then per minute
      + ko_wait_weight  * expected wait before / between knockout matches beyond free_wait
      + wait tiers on every gap (convex: each minute beyond 60, 90, 120, 180 min costs more and more;
        knockout entries are judged on their worst case)
      + makespan_weight * length of the day             (default weight: number of teams)
      + pool_spread_weight / series_spread_weight * minutes a pool / a series' pool phase is stretched
      + overtime_weight * minutes past end_time      (default: 10 x the makespan weight)
    With the CP-SAT solver a court may stay idle for a while when that reduces
    waiting; the heuristic solver never idles a court that could be used.
"""
from __future__ import annotations

import argparse
import csv
import heapq
import html
import json
import math
import os
import random
import statistics
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

INF = float("inf")


# --------------------------------------------------------------------------- utils
def parse_hhmm(value) -> int:
    h, m = str(value).strip().split(":")
    return int(h) * 60 + int(m)


def fmt_hhmm(minutes: float) -> str:
    minutes = int(round(minutes))
    day, minutes = divmod(minutes, 24 * 60)
    s = f"{minutes // 60:02d}:{minutes % 60:02d}"
    return s if day == 0 else f"{s}+{day}d"


def fmt_dur(minutes: float) -> str:
    minutes = int(round(minutes))
    return f"{minutes // 60}h{minutes % 60:02d}"


def pct(values, q):
    """Percentile q in [0, 100] (nearest rank)."""
    if not values:
        return 0.0
    v = sorted(values)
    k = min(len(v) - 1, max(0, math.ceil(q / 100 * len(v)) - 1))
    return v[k]


def stop_requested(stop_file) -> bool:
    """A stop request is the presence of a file (touched by the web interface or by hand)."""
    return bool(stop_file) and os.path.exists(stop_file)


def log(msg: str, quiet=False):
    if not quiet:
        print(msg, flush=True)


DEFAULT_TIERS = ((60, 2.0), (90, 6.0), (120, 20.0), (180, 100.0))


@dataclass
class Weights:
    ko: float = 1.0              # knockout waits vs pool gaps (linear part)
    makespan: float = 0.0        # per minute of day length
    tiers: tuple = DEFAULT_TIERS # (threshold, extra weight per minute beyond it) for every gap
    pool_spread: float = 2.0     # per minute a pool lasts beyond its shortest possible span
    series_spread: float = 5.0   # per minute a series' pool phase lasts beyond its lower bound
    overtime: float = 0.0        # per minute the day runs past end_time (if one is set)
    free_wait: int = 60          # gaps up to this many minutes all cost the same (nothing)
    steps: tuple = ()            # (threshold, fixed cost) added once to every gap longer than threshold


def tier_cost(g: float, tiers) -> float:
    c = 0.0
    for b, w in tiers:
        if g > b:
            c += w * (g - b)
    return c


def step_cost(g: float, steps) -> float:
    return sum(c for b, c in steps if g > b)


def gap_cost(g: float, W: "Weights", base_weight: float = 1.0) -> float:
    """Cost of one gap: free up to free_wait, then base_weight per minute, plus the tiers and steps."""
    c = base_weight * (g - W.free_wait) if g > W.free_wait else 0.0
    return c + tier_cost(g, W.tiers) + step_cost(g, W.steps)


def parse_tiers(text: str) -> tuple:
    if text.strip().lower() in ("", "none", "off", "0"):
        return ()
    out = []
    for part in text.split(","):
        b, _, w = part.partition(":")
        out.append((int(b), float(w)))
    return tuple(sorted(out))


# --------------------------------------------------------------------------- problem
class Problem:
    """Integer-indexed view of tournament.json. 'Teams' are the scheduling entities that
    need rest; a future version can make them individual players without touching the solvers."""

    def __init__(self, doc: dict, ov: dict, round_order: bool = True):
        ev = dict(doc["event"])
        for k, v in ov.items():
            if v is not None:
                ev[k] = v
        self.event = ev
        self.name = ev.get("name", "Tournament")
        self.t0 = parse_hhmm(ev["start_time"])
        self.end_limit = parse_hhmm(ev["end_time"]) - self.t0 if ev.get("end_time") else None
        self.courts = int(ev["courts"])
        self.rest = int(ev["min_rest"])
        self.lead = int(ev["convocation_lead"])
        self.rounding = max(1, int(ev.get("convocation_rounding", 1)))
        self.base_dur = int(ev["match_duration"])
        if self.courts < 1:
            sys.exit("Need at least one court.")

        self.series_names = [s["name"] for s in doc["series"]]
        self.series_dur = {s["name"]: int(s.get("match_duration") or self.base_dur) for s in doc["series"]}
        self.series_format = {s["name"]: s.get("format", {}) for s in doc["series"]}
        self.series_pools = {s["name"]: [p["name"] for p in s.get("pools", [])] for s in doc["series"]}

        self.team_id, self.team_name, self.team_series, self.team_pool = [], [], [], []
        tix = {}
        for s in doc["series"]:
            for t in s["teams"]:
                tix[t["id"]] = len(self.team_id)
                self.team_id.append(t["id"])
                self.team_name.append(t.get("name", t["id"]))
                self.team_series.append(s["name"])
                self.team_pool.append(t.get("pool"))
        self.T = len(self.team_id)

        ms = doc["matches"]
        self.M = len(ms)
        mix = {m["id"]: i for i, m in enumerate(ms)}
        if len(mix) != self.M:
            sys.exit("Duplicate match ids in the tournament file.")
        self.mix = mix
        self.mid = [m["id"] for m in ms]
        self.mseries = [m["series"] for m in ms]
        self.stage = [m["stage"] for m in ms]
        self.pool = [m.get("pool") for m in ms]
        self.round = [str(m.get("round", "")) for m in ms]
        self.round_no = [int(m["round"]) if m["stage"] == "pool" else int(m.get("round_index", 0)) for m in ms]
        self.dur = [self.series_dur[m["series"]] for m in ms]
        self.teams = [tuple(tix[t] for t in m.get("teams", [])) for m in ms]
        self.slots = []
        for m in ms:
            sl = []
            for s in m.get("slots", []):
                sl.append((s["label"], tuple(sorted(mix[d] for d in s["after"])), s.get("source", {})))
            self.slots.append(sl)
        self.deps = [tuple(sorted({d for _, ds, _ in sl for d in ds})) for sl in self.slots]
        self.ndeps = [len(d) for d in self.deps]
        self.succ = [[] for _ in range(self.M)]
        for i, ds in enumerate(self.deps):
            for d in ds:
                self.succ[d].append(i)
        self.team_matches = [[] for _ in range(self.T)]
        for i, ts in enumerate(self.teams):
            for t in ts:
                self.team_matches[t].append(i)
        # unique dependency groups (a pool, or one previous KO match) for knockout waits.
        # The rest rule uses the end of the whole group; the waiting objective uses the
        # expected wait of whoever qualifies: the mean over the group's teams of the end
        # of their last match in the group (or the match end for a 'winner of' slot).
        gix: dict[tuple, int] = {}
        self.groups: list[tuple] = []
        self.group_teams: list[list[tuple[int, tuple]]] = []
        self.ko_slots: list[tuple[int, int]] = []    # (match, group)
        for i, sl in enumerate(self.slots):
            for _, ds, _ in sl:
                if ds not in gix:
                    gix[ds] = len(self.groups)
                    self.groups.append(ds)
                    tm: dict[int, list[int]] = {}
                    for d in ds:
                        for t in self.teams[d]:
                            tm.setdefault(t, []).append(d)
                    self.group_teams.append([(t, tuple(v)) for t, v in sorted(tm.items())])
                self.ko_slots.append((i, gix[ds]))
        # optional start-start precedences: pool rounds are started in order
        self.round_order = round_order
        self.sdeps = [[] for _ in range(self.M)]
        if round_order:
            by_pool: dict[tuple, dict[int, list[int]]] = {}
            for i in range(self.M):
                if self.stage[i] == "pool":
                    by_pool.setdefault((self.mseries[i], self.pool[i]), {}).setdefault(self.round_no[i], []).append(i)
            for rounds in by_pool.values():
                keys = sorted(rounds)
                for a, b in zip(keys, keys[1:]):
                    for j in rounds[b]:
                        self.sdeps[j] = list(rounds[a])
        self.nsdeps = [len(x) for x in self.sdeps]
        self.ssucc = [[] for _ in range(self.M)]
        for i, ds in enumerate(self.sdeps):
            for d in ds:
                self.ssucc[d].append(i)
        self.series_rank = {s: k for k, s in enumerate(self.series_names)}
        # practical minimum gap: courts run in step, a match every `base_dur` minutes
        D = self.base_dur
        self.g0 = math.ceil(self.rest / D) * D if self.rest > 0 and D > 0 else 0
        # shortest possible day: all court time spread perfectly over the courts
        self.day_lb = math.ceil(sum(self.dur) / self.courts)
        # team matches in round order (consecutive pool matches of a team)
        self.team_seq = [sorted(ms, key=lambda i: (self.round_no[i], i)) for ms in self.team_matches]
        # pools: matches and shortest possible span (rounds back to back with the minimum gap)
        self.pools_list = []
        for s in self.series_names:
            for pname in self.series_pools[s]:
                pm = [i for i in range(self.M) if self.mseries[i] == s and self.pool[i] == pname]
                if not pm:
                    continue
                rounds = len({self.round_no[i] for i in pm})
                d = max(self.dur[i] for i in pm)
                self.pools_list.append((s, pname, pm, rounds * d + (rounds - 1) * self.g0))
        # series pool phase: matches and a lower bound on its span
        self.series_pool = {}
        for s in self.series_names:
            pm = [i for i in range(self.M) if self.mseries[i] == s and self.stage[i] == "pool"]
            spans = [ms for (ss, _, _, ms) in self.pools_list if ss == s]
            lb = max(max(spans, default=0), math.ceil(sum(self.dur[i] for i in pm) / self.courts))
            self.series_pool[s] = (pm, lb)
        self._check_acyclic()

    def _check_acyclic(self):
        seen = [0] * self.M
        order = []
        indeg = self.ndeps[:]
        stack = [i for i in range(self.M) if indeg[i] == 0]
        while stack:
            i = stack.pop()
            order.append(i)
            seen[i] = 1
            for s in self.succ[i]:
                indeg[s] -= 1
                if indeg[s] == 0:
                    stack.append(s)
        if len(order) != self.M:
            sys.exit("Match dependencies contain a cycle.")

    def default_weights(self) -> Weights:
        return Weights(ko=1.0, makespan=float(self.T), overtime=10.0 * self.T)

    def slot_label(self, label: str, source: dict, number: list[int]) -> str:
        if source.get("type") == "winner":
            return f"W#{number[self.mid.index(source['match'])]}" if number else label
        return label


# --------------------------------------------------------------------------- evaluation
@dataclass
class Metrics:
    objective: float
    team_wait: list            # per team: idle time between its pool matches (None if no match)
    ko_exp: list               # per KO slot: expected wait of whoever fills it
    ko_worst: list             # per KO slot: worst-case wait (earliest-finishing possible qualifier)
    makespan: float
    tier_cost: float           # total extra penalty from the wait tiers (and steps)
    pool_spread: float         # total minutes pools last beyond their shortest span
    series_spread: float       # total minutes series' pool phases last beyond their lower bound
    components: dict = None    # objective split by term (cost units: 1 = one minute of gap beyond free wait)


def evaluate(P: Problem, W: Weights, start, dur=None, full=False):
    """Objective of a timetable (lower is better):
         every pool gap: free up to free_wait, then 1 per minute, plus tiers and steps
       + every KO slot: ko * expected wait beyond free_wait, plus tiers and steps on the worst case
       + makespan weight * day length + overtime + pool / series spread penalties."""
    dur = dur or P.dur
    end = [s + d for s, d in zip(start, dur)]
    tiers, steps, free = W.tiers, W.steps, W.free_wait
    lo = min([free] + [b for b, _ in tiers] + [b for b, _ in steps])   # below this a gap costs nothing
    base = tier = step = 0.0
    tw = [None] * P.T if full else None
    for t, ms in enumerate(P.team_matches):
        if not ms:
            continue
        idle = 0.0
        if len(ms) > 1:
            seq = sorted(ms, key=start.__getitem__)
            for a, b in zip(seq, seq[1:]):
                g = start[b] - end[a]
                idle += g
                if g > lo:
                    if g > free:
                        base += g - free
                    tier += tier_cost(g, tiers)
                    step += step_cost(g, steps)
        if full:
            tw[t] = idle
    gexp, gmin = [], []
    for g, gt in zip(P.groups, P.group_teams):
        if gt:
            lasts = [max(end[d] for d in ds) for _, ds in gt]
            gexp.append(sum(lasts) / len(lasts))
            gmin.append(min(lasts))
        else:
            e = max(end[d] for d in g)
            gexp.append(e)
            gmin.append(e)
    kexp = [start[m] - gexp[g] for m, g in P.ko_slots]
    kworst = [start[m] - gmin[g] for m, g in P.ko_slots]
    ko_base = sum(w - free for w in kexp if w > free)
    for w in kworst:
        if w > lo:
            tier += tier_cost(w, tiers)
            step += step_cost(w, steps)
    pspread = sum(max(0.0, max(end[i] for i in pm) - min(start[i] for i in pm) - ms)
                  for _, _, pm, ms in P.pools_list)
    sspread = 0.0
    for pm, lb in P.series_pool.values():
        if pm:
            sspread += max(0.0, max(end[i] for i in pm) - min(start[i] for i in pm) - lb)
    mk = max(end)
    over = mk - P.end_limit if P.end_limit is not None and mk > P.end_limit else 0.0
    obj = (base + W.ko * ko_base + tier + step + W.makespan * mk
           + W.pool_spread * pspread + W.series_spread * sspread + W.overtime * over)
    if full:
        comp = {"day_length": W.makespan * mk, "day_excess": W.makespan * max(0.0, mk - P.day_lb),
                "overtime": W.overtime * over, "pool_gaps": base, "ko_waits": W.ko * ko_base,
                "tiers": tier, "steps": step, "pool_spread": W.pool_spread * pspread,
                "series_spread": W.series_spread * sspread}
        return Metrics(obj, tw, kexp, kworst, mk, tier + step, pspread, sspread, comp)
    return obj


# --------------------------------------------------------------------------- dispatcher
def dispatch(P: Problem, order, dur=None, ready0=None, not_before=None, planning=False):
    """Event-driven court allocation. Whenever courts are free, they take the first
    matches of `order` that are playable now. Returns start times (minutes from t0).
    planning=True also keeps pool rounds in order (if enabled in the problem)."""
    dur = dur or P.dur
    teams, succ, rest = P.teams, P.succ, P.rest
    if planning and P.round_order:
        ssucc, sdep_left = P.ssucc, P.nsdeps[:]
    else:
        ssucc, sdep_left = [()] * P.M, [0] * P.M
    dep_left = P.ndeps[:]
    dep_ready = [0.0] * P.M
    team_free = list(ready0) if ready0 is not None else [0.0] * P.T
    start = [None] * P.M
    running: list[float] = []
    free = P.courts
    pending = list(order)
    t = 0.0
    push, pop = heapq.heappush, heapq.heappop
    while pending:
        while running and running[0] <= t:
            pop(running)
            free += 1
        next_ready = INF
        if free:
            i, n = 0, len(pending)
            while i < n:
                m = pending[i]
                if dep_left[m] or sdep_left[m]:
                    i += 1
                    continue
                r = dep_ready[m]
                for tm in teams[m]:
                    if team_free[tm] > r:
                        r = team_free[tm]
                if not_before is not None and not_before[m] > r:
                    r = not_before[m]
                if r <= t:
                    start[m] = t
                    e = t + dur[m]
                    push(running, e)
                    for tm in teams[m]:
                        team_free[tm] = e + rest
                    for s in succ[m]:
                        dep_left[s] -= 1
                        if e + rest > dep_ready[s]:
                            dep_ready[s] = e + rest
                    unlocked = False
                    for s in ssucc[m]:
                        sdep_left[s] -= 1
                        unlocked = unlocked or sdep_left[s] == 0
                    del pending[i]
                    if unlocked:
                        i = 0      # an earlier, higher-priority match may now be playable
                    n -= 1
                    free -= 1
                    if not free:
                        break
                else:
                    if r < next_ready:
                        next_ready = r
                    i += 1
        if not pending:
            break
        t_next = running[0] if running else INF
        if free and next_ready < t_next:
            t_next = next_ready
        if t_next == INF:
            raise RuntimeError("Scheduling deadlock (inconsistent dependencies?)")
        t = t_next
    return start


# --------------------------------------------------------------------------- heuristic
def series_critical_path(P: Problem, s: str) -> int:
    """Shortest possible duration of a series: pool phase, then each knockout round after a rest."""
    _, lb = P.series_pool[s]
    ko_rounds = max([P.round_no[i] for i in range(P.M) if P.mseries[i] == s and P.stage[i] == "ko"], default=0)
    return lb + ko_rounds * (P.rest + P.series_dur[s])


def series_priority(P: Problem) -> list[str]:
    """Series with the longest critical path first: they would otherwise end the day."""
    def key(s):
        n = sum(1 for i in range(P.M) if P.mseries[i] == s)
        return (-series_critical_path(P, s), -n, P.series_rank[s])
    return sorted(P.series_names, key=key)


def constructive_order(P: Problem, waves=None) -> list[int]:
    """Slot model (slot = mean match duration): pools are packed so that each team plays every
    (rest slots + 1) slots, and each series' knockout goes right after its pools.
    `waves` is a list of groups of series: groups are packed one after another, the series of a
    group side by side (their pools interleaved). Default: one series per group, by priority."""
    C, D = P.courts, max(1, P.base_dur)
    step = (math.ceil(P.rest / D) if P.rest > 0 else 0) + 1
    cap: dict[int, int] = {}
    slot = [0] * P.M
    team_next = [0] * P.T
    if waves is None:
        waves = [[s] for s in series_priority(P)]

    def used(k):
        return cap.get(k, 0)

    def put(m, k):
        slot[m] = k
        cap[k] = used(k) + 1
        for t in P.teams[m]:
            team_next[t] = k + step

    def place_pool(s, pname):
        pm = [i for i in range(P.M) if P.mseries[i] == s and P.pool[i] == pname]
        rounds = sorted({P.round_no[i] for i in pm})
        by_round = [[i for i in pm if P.round_no[i] == r] for r in rounds]
        k = 0
        if all(len(rm) <= C for rm in by_round):
            while k < 100000 and not all(used(k + j * step) + len(rm) <= C for j, rm in enumerate(by_round)):
                k += 1
        for j, rm in enumerate(by_round):
            for m in rm:
                e = max([k + j * step] + [team_next[t] for t in P.teams[m]])
                while used(e) >= C:
                    e += 1
                put(m, e)

    def place_ko(s):
        ko = sorted((i for i in range(P.M) if P.mseries[i] == s and P.stage[i] == "ko"),
                    key=lambda i: (P.round_no[i], P.mid[i]))
        for m in ko:
            e = max(slot[d] for d in P.deps[m]) + step if P.deps[m] else 0
            while used(e) >= C:
                e += 1
            put(m, e)

    for group in waves:
        queues = {s: list(P.series_pools[s]) for s in group}
        while any(queues.values()):
            for s in group:                      # interleave the pools of the group's series
                if queues[s]:
                    place_pool(s, queues[s].pop(0))
                    if not queues[s]:
                        place_ko(s)
        for s in group:
            if not P.series_pools[s]:
                place_ko(s)
    return sorted(range(P.M), key=lambda i: (slot[i], P.series_rank[P.mseries[i]], i))


def ordered_partitions(items):
    """All ways to split items into an ordered sequence of non-empty groups (Fubini number)."""
    items = list(items)
    if not items:
        yield []
        return
    first, rest = items[0], items[1:]
    for part in ordered_partitions(rest):
        for k in range(len(part) + 1):           # first item in a new group at position k
            yield part[:k] + [[first]] + part[k:]
        for k in range(len(part)):               # or added to an existing group
            yield part[:k] + [[first] + part[k]] + part[k + 1:]


def anneal(P: Problem, W: Weights, order, seconds: float, rng: random.Random, quiet=False,
           stop_file=None, tag=""):
    """Simulated annealing over the priority order fed to the dispatcher."""
    groups = []
    series_sets = [{i for i in range(P.M) if P.mseries[i] == s} for s in P.series_names]
    for s in P.series_names:
        for pname in P.series_pools[s]:
            groups.append({i for i in range(P.M) if P.mseries[i] == s and P.pool[i] == pname})
        for r in sorted({P.round_no[i] for i in range(P.M) if P.mseries[i] == s and P.stage[i] == "ko"}):
            groups.append({i for i in range(P.M) if P.mseries[i] == s and P.stage[i] == "ko" and P.round_no[i] == r})
    M = P.M
    win = max(4, 2 * P.courts)

    def neighbor(o):
        o = o[:]
        r = rng.random()
        if r < 0.4 or not groups:
            i = rng.randrange(M)
            j = min(M - 1, max(0, i + rng.randint(-win, win)))
            o[i], o[j] = o[j], o[i]
        elif r < 0.8:
            i = rng.randrange(M)
            x = o.pop(i)
            o.insert(min(M - 1, max(0, i + rng.randint(-win, win))), x)
        else:
            # move a whole pool, KO round or (sometimes) series as a block
            g = rng.choice(series_sets) if len(series_sets) > 1 and rng.random() < 0.15 else rng.choice(groups)
            pos = [k for k, m in enumerate(o) if m in g]
            members = [o[k] for k in pos]
            others = [m for m in o if m not in g]
            reach = 2 * win if len(g) < 3 * win else M // 2
            at = min(len(others), max(0, pos[0] + rng.randint(-reach, reach)))
            o = others[:at] + members + others[at:]
        return o

    def cost(o):
        return evaluate(P, W, dispatch(P, o, planning=True))

    cur, cur_c = order[:], cost(order)
    best, best_c = cur[:], cur_c
    samples = [abs(cost(neighbor(cur)) - cur_c) for _ in range(20)]
    t_hi = max(1.0, statistics.mean(samples)) * 0.3
    t_lo = t_hi / 200
    t_start = time.time()
    next_report = t_start + 10
    it = acc = 0
    while True:
        el = time.time() - t_start
        if el >= seconds:
            break
        if it % 50 == 0:
            if stop_requested(stop_file):
                break
            if tag and time.time() >= next_report:
                print(f"  heuristic {tag}: best {best_c:,.0f} after {el:.0f}s", flush=True)
                next_report += 10
        temp = t_hi * (t_lo / t_hi) ** (el / seconds)
        cand = neighbor(cur)
        c = cost(cand)
        it += 1
        if c <= cur_c or rng.random() < math.exp(-(c - cur_c) / temp):
            cur, cur_c = cand, c
            acc += 1
            if c < best_c:
                best, best_c = cand[:], c
    log(f"  annealing: {it} iterations in {time.time() - t_start:.0f}s, objective {best_c:,.0f}", quiet)
    return best, best_c


def _anneal_job(args):
    P, W, order, seconds, seed, stop_file, tag = args
    return anneal(P, W, order, seconds, random.Random(seed), quiet=True, stop_file=stop_file, tag=tag)


def starting_orders(P: Problem, W: Weights, k: int, seed: int, budget: float) -> list[list[int]]:
    """Build the constructive plan for many arrangements of the series (which series go side by
    side, which follow each other; all arrangements for up to 5 series) and return the k best."""
    rng = random.Random(seed)
    names = series_priority(P)
    if len(names) <= 5:
        waves_list = list(ordered_partitions(names))          # 541 for 5 series
    else:
        waves_list = [[[s] for s in names], [names]]
        while len(waves_list) < 400:
            sh = rng.sample(names, len(names))
            cuts = sorted(rng.sample(range(1, len(sh)), rng.randint(0, len(sh) - 1)))
            waves_list.append([sh[i:j] for i, j in zip([0] + cuts, cuts + [len(sh)])])
    t0, scored = time.time(), []
    for wv in waves_list:
        o = constructive_order(P, wv)
        scored.append((evaluate(P, W, dispatch(P, o, planning=True)), o))
        if time.time() - t0 > budget:
            break
    scored.sort(key=lambda x: x[0])
    return [o for _, o in scored[:max(1, k)]]


def heuristic(P: Problem, W: Weights, seconds: float, jobs: int, seed: int, quiet=False, stop_file=None):
    """Best constructive starts (over series orders) + independent annealing runs in parallel."""
    t = time.time()
    starts = starting_orders(P, W, max(1, jobs), seed, budget=min(10.0, 0.15 * seconds))
    seconds = max(1.0, seconds - (time.time() - t))
    starts = (starts * jobs)[:max(1, jobs)]
    if jobs <= 1:
        best = anneal(P, W, starts[0], seconds, random.Random(seed), quiet=True, stop_file=stop_file, tag="run 1")
        results = [best]
    else:
        from concurrent.futures import ProcessPoolExecutor
        try:
            with ProcessPoolExecutor(max_workers=jobs) as ex:
                results = list(ex.map(_anneal_job, [(P, W, starts[k], seconds, seed + k, stop_file, f"run {k + 1}")
                                                    for k in range(jobs)]))
        except (OSError, RuntimeError) as e:          # e.g. no fork/spawn allowed
            log(f"  (parallel runs unavailable: {e}; running one)", quiet)
            results = [anneal(P, W, starts[0], seconds, random.Random(seed), quiet=True, stop_file=stop_file,
                              tag="run 1")]
    best = min(results, key=lambda r: r[1])
    costs = ", ".join(f"{c:,.0f}" for _, c in sorted(results, key=lambda r: r[1]))
    log(f"  heuristic: {len(results)} run(s) x {seconds:.0f}s ({time.time() - t:.0f}s), objectives {costs}", quiet)
    return best


# --------------------------------------------------------------------------- CP-SAT
def solve_cpsat(P: Problem, W: Weights, hint, seconds: float, workers: int, verbose: bool, quiet=False,
                stop_file=None):
    from ortools.sat.python import cp_model

    scale = 60          # integer coefficients; divisible by common pool sizes (KO waits are pool averages)
    ic = lambda x: int(round(scale * x))              # noqa: E731
    hint_end = max(h + d for h, d in zip(hint, P.dur))
    H = int(hint_end + max(120, 0.25 * hint_end))
    md = cp_model.CpModel()
    hs = [int(h) for h in hint]                         # hinted starts / ends: every variable gets
    he = [h + d for h, d in zip(hs, P.dur)]             # a hint so CP-SAT starts from the full plan
    hints = []

    def var(lo, hi, name, value):
        v = md.NewIntVar(lo, hi, name)
        hints.append((v, int(round(value))))
        return v

    s = [md.NewIntVar(0, H - P.dur[i], f"s{i}") for i in range(P.M)]
    e = [s[i] + P.dur[i] for i in range(P.M)]
    iv = [md.NewFixedSizeIntervalVar(s[i], P.dur[i], f"iv{i}") for i in range(P.M)]
    md.AddCumulative(iv, [1] * P.M, P.courts)
    for t, ms in enumerate(P.team_matches):
        if len(ms) > 1:
            md.AddNoOverlap([md.NewFixedSizeIntervalVar(s[i], P.dur[i] + P.rest, f"r{t}_{i}") for i in ms])
    for i, ds in enumerate(P.deps):
        for d in ds:
            md.Add(s[i] >= e[d] + P.rest)
    for i, ds in enumerate(P.sdeps):
        for d in ds:
            md.Add(s[i] >= s[d])

    obj = []                                            # list of (coef, expr)

    def tiers_on(expr, name, value, mult=1):
        """Convex piecewise penalty on a gap: sum_k w_k * max(0, gap - b_k), plus a fixed cost
        for each step threshold exceeded. `mult` scales expr (and value)."""
        for k, (b, w) in enumerate(W.tiers):
            x = var(0, mult * H, f"{name}_t{k}", max(0, value - mult * b))
            md.Add(x >= expr - mult * b)
            obj.append((ic(w / mult), x))
        for k, (b, c) in enumerate(W.steps):
            z = md.NewBoolVar(f"{name}_s{k}")
            hints.append((z, int(value > mult * b)))
            md.Add(expr <= mult * b).OnlyEnforceIf(z.Not())
            obj.append((ic(c), z))

    def base_on(expr, name, value, coef, mult=1):
        """coef * max(0, gap - free_wait): gaps up to free_wait cost nothing."""
        if W.free_wait <= 0:
            obj.append((coef, expr))
            return
        y = var(0, mult * H, f"{name}_b", max(0, value - mult * W.free_wait))
        md.Add(y >= expr - mult * W.free_wait)
        obj.append((coef, y))

    shaped = bool(W.tiers or W.steps)

    # pool gaps: with pool rounds in order, a team's consecutive matches are known
    last_of: dict[int, object] = {}
    for t, ms in enumerate(P.team_matches):
        if not ms:
            continue
        if P.round_order:
            seq = P.team_seq[t]
            for k, (a, b) in enumerate(zip(seq, seq[1:])):
                g = s[b] - e[a]
                base_on(g, f"pg{t}_{k}", hs[b] - he[a], scale)
                if shaped:
                    tiers_on(g, f"pg{t}_{k}", hs[b] - he[a])
            last_of[t] = (set(ms), e[seq[-1]], he[seq[-1]])
        else:                                           # order unknown: use the span and the mean gap
            played = sum(P.dur[i] for i in ms)
            hf, hl = min(hs[i] for i in ms), max(he[i] for i in ms)
            first = var(0, H, f"first{t}", hf)
            last = var(0, H, f"last{t}", hl)
            for i in ms:
                md.Add(first <= s[i])
            md.AddMaxEquality(last, [e[i] for i in ms])
            last_of[t] = (set(ms), last, hl)
            n = len(ms) - 1
            if n:
                base_on(last - first - played, f"ps{t}", hl - hf - played, scale, mult=n)
                if shaped:
                    tiers_on(last - first - played, f"ps{t}", hl - hf - played, mult=n)

    # knockout slots: expected wait (linear) + tiers on the worst case
    gref, gref_vals = [], []
    for k, (g, gt) in enumerate(zip(P.groups, P.group_teams)):
        if not gt:                                      # winner of one match (or of several)
            hv = max(he[d] for d in g)
            gref_vals.append([hv])
            if len(g) == 1:
                gref.append(([e[g[0]]], None, hv))
            else:
                v = var(0, H, f"g{k}", hv)
                md.AddMaxEquality(v, [e[d] for d in g])
                gref.append(([v], None, hv))
            continue
        lasts, hl = [], []
        for t, ds in gt:
            if t in last_of and last_of[t][0] == set(ds):
                lasts.append(last_of[t][1])
                hl.append(last_of[t][2])
            else:
                hv = max(he[d] for d in ds)
                v = var(0, H, f"g{k}t{t}", hv)
                md.AddMaxEquality(v, [e[d] for d in ds])
                lasts.append(v)
                hl.append(hv)
        gref_vals.append(hl)
        mn = var(0, H, f"gmin{k}", min(hl))             # pushed up to min(lasts) by the objective
        for x in lasts:
            md.Add(mn <= x)
        gref.append((lasts, mn, min(hl)))
    for k, (m, g) in enumerate(P.ko_slots):
        lasts, mn, hmin = gref[g]
        n = len(lasts)
        hl_sum = (sum(gref_vals[g]) if gref_vals[g] else hmin)
        base_on(n * s[m] - sum(lasts), f"ke{k}", n * hs[m] - hl_sum, ic(W.ko / n), mult=n)
        if shaped:
            tiers_on(s[m] - (mn if mn is not None else lasts[0]), f"kw{k}", hs[m] - hmin)

    # compactness
    if W.pool_spread:
        for k, (_, _, pm, _) in enumerate(P.pools_list):
            pf = var(0, H, f"pf{k}", min(hs[i] for i in pm))
            pl = var(0, H, f"pl{k}", max(he[i] for i in pm))
            for i in pm:
                md.Add(pf <= s[i])
                md.Add(pl >= e[i])
            obj.append((ic(W.pool_spread), pl - pf))
    if W.series_spread:
        for k, (pm, _) in enumerate(P.series_pool.values()):
            if pm:
                sf = var(0, H, f"sf{k}", min(hs[i] for i in pm))
                sl = var(0, H, f"sl{k}", max(he[i] for i in pm))
                for i in pm:
                    md.Add(sf <= s[i])
                    md.Add(sl >= e[i])
                obj.append((ic(W.series_spread), sl - sf))
    mk = var(0, H, "makespan", max(he))
    for i in range(P.M):
        md.Add(mk >= e[i])
    obj.append((ic(W.makespan), mk))
    if P.end_limit is not None and W.overtime:
        ot = var(0, H, "overtime", max(0, max(he) - P.end_limit))
        md.Add(ot >= mk - P.end_limit)
        obj.append((ic(W.overtime), ot))
    md.Minimize(sum(c * x for c, x in obj if c))
    for i in range(P.M):
        md.AddHint(s[i], hs[i])
    for v, x in hints:
        md.AddHint(v, x)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(seconds)
    solver.parameters.num_workers = workers
    solver.parameters.log_search_progress = verbose
    class Progress(cp_model.CpSolverSolutionCallback):
        """Reports each improvement (with the planner's own objective) for the live log."""
        def __init__(self):
            super().__init__()
            self.best, self.t0 = evaluate(P, W, hs), time.time()

        def on_solution_callback(self):
            c = evaluate(P, W, [self.Value(v) for v in s])
            if c < self.best - 0.5:
                self.best = c
                log(f"  CP-SAT: best {c:,.0f} after {time.time() - self.t0:.0f}s", quiet)

    done = threading.Event()

    def watch_stop():                                   # stop request -> end the search, keep the best
        while not done.wait(0.3):
            if stop_requested(stop_file):
                solver.StopSearch()
                return

    threading.Thread(target=watch_stop, daemon=True).start()
    status = solver.Solve(md, Progress())
    done.set()
    name = solver.StatusName(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        log(f"  CP-SAT: {name}, keeping the heuristic plan", quiet)
        return None, name
    res = [solver.Value(v) for v in s]
    log(f"  CP-SAT: {name} in {solver.WallTime():.0f}s, objective {evaluate(P, W, res):,.0f}", quiet)
    return res, name


# --------------------------------------------------------------------------- plan
class Plan:
    def __init__(self, P: Problem, start, solver_info=""):
        self.P = P
        self.start = [int(round(x)) for x in start]
        self.solver_info = solver_info
        order = sorted(range(P.M), key=lambda i: (self.start[i], P.series_rank[P.mseries[i]], P.stage[i] != "pool", i))
        self.number = [0] * P.M
        for k, i in enumerate(order, start=1):
            self.number[i] = k
        self.by_number = order
        self.end = [s + d for s, d in zip(self.start, P.dur)]
        # display courts: lowest-index court free at the start time
        free_at = [-INF] * P.courts
        self.court = [0] * P.M
        for i in order:
            c = next((c for c in range(P.courts) if free_at[c] <= self.start[i]), None)
            if c is None:
                raise RuntimeError(f"More than {P.courts} matches at {fmt_hhmm(P.t0 + self.start[i])}")
            self.court[i] = c + 1
            free_at[c] = self.end[i]
        # convocations (absolute clock minutes)
        self.conv = [None] * P.T
        self.first = [None] * P.T
        for t, ms in enumerate(P.team_matches):
            if ms:
                f = min(ms, key=lambda i: self.start[i])
                self.first[t] = f
                abs_conv = P.t0 + self.start[f] - P.lead
                self.conv[t] = (abs_conv // P.rounding) * P.rounding

    def conv_rel(self):
        return [(c - self.P.t0) if c is not None else 0 for c in self.conv]

    def validate(self) -> list[str]:
        P, errs = self.P, []
        ev = sorted([(self.end[i], -1) for i in range(P.M)] + [(self.start[i], 1) for i in range(P.M)])
        load = peak = 0
        for _, d in ev:
            load += d
            peak = max(peak, load)
        if peak > P.courts:
            errs.append(f"{peak} matches at once on {P.courts} courts")
        for t, ms in enumerate(P.team_matches):
            ms = sorted(ms, key=lambda i: self.start[i])
            for a, b in zip(ms, ms[1:]):
                if self.start[b] < self.end[a] + P.rest:
                    errs.append(f"team {P.team_id[t]}: only {self.start[b] - self.end[a]} min rest "
                                f"between #{self.number[a]} and #{self.number[b]}")
        for i, ds in enumerate(P.deps):
            for d in ds:
                if self.start[i] < self.end[d] + P.rest:
                    errs.append(f"#{self.number[i]} starts {self.end[d] + P.rest - self.start[i]} min "
                                f"too early after #{self.number[d]}")
        for i, ds in enumerate(P.sdeps):
            for d in ds:
                if self.start[i] < self.start[d]:
                    errs.append(f"#{self.number[i]} (pool round {P.round_no[i]}) starts before #{self.number[d]}")
        if any(s < 0 for s in self.start):
            errs.append("match before the start time")
        # replay: the on-the-day rule with mean durations must reproduce the plan
        replay = dispatch(P, self.by_number, ready0=self.conv_rel(), not_before=self.start)
        if any(abs(a - b) > 1e-6 for a, b in zip(replay, self.start)):
            errs.append("replay of the plan with mean durations does not match the plan")
        return errs

    def to_json(self) -> dict:
        P = self.P
        return {"event": P.event, "solver": self.solver_info,
                "matches": [{"id": P.mid[i], "no": self.number[i], "start": fmt_hhmm(P.t0 + self.start[i]),
                             "end": fmt_hhmm(P.t0 + self.end[i]), "court": self.court[i]}
                            for i in self.by_number],
                "convocations": {P.team_id[t]: fmt_hhmm(c) for t, c in enumerate(self.conv) if c is not None}}

    @staticmethod
    def from_json(P: Problem, doc: dict) -> "Plan":
        by_id = {m["id"]: parse_hhmm(m["start"]) - P.t0 for m in doc["matches"]}
        missing = [m for m in P.mid if m not in by_id]
        if missing:
            sys.exit(f"Plan does not match the tournament file (missing {missing[:3]}...)")
        return Plan(P, [by_id[m] for m in P.mid], doc.get("solver", "loaded plan"))


# --------------------------------------------------------------------------- statistics
GRADES = [(5, "A", "excellent"), (15, "B", "good"), (30, "C", "fair"), (45, "D", "poor"), (INF, "E", "bad")]


def grade(avoidable: float) -> tuple[str, str]:
    for lim, g, txt in GRADES:
        if avoidable <= lim:
            return g, txt
    return "E", "bad"


def ko_paths(P: Problem) -> dict:
    """(series, pool, rank) -> knockout matches a qualifier would play, from entry to final."""
    next_of, entry = {}, {}
    for i, sl in enumerate(P.slots):
        for _, _, src in sl:
            if src.get("type") == "winner":
                next_of[P.mix[src["match"]]] = i
            elif src.get("type") == "pool":
                entry[(P.mseries[i], src["pool"], src["rank"])] = i
    paths = {}
    for key, k in entry.items():
        path = [k]
        while path[-1] in next_of:
            path.append(next_of[path[-1]])
        paths[key] = path
    return paths


def dist(values) -> dict | None:
    if not values:
        return None
    return {"n": len(values), "mean": statistics.mean(values), "median": statistics.median(values),
            "p90": pct(values, 90), "max": max(values), "total": sum(values)}


def compute_stats(P: Problem, W: Weights, start, dur=None, conv=None, paths=None, detail=True) -> dict:
    """Wait / court-use statistics of a timetable (planned or simulated). Times are minutes from t0."""
    dur = dur or P.dur
    end = [s + d for s, d in zip(start, dur)]
    if conv is None:
        conv = [min(start[i] for i in ms) - P.lead if ms else 0 for ms in P.team_matches]
    paths = ko_paths(P) if paths is None else paths
    by_pool: dict[tuple, dict[int, list[int]]] = {}
    for (s, p, r), path in paths.items():
        by_pool.setdefault((s, p), {})[r] = path
    C, mk, busy = P.courts, max(end), sum(dur)
    g0 = P.g0
    met = evaluate(P, W, start, dur, full=True)
    thresholds = [b for b, _ in W.tiers] or [60]

    teams = []
    for t, ms in enumerate(P.team_matches):
        if not ms:
            continue
        ms = sorted(ms, key=start.__getitem__)
        first, pend = start[ms[0]], max(end[i] for i in ms)
        played = sum(dur[i] for i in ms)
        gaps = [start[b] - end[a] for a, b in zip(ms, ms[1:])]
        row = {"team": t, "series": P.team_series[t], "first": first, "conv": conv[t], "pool_end": pend,
               "matches": ms, "played": played, "gaps": gaps, "idle": pend - first - played,
               "stay_out": pend - conv[t], "fin": None, "entry_wait": None}
        for r, path in sorted(by_pool.get((P.team_series[t], P.team_pool[t]), {}).items()):
            entry = start[path[0]] - pend
            ko_gaps = [start[b] - end[a] for a, b in zip(path, path[1:])]
            on_court = played + sum(dur[k] for k in path)
            fin_end = end[path[-1]]
            f = {"rank": r, "entry_wait": entry, "max_ko_gap": max(ko_gaps, default=0), "end": fin_end,
                 "stay": fin_end - conv[t], "span": fin_end - first, "on_court": on_court,
                 "idle": fin_end - first - on_court, "path": path}
            if row["fin"] is None or f["stay"] > row["fin"]["stay"]:
                row["fin"] = f
            row["entry_wait"] = entry if row["entry_wait"] is None else max(row["entry_wait"], entry)
        teams.append(row)

    kinds = ["entry" if P.group_teams[g] else "round" for _, g in P.ko_slots]

    def ko_lists(series=None):
        ee, ew, rr = [], [], []
        for (m, _), kind, xe, xw in zip(P.ko_slots, kinds, met.ko_exp, met.ko_worst):
            if series is not None and P.mseries[m] != series:
                continue
            if kind == "entry":
                ee.append(xe)
                ew.append(xw)
            else:
                rr.append(xe)
        return ee, ew, rr

    def wait_block(rows, ee, ew, rr):
        gaps = [g for r in rows for g in r["gaps"]]
        allg = gaps + ee + rr                 # every wait between two matches (expected for KO entries)
        judged = gaps + ew + rr               # worst cases, used for the limits
        avoid = [max(0.0, g - g0) for g in allg]
        idx = statistics.mean(avoid) if avoid else 0.0
        return {"all": dist(allg), "worst_any": max(judged, default=0), "n_judged": len(judged),
                "over": {b: sum(x > b for x in judged) for b in thresholds},
                "gaps": dist(gaps), "entry_exp": dist(ee), "rounds": dist(rr),
                "idle": dist([r["idle"] for r in rows]), "wait_index": idx, "grade": grade(idx),
                "entry_wait": dist([r["entry_wait"] for r in rows if r["entry_wait"] is not None]),
                "stay_out": dist([r["stay_out"] for r in rows]),
                "stay_fin": dist([r["fin"]["stay"] for r in rows if r["fin"]]),
                "span_fin": dist([r["fin"]["span"] for r in rows if r["fin"]]),
                "play_share_out": statistics.mean([r["played"] / r["stay_out"] for r in rows if r["stay_out"] > 0] or [0]),
                "play_share_fin": statistics.mean([r["fin"]["on_court"] / r["fin"]["stay"]
                                                   for r in rows if r["fin"] and r["fin"]["stay"] > 0] or [0])}

    glob = wait_block(teams, *ko_lists())
    glob["objective"] = met.objective
    glob["components"] = met.components
    glob["day_lb"] = P.day_lb
    pool_stretch = [max(0.0, max(end[i] for i in pm) - min(start[i] for i in pm) - ms)
                    for _, _, pm, ms in P.pools_list]
    pool_min = [ms for _, _, _, ms in P.pools_list]
    series_min = [lb for pm, lb in P.series_pool.values() if pm]
    glob.update(makespan=mk, busy=busy, court_use=busy / (C * mk) if mk else 0,
                idle_court=C * mk - busy, matches_per_hour=P.M / (mk / 60) if mk else 0, min_gap=g0,
                thresholds=thresholds, pool_stretch_total=sum(pool_stretch),
                pool_stretch_rel=sum(pool_stretch) / sum(pool_min) if sum(pool_min) else 0.0,
                pool_stretch_max_rel=max([x / m for x, m in zip(pool_stretch, pool_min) if m] or [0.0]),
                series_stretch_total=met.series_spread,
                series_stretch_rel=met.series_spread / sum(series_min) if sum(series_min) else 0.0)
    # longest stay on site: first match -> end of the final (worst-case path), or pool end
    longest = max(teams, key=lambda r: r["fin"]["span"] if r["fin"] else r["pool_end"] - r["first"])
    glob["longest"] = {"team": longest["team"],
                       "span": longest["fin"]["span"] if longest["fin"] else longest["pool_end"] - longest["first"],
                       "idle": longest["fin"]["idle"] if longest["fin"] else longest["idle"],
                       "first": longest["first"],
                       "end": longest["fin"]["end"] if longest["fin"] else longest["pool_end"]}
    if detail:
        # courts: first moment a court is idle, idle time in the last 2 hours, use per clock hour
        ev = sorted([(e, -1) for e in end] + [(s, 1) for s in start])
        run, full_until, k = 0, 0, 0
        while k < len(ev):
            tt = ev[k][0]
            was_full = run >= C
            while k < len(ev) and ev[k][0] == tt:
                run += ev[k][1]
                k += 1
            if was_full and run < C:
                full_until = tt          # end of the last stretch with every court busy
        tail0 = max(0.0, mk - 120)
        busy_tail = sum(max(0.0, min(e, mk) - max(s, tail0)) for s, e in zip(start, end))
        hours = []
        h = (P.t0 // 60) * 60 - P.t0
        while h < mk:
            a, b = max(h, 0), min(h + 60, mk)
            used = sum(max(0.0, min(e, b) - max(s, a)) for s, e in zip(start, end))
            hours.append((P.t0 + h, used / (C * (b - a)) if b > a else 0))
            h += 60
        glob.update(full_until=full_until, idle_tail=C * (mk - tail0) - busy_tail, hours=hours,
                    conv_first=min(conv[r["team"]] for r in teams), conv_last=max(conv[r["team"]] for r in teams),
                    conv_distinct=len({conv[r["team"]] for r in teams}))
        top = []
        for r in teams:
            ms = r["matches"]
            for a, b, g in zip(ms, ms[1:], r["gaps"]):
                top.append((g, r["team"], a, b))
        glob["top_gaps"] = sorted(top, reverse=True)[:5]
    series = []
    for s in P.series_names:
        rows = [r for r in teams if r["series"] == s]
        ms = [i for i in range(P.M) if P.mseries[i] == s]
        blk = wait_block(rows, *ko_lists(s))
        pm, lb = P.series_pool[s]
        p0, p1 = min(start[i] for i in pm), max(end[i] for i in pm)
        blk.update(name=s, teams=len(rows), matches=len(ms), first=min(start[i] for i in ms),
                   last=max(end[i] for i in ms), pools_start=p0, pools_done=p1,
                   stretch=max(0.0, p1 - p0 - lb), pool_phase_min=lb,
                   stretch_rel=max(0.0, p1 - p0 - lb) / lb if lb else 0.0)
        series.append(blk)
    return {"global": glob, "series": series, "teams": teams if detail else None}


def weights_text(W: Weights) -> str:
    tiers = ", ".join(f">{b}:+{w:g}" for b, w in W.tiers) or "none"
    steps = (", steps [" + ", ".join(f">{b}:{c:g}" for b, c in W.steps) + "]") if W.steps else ""
    return (f"weights: gaps free up to {W.free_wait} min, day length {W.makespan:g}/min, KO wait {W.ko:g}, "
            f"wait tiers [{tiers}]{steps}, "
            f"pool spread {W.pool_spread:g}, series spread {W.series_spread:g}, overtime {W.overtime:g}/min")


def stats_text(P: Problem, W: Weights, plan: Plan, st: dict) -> list[str]:
    g = st["global"]
    T = lambda m: fmt_hhmm(P.t0 + m)                    # noqa: E731
    out = [f"{P.name}: {P.M} matches, {P.T} teams, {P.courts} courts, mean match {P.base_dur} min, "
           f"min rest {P.rest} min",
           f"Solver: {plan.solver_info}", f"  ({weights_text(W)})", "", "SCHEDULE STATISTICS"]
    limit = ""
    if P.end_limit is not None:
        over = g["makespan"] - P.end_limit
        limit = f"   limit {T(P.end_limit)}: " + (f"OVER BY {fmt_dur(over)}" if over > 0 else f"ok, {fmt_dur(-over)} margin")
    out.append(f"  Day            {T(0)} -> {T(g['makespan'])} ({fmt_dur(g['makespan'])}){limit}")
    out.append(f"  Courts         use {g['court_use']:.0%} on average, every court busy until {T(g['full_until'])} "
               f"(end of the last full stretch), idle court time {fmt_dur(g['idle_court'])} "
               f"({fmt_dur(g['idle_tail'])} of it in the last 2 h); {g['matches_per_hour']:.1f} matches/hour")
    out.append("  Use per hour   " + "  ".join(f"{fmt_hhmm(h)[:2]}h {u:.0%}" for h, u in g["hours"]))
    gr, txt = g["grade"]
    out.append(f"  WAIT RATING    {gr} ({txt}): {g['wait_index']:.1f} min of avoidable wait per gap "
               f"(beyond the practical minimum of {g['min_gap']} min)")
    out.append("                 scale: A <= 5, B <= 15, C <= 30, D <= 45, E > 45 min")
    al = g["all"]
    if al:
        out.append(f"  All gaps       any wait between two matches of a team: mean {al['mean']:.0f}, median "
                   f"{al['median']:.0f}, P90 {al['p90']:.0f}, worst {g['worst_any']:.0f} min")
        out.append("  Long gaps      " + " | ".join(f"> {b} min: {n}" for b, n in g["over"].items())
                   + f"   (of {g['n_judged']}; KO entries counted at their worst case)")
    gp = g["gaps"]
    if gp:
        out.append(f"  Pool gaps      between pool matches: mean {gp['mean']:.0f}, median {gp['median']:.0f}, "
                   f"P90 {gp['p90']:.0f}, worst {gp['max']:.0f} min ({gp['n']} gaps)")
    if g["entry_exp"]:
        ee, ew = g["entry_exp"], g["entry_wait"]
        out.append(f"  Before KO      pool -> first KO match: expected mean {ee['mean']:.0f} min; worst case for a "
                   f"team (own last pool match -> first KO match) {ew['max']:.0f} min")
    if g["rounds"]:
        rr = g["rounds"]
        out.append(f"  Between KO     between knockout rounds: mean {rr['mean']:.0f}, median {rr['median']:.0f}, "
                   f"worst {rr['max']:.0f} min ({rr['n']} gaps)")
    idl = g["idle"]
    out.append(f"  Pool idle      per team over its pool phase: mean {idl['mean']:.0f}, median {idl['median']:.0f}, "
               f"P90 {idl['p90']:.0f}, worst {idl['max']:.0f} min; total for all teams {fmt_dur(idl['total'])}")
    out.append(f"  Compactness    pools last {g['pool_stretch_rel']:+.0%} longer than their shortest span "
               f"(worst pool {g['pool_stretch_max_rel']:+.0%}); series pool phases {g['series_stretch_rel']:+.0%} "
               f"longer than their minimum (see table)")
    c = g.get("components")
    if c:
        parts = [("day beyond the minimum", c["day_excess"]), ("overtime", c["overtime"]),
                 ("gaps beyond free wait", c["pool_gaps"]), ("KO waits", c["ko_waits"]), ("wait tiers", c["tiers"]),
                 ("wait steps", c["steps"]), ("pool spread", c["pool_spread"]), ("series spread", c["series_spread"])]
        tot = sum(v for _, v in parts) or 1
        out.append("  Cost split     " + ", ".join(f"{n} {v / tot:.0%}" for n, v in parts if v > 0.5)
                   + f"   (objective {g['objective']:,.0f}, of which {c['day_length'] - c['day_excess']:,.0f} "
                   "is the unavoidable minimum day)")
    so, sf = g["stay_out"], g["stay_fin"]
    line = f"  Time on site   (from convocation) if out after the pools: mean {fmt_dur(so['mean'])}, max {fmt_dur(so['max'])}"
    if sf:
        line += f"; as a finalist (worst path): mean {fmt_dur(sf['mean'])}, max {fmt_dur(sf['max'])}"
    out.append(line)
    share = f"  Playing share  time on court / time on site: {g['play_share_out']:.0%} if out after the pools"
    if sf:
        share += f", {g['play_share_fin']:.0%} for a finalist"
    out.append(share)
    lo = g["longest"]
    t = lo["team"]
    out.append(f"  Longest stay   {P.team_name[t]} ({P.team_series[t]} {P.team_pool[t]}): first match {T(lo['first'])} "
               f"-> {'final' if sf else 'last match'} ends {T(lo['end'])} = {fmt_dur(lo['span'])}, "
               f"of which {fmt_dur(lo['idle'])} waiting")
    out.append(f"  Convocations   {g['conv_distinct']} different times, {T(g['conv_first'])} -> {T(g['conv_last'])}")
    if g["top_gaps"]:
        out.append("  Longest gaps   " + "; ".join(
            f"{gap:.0f} min {P.team_name[t]} (#{plan.number[a]}->#{plan.number[b]})" for gap, t, a, b in g["top_gaps"]))
    sw = max(7, max(len(s) for s in P.series_names) + 1)
    out += ["", f"  {'Series':<{sw}}{'teams':>6}{'matches':>8}{'pool phase':>13}{'stretch':>8}{'last':>7}"
                f"{'mean gap':>9}{'worst gap':>10}{'KO wait':>8}{'KO rounds':>10}{'longest stay':>13}{'rating':>8}"]
    for s in st["series"]:
        kw = f"{s['entry_wait']['max']:.0f}" if s["entry_wait"] else "-"
        kr = f"{s['rounds']['max']:.0f}" if s["rounds"] else "-"
        stay = s["span_fin"]["max"] if s["span_fin"] else (s["last"] - s["first"])
        mg = f"{s['all']['mean']:.0f}" if s["all"] else "-"
        phase = f"{T(s['pools_start'])}-{T(s['pools_done'])}"
        out.append(f"  {s['name']:<{sw}}{s['teams']:>6}{s['matches']:>8}{phase:>13}{format(s['stretch_rel'], '+.0%'):>8}"
                   f"{T(s['last']):>7}{mg:>9}{s['worst_any']:>10.0f}{kw:>8}{kr:>10}{fmt_dur(stay):>13}"
                   f"{s['grade'][0] + ' ' + format(s['wait_index'], '.0f'):>8}")
    out.append("  (stretch = how much longer the pool phase lasts than its shortest possible length; mean/worst gap = any wait between two "
               "matches; KO wait = worst case, own last pool match -> first KO match; KO rounds = worst gap between "
               "knockout rounds; longest stay = first match -> end of the final on the worst path)")
    return out


def stats_json(P: Problem, plan: Plan, st: dict, sim: dict | None, W: Weights | None = None) -> dict:
    g = dict(st["global"])
    lo = g.pop("longest")
    g["longest"] = {"team": P.team_id[lo["team"]], "span": lo["span"], "idle": lo["idle"],
                    "first": fmt_hhmm(P.t0 + lo["first"]), "end": fmt_hhmm(P.t0 + lo["end"])}
    g["hours"] = [{"hour": fmt_hhmm(h), "use": round(u, 3)} for h, u in g["hours"]]
    g["top_gaps"] = [{"minutes": gap, "team": P.team_id[t], "from": plan.number[a], "to": plan.number[b]}
                     for gap, t, a, b in g["top_gaps"]]
    g["grade"] = g["grade"][0]
    g["over"] = {str(k): v for k, v in g["over"].items()}
    for k in ("full_until", "conv_first", "conv_last"):
        g[k] = fmt_hhmm(P.t0 + g[k])
    series = []
    for s in st["series"]:
        s = dict(s)
        s["grade"] = s["grade"][0]
        s["over"] = {str(k): v for k, v in s["over"].items()}
        for k in ("first", "last", "pools_start", "pools_done"):
            s[k] = fmt_hhmm(P.t0 + s[k])
        series.append(s)
    g["day_start"], g["day_end"] = fmt_hhmm(P.t0), fmt_hhmm(P.t0 + g["makespan"])
    info = {"name": P.name, "t0": P.t0, "courts": P.courts, "match_duration": P.base_dur, "min_rest": P.rest,
            "matches": P.M, "teams": P.T, "solver": plan.solver_info,
            "end_limit": fmt_hhmm(P.t0 + P.end_limit) if P.end_limit is not None else None,
            "weights": weights_text(W) if W else None}
    return {"info": info, "plan": g, "series": series, "simulation": sim}


def write_team_stats(P: Problem, plan: Plan, st: dict, path: Path, sep: str):
    T = lambda m: fmt_hhmm(P.t0 + m)                    # noqa: E731
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=sep)
        w.writerow(["Series", "Team id", "Team", "Pool", "Convocation", "First match", "Pool done", "Pool matches",
                    "On court in pools (min)", "Idle in pools (min)", "Longest gap (min)",
                    "On site if out after pools", "Wait before 1st KO match, worst (min)",
                    "Longest gap in KO (min)", "If finalist: final ends", "If finalist: on site",
                    "If finalist: waiting (min)", "If finalist: playing share"])
        for r in sorted(st["teams"], key=lambda r: (P.series_rank[r["series"]], r["conv"], P.team_id[r["team"]])):
            t, fn = r["team"], r["fin"]
            w.writerow([r["series"], P.team_id[t], P.team_name[t], P.team_pool[t] or "", T(r["conv"]),
                        f"#{plan.number[r['matches'][0]]} {T(r['first'])}", T(r["pool_end"]), len(r["matches"]),
                        int(r["played"]), int(r["idle"]), int(max(r["gaps"], default=0)), fmt_dur(r["stay_out"]),
                        "" if r["entry_wait"] is None else int(r["entry_wait"]),
                        "" if not fn else int(fn["max_ko_gap"]), "" if not fn else T(fn["end"]),
                        "" if not fn else fmt_dur(fn["stay"]), "" if not fn else int(fn["idle"]),
                        "" if not fn else f"{fn['on_court'] / fn['stay']:.0%}"])


# --------------------------------------------------------------------------- reporting
def match_teams(P: Problem, plan: Plan, i: int) -> tuple[str, str]:
    if P.teams[i]:
        names = [P.team_name[t] for t in P.teams[i]]
        return names[0], names[1] if len(names) > 1 else ""
    labels = [P.slot_label(lbl, src, plan.number) for lbl, _, src in P.slots[i]]
    return labels[0], labels[1] if len(labels) > 1 else ""


def qualified_next(P: Problem, plan: Plan) -> dict:
    """(series, pool, rank) -> knockout match index."""
    out = {}
    for i, sl in enumerate(P.slots):
        for _, _, src in sl:
            if src.get("type") == "pool":
                out[(P.mseries[i], src["pool"], src["rank"])] = i
    return out


def write_outputs(P: Problem, W: Weights, plan: Plan, out_dir: Path, sep: str):
    out_dir.mkdir(parents=True, exist_ok=True)
    met = evaluate(P, W, plan.start, full=True)
    with open(out_dir / "schedule.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=sep)
        w.writerow(["No", "Start", "End", "Court", "Series", "Stage", "Round", "Pool", "Team 1", "Team 2", "Match id"])
        for i in plan.by_number:
            a, b = match_teams(P, plan, i)
            w.writerow([plan.number[i], fmt_hhmm(P.t0 + plan.start[i]), fmt_hhmm(P.t0 + plan.end[i]),
                        plan.court[i], P.mseries[i], "Pool" if P.stage[i] == "pool" else "KO",
                        f"R{P.round[i]}" if P.stage[i] == "pool" else P.round[i],
                        P.pool[i] or "", a, b, P.mid[i]])
    qn = qualified_next(P, plan)
    max_rank = max([r for (_, _, r) in qn] or [0])
    with open(out_dir / "convocations.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=sep)
        w.writerow(["Series", "Team id", "Team", "Pool", "Convocation", "First match", "Pool matches",
                    "Idle between pool matches (min)", "Pool done"]
                   + [f"If {r}{'st' if r == 1 else 'nd' if r == 2 else 'rd' if r == 3 else 'th'}" for r in range(1, max_rank + 1)])
        rows = sorted(range(P.T), key=lambda t: (P.series_rank[P.team_series[t]], plan.conv[t] or 0, P.team_id[t]))
        for t in rows:
            ms = sorted(P.team_matches[t], key=lambda i: plan.start[i])
            if not ms:
                continue
            listing = ", ".join(f"#{plan.number[i]} {fmt_hhmm(P.t0 + plan.start[i])}" for i in ms)
            nxt = []
            for r in range(1, max_rank + 1):
                k = qn.get((P.team_series[t], P.team_pool[t], r))
                nxt.append(f"#{plan.number[k]} {fmt_hhmm(P.t0 + plan.start[k])} {P.round[k]}" if k is not None else "")
            f0 = ms[0]
            w.writerow([P.team_series[t], P.team_id[t], P.team_name[t], P.team_pool[t] or "",
                        fmt_hhmm(plan.conv[t]), f"#{plan.number[f0]} {fmt_hhmm(P.t0 + plan.start[f0])}",
                        listing, int(met.team_wait[t]), fmt_hhmm(P.t0 + max(plan.end[i] for i in ms))] + nxt)
    (out_dir / "plan.json").write_text(json.dumps(plan.to_json(), indent=1, ensure_ascii=False), encoding="utf-8")
    st = compute_stats(P, W, plan.start, conv=plan.conv_rel())
    write_team_stats(P, plan, st, out_dir / "team_stats.csv", sep)
    write_html(P, plan, met, st, out_dir / "schedule.html")
    return met, st


SERIES_COLORS = ["#2a78d6", "#d6532a", "#2fa36b", "#9b59b6", "#c9a227", "#1fa6b8", "#d63a7a",
                 "#6b7f2a", "#8a5a2b", "#5a6fd6", "#b8461f", "#3d8f8f"]


def write_html(P: Problem, plan: Plan, met: Metrics, st: dict, path: Path):
    ppm = 2.2                     # pixels per minute
    row_h, left, top = 30, 60, 52
    span = met.makespan
    width = int(left + span * ppm + 20)
    color = {s: SERIES_COLORS[k % len(SERIES_COLORS)] for k, s in enumerate(P.series_names)}
    blocks = []
    h0 = (P.t0 // 60) * 60
    for hour in range(h0, P.t0 + int(span) + 60, 60):
        x = left + (hour - P.t0) * ppm
        if x >= left - 1:
            blocks.append(f'<div class="tick" style="left:{x:.0f}px"><span>{fmt_hhmm(hour)}</span></div>')
    for c in range(1, P.courts + 1):
        blocks.append(f'<div class="court" style="top:{top + (c - 1) * row_h}px">Court {c}</div>')
    for i in plan.by_number:
        a, b = match_teams(P, plan, i)
        tip = (f"#{plan.number[i]} {fmt_hhmm(P.t0 + plan.start[i])}-{fmt_hhmm(P.t0 + plan.end[i])} "
               f"{P.mseries[i]} {P.pool[i] or P.round[i]}: {a} vs {b}")
        lab = f"{plan.number[i]}" + (f" {P.mseries[i]}{P.round[i]}" if P.stage[i] == "ko" else "")
        blocks.append(
            f'<div class="m{" ko" if P.stage[i] == "ko" else ""}" title="{html.escape(tip)}" '
            f'style="left:{left + plan.start[i] * ppm:.1f}px;top:{top + (plan.court[i] - 1) * row_h}px;'
            f'width:{P.dur[i] * ppm - 2:.1f}px;background:{color[P.mseries[i]]}">{html.escape(lab)}</div>')
    g = st["global"]
    blocks.append('<div class="court" style="top:22px">Court use</div>')
    for h, u in g["hours"]:
        x0 = left + max(0, h - P.t0) * ppm
        x1 = left + min(g["makespan"], h + 60 - P.t0) * ppm
        shade = int(235 - 135 * u)
        blocks.append(f'<div class="use" style="left:{x0:.0f}px;width:{x1 - x0 - 2:.0f}px;'
                      f'background:rgb({shade},{shade + 10},255)">{u:.0%}</div>')
    legend = " ".join(f'<span class="lg" style="background:{color[s]}">{html.escape(s)}</span>' for s in P.series_names)
    gr, gtxt = g["grade"]
    lo = g["longest"]

    def tile(label, value, sub=""):
        return (f'<div class="tile"><div class="tl">{html.escape(label)}</div><div class="tv">{html.escape(value)}</div>'
                f'<div class="ts">{html.escape(sub)}</div></div>')
    tiles = [tile("Wait rating", f"{gr} - {gtxt}", f"{g['wait_index']:.1f} min avoidable wait per gap"),
             tile("Day", f"{fmt_hhmm(P.t0)} - {fmt_hhmm(P.t0 + g['makespan'])}", fmt_dur(g["makespan"])),
             tile("Court use", f"{g['court_use']:.0%}", f"courts all busy until {fmt_hhmm(P.t0 + g['full_until'])}")]
    if g["all"]:
        b0 = g["thresholds"][0]
        tiles.append(tile("Mean wait between matches", f"{g['all']['mean']:.0f} min",
                          f"worst {g['worst_any']:.0f}, {g['over'][b0]} gaps over {b0} min"))
    tiles.append(tile("Idle per team (pools)", f"{g['idle']['mean']:.0f} min",
                      f"worst {g['idle']['max']:.0f}, total {fmt_dur(g['idle']['total'])}"))
    if g["entry_wait"]:
        tiles.append(tile("Worst wait before KO", f"{g['entry_wait']['max']:.0f} min",
                          f"expected mean {g['entry_exp']['mean']:.0f} min"
                          + (f", between rounds worst {g['rounds']['max']:.0f}" if g["rounds"] else "")))
    tiles.append(tile("Longest stay", fmt_dur(lo["span"]),
                      f"{P.team_name[lo['team']]}, {fmt_dur(lo['idle'])} waiting"))
    so, sf = g["stay_out"], g["stay_fin"]
    tiles.append(tile("On site if out after pools", fmt_dur(so["mean"]),
                      f"playing {g['play_share_out']:.0%} of the time"))
    if sf:
        tiles.append(tile("On site as finalist", fmt_dur(sf["mean"]),
                          f"worst {fmt_dur(sf['max'])}, playing {g['play_share_fin']:.0%}"))
    srows = []
    for s in st["series"]:
        stay = s["span_fin"]["max"] if s["span_fin"] else s["last"] - s["first"]
        cells = [html.escape(s["name"]), s["teams"], s["matches"],
                 f"{fmt_hhmm(P.t0 + s['pools_start'])}-{fmt_hhmm(P.t0 + s['pools_done'])}", f"{s['stretch_rel']:+.0%}",
                 fmt_hhmm(P.t0 + s["last"]), f"{s['all']['mean']:.0f}" if s["all"] else "-",
                 f"{s['worst_any']:.0f}", f"{s['entry_wait']['max']:.0f}" if s["entry_wait"] else "-",
                 f"{s['rounds']['max']:.0f}" if s["rounds"] else "-", fmt_dur(stay),
                 f"<b>{s['grade'][0]}</b> ({s['wait_index']:.0f} min)"]
        srows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    rows = []
    for t in sorted(range(P.T), key=lambda t: (P.series_rank[P.team_series[t]], plan.conv[t] or 0)):
        ms = sorted(P.team_matches[t], key=lambda i: plan.start[i])
        if not ms:
            continue
        rows.append(f"<tr><td>{html.escape(P.team_series[t])}</td><td>{html.escape(P.team_name[t])}</td>"
                    f"<td>{html.escape(P.team_pool[t] or '')}</td><td><b>{fmt_hhmm(plan.conv[t])}</b></td>"
                    f"<td>{', '.join(f'#{plan.number[i]} {fmt_hhmm(P.t0 + plan.start[i])}' for i in ms)}</td>"
                    f"<td>{met.team_wait[t]:.0f}</td></tr>")
    doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{html.escape(P.name)} - schedule</title>
<style>
body{{font-family:system-ui,sans-serif;margin:20px;color:#222}} h1{{font-size:20px}}
.wrap{{overflow-x:auto;border:1px solid #ddd;border-radius:6px;padding:4px}}
.g{{position:relative;height:{top + 6 + P.courts * row_h}px;width:{width}px}}
.use{{position:absolute;top:24px;height:20px;font-size:10px;line-height:20px;text-align:center;border-radius:3px;color:#223}}
.tiles{{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0 18px}}
.tile{{border:1px solid #e3e3e3;border-radius:8px;padding:8px 12px;min-width:150px}}
.tl{{font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.03em}} .tv{{font-size:20px;font-weight:600;margin:2px 0}}
.ts{{font-size:12px;color:#555}}
.tick{{position:absolute;top:0;bottom:0;border-left:1px dashed #ccc}} .tick span{{font-size:11px;color:#666;margin-left:3px}}
.court{{position:absolute;left:0;width:{left - 6}px;font-size:11px;line-height:{row_h - 4}px;color:#444}}
.m{{position:absolute;height:{row_h - 6}px;border-radius:4px;color:#fff;font-size:11px;line-height:{row_h - 6}px;
   overflow:hidden;white-space:nowrap;padding-left:3px;box-sizing:border-box;opacity:.92}}
.m.ko{{outline:2px solid #222;outline-offset:-2px}}
.lg{{color:#fff;padding:2px 8px;border-radius:4px;font-size:12px}}
table{{border-collapse:collapse;font-size:13px;margin-top:16px}} td,th{{border-bottom:1px solid #eee;padding:3px 10px;text-align:left}}
</style></head><body>
<h1>{html.escape(P.name)}</h1>
<p>{P.M} matches, {P.courts} courts, {fmt_hhmm(P.t0)} &rarr; {fmt_hhmm(P.t0 + met.makespan)}. Series: {legend}
&nbsp; (outlined = knockout). Hover a match for details.</p>
<div class="tiles">{''.join(tiles)}</div>
<div class="wrap"><div class="g">{''.join(blocks)}</div></div>
<table><tr><th>Series</th><th>Teams</th><th>Matches</th><th>Pool phase</th><th>Stretch</th><th>Last ends</th>
<th>Mean gap</th><th>Worst gap</th><th>Worst KO wait</th><th>Worst KO round gap</th><th>Longest stay</th><th>Rating</th></tr>
{''.join(srows)}</table>
<table><tr><th>Series</th><th>Team</th><th>Pool</th><th>Convocation</th><th>Pool matches</th><th>Idle (min)</th></tr>
{''.join(rows)}</table></body></html>"""
    path.write_text(doc, encoding="utf-8")


# --------------------------------------------------------------------------- simulation
def simulate(P: Problem, W: Weights, plan: Plan, st_plan: dict, runs: int, cv: float, max_advance, seed: int,
             stop_file=None):
    """Replay the day with random durations; returns (text lines, summary dict)."""
    rng = random.Random(seed)
    sigma = math.sqrt(math.log(1 + cv * cv))
    conv = plan.conv_rel()
    paths = ko_paths(P)
    nb = None if max_advance is None else [s - max_advance for s in plan.start]
    keys = ["makespan", "court_use", "wait_index", "all_mean", "all_p90", "worst_any", "over_top", "gap_mean",
            "gap_max", "entry_mean", "entry_max", "rounds_max", "stay_out_mean", "stay_fin_max", "longest"]
    acc = {k: [] for k in keys}
    delays, first_wait, late15 = [], [], []
    top_b = ([b for b, _ in W.tiers] or [60])[min(2, max(0, len(W.tiers) - 1))]

    def row(st):
        g = st["global"]
        z = lambda d, k: d[k] if d else 0                # noqa: E731
        return {"makespan": g["makespan"], "court_use": g["court_use"], "wait_index": g["wait_index"],
                "all_mean": z(g["all"], "mean"), "all_p90": z(g["all"], "p90"), "worst_any": g["worst_any"],
                "over_top": g["over"].get(top_b, 0),
                "gap_mean": z(g["gaps"], "mean"), "gap_max": z(g["gaps"], "max"),
                "entry_mean": z(g["entry_exp"], "mean"), "entry_max": z(g["entry_wait"], "max"),
                "rounds_max": z(g["rounds"], "max"), "stay_out_mean": g["stay_out"]["mean"],
                "stay_fin_max": z(g["stay_fin"], "max"), "longest": g["longest"]["span"]}

    done_runs = 0
    for _ in range(runs):
        if done_runs >= 10 and stop_requested(stop_file):
            break
        done_runs += 1
        dur = [d * math.exp(rng.gauss(-sigma * sigma / 2, sigma)) if cv > 0 else d for d in P.dur]
        stt = dispatch(P, plan.by_number, dur, ready0=conv, not_before=nb)
        for k, v in row(compute_stats(P, W, stt, dur, conv=conv, paths=paths, detail=False)).items():
            acc[k].append(v)
        d = [stt[i] - plan.start[i] for i in range(P.M)]
        delays.extend(d)
        late15.append(sum(1 for x in d if x > 15) / P.M)
        for t, f in enumerate(plan.first):
            if f is not None:
                first_wait.append(min(stt[i] for i in P.team_matches[t]) - conv[t])
    planned = row(st_plan)
    hh = lambda m: fmt_hhmm(P.t0 + m)                   # noqa: E731
    mn = lambda m: f"{m:.0f} min"                       # noqa: E731
    fmts = {"makespan": ("End of day", hh), "court_use": ("Court use", lambda v: f"{v:.0%}"),
            "wait_index": ("Avoidable wait per gap", lambda v: f"{v:.1f} min {grade(v)[0]}"),
            "all_mean": ("All gaps, mean", mn), "all_p90": ("All gaps, P90", mn), "worst_any": ("All gaps, worst", mn),
            "over_top": (f"Gaps over {top_b} min", lambda v: f"{v:.1f}" if v % 1 else f"{v:.0f}"),
            "gap_mean": ("Pool gap, mean", mn), "gap_max": ("Pool gap, worst", mn),
            "entry_mean": ("Before KO, expected mean", mn), "entry_max": ("Before KO, worst case", mn),
            "rounds_max": ("Between KO rounds, worst", mn),
            "stay_out_mean": ("On site if out after pools, mean", fmt_dur),
            "stay_fin_max": ("On site as finalist, worst", fmt_dur), "longest": ("Longest stay (1st match->final)", fmt_dur)}
    adv = "any time early" if max_advance is None else f"up to {max_advance:g} min early"
    stopped = done_runs < runs
    runs = done_runs
    out = ["", f"SIMULATED DAY ({runs} runs{' - stopped early' if stopped else ''}, durations lognormal with the planned mean and CV {cv:.0%}; "
               f"matches called {adv} when both teams are present and rested)",
           f"  {'':<36}{'plan':>12}{'mean':>12}{'P90':>12}{'worst':>12}"]
    summary = {}
    for k in keys:
        label, f = fmts[k]
        v = acc[k]
        worst = min(v) if k == "court_use" else max(v)
        p90 = pct(v, 10) if k == "court_use" else pct(v, 90)
        out.append(f"  {label:<36}{f(planned[k]):>12}{f(statistics.mean(v)):>12}{f(p90):>12}{f(worst):>12}")
        summary[k] = {"plan": planned[k], "mean": statistics.mean(v), "p90": p90, "worst": worst}
    sg = lambda v: f"{int(round(v)):+d}"                 # noqa: E731
    out.append(f"  Start vs plan: mean {sg(statistics.mean(delays))} min, P10 {sg(pct(delays, 10))}, "
               f"P90 {sg(pct(delays, 90))}, P99 {sg(pct(delays, 99))}; matches > 15 min late: "
               f"{statistics.mean(late15):.0%}")
    out.append(f"  Convocation -> first match: mean {statistics.mean(first_wait):.0f} min (plan {P.lead}), "
               f"P90 {pct(first_wait, 90):.0f}, worst {max(first_wait):.0f}")
    out.append("  (for court use, P90 and worst are the low side)")
    summary.update(runs=runs, cv=cv, max_advance=max_advance, delay_mean=statistics.mean(delays),
                   delay_p90=pct(delays, 90), late15=statistics.mean(late15),
                   first_wait_mean=statistics.mean(first_wait), first_wait_p90=pct(first_wait, 90))
    return out, summary


# --------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tournament", type=Path, help="tournament.json written by tournament_setup.py")
    ap.add_argument("-o", "--out-dir", type=Path, default=None, help="output folder (default: ./schedule_out)")
    g = ap.add_argument_group("event overrides (default: values in the tournament file)")
    g.add_argument("--courts", type=int)
    g.add_argument("--duration", type=int, dest="match_duration", help="mean match duration (min)")
    g.add_argument("--rest", type=int, dest="min_rest", help="TIME_DELTA, min rest between matches (min)")
    g.add_argument("--start", dest="start_time", help="start time HH:MM")
    g.add_argument("--end", dest="end_time", help="end time HH:MM (only reported)")
    g.add_argument("--lead", type=int, dest="convocation_lead", help="convocation lead time (min)")
    g.add_argument("--rounding", type=int, dest="convocation_rounding", help="convocation rounding (min)")
    o = ap.add_argument_group("optimization")
    o.add_argument("--solver", choices=["auto", "cpsat", "heuristic"], default="auto",
                   help="auto = CP-SAT when OR-Tools is installed, else heuristic")
    o.add_argument("--time-limit", type=float, default=60, help="seconds (default 60)")
    o.add_argument("--workers", type=int, default=os.cpu_count() or 4, help="CP-SAT threads")
    o.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1),
                   help="parallel annealing runs for the heuristic (default: CPU count, max 8)")
    o.add_argument("--makespan-weight", type=float, default=None,
                   help="minutes of total team idle time one minute of day length is worth (default: number of teams)")
    o.add_argument("--ko-wait-weight", type=float, default=1.0)
    o.add_argument("--overtime-weight", type=float, default=None,
                   help="per minute the day runs past end_time (default: 10 x the makespan weight; 0 = ignore end_time)")
    o.add_argument("--wait-tiers", default=",".join(f"{b}:{w:g}" for b, w in DEFAULT_TIERS),
                   help="non-linear wait penalty: 'minutes:extra_weight,...' applied to every gap, e.g. "
                        "'60:2,90:6,120:20,180:100' (each minute of a gap beyond 180 costs 1+2+6+20+100); 'none' = linear")
    o.add_argument("--free-wait", type=int, default=60,
                   help="gaps up to this many minutes all cost the same (default 60; 0 = every minute counts)")
    o.add_argument("--wait-steps", default="none",
                   help="fixed cost added to every gap longer than a threshold, e.g. '120:300,180:1000' "
                        "(default none)")
    o.add_argument("--pool-spread-weight", type=float, default=2.0,
                   help="per minute a pool lasts beyond its shortest possible span (default 2)")
    o.add_argument("--series-spread-weight", type=float, default=5.0,
                   help="per minute a series' pool phase lasts beyond its lower bound (default 5)")
    o.add_argument("--free-round-order", action="store_true",
                   help="allow pool rounds to be played out of order (default: round 1, then 2, ...)")
    o.add_argument("--seed", type=int, default=1)
    o.add_argument("--plan", type=Path, help="load a saved plan.json instead of optimizing")
    o.add_argument("--verbose", action="store_true", help="show the CP-SAT search log")
    s = ap.add_argument_group("simulation")
    s.add_argument("--simulate", type=int, default=0, metavar="RUNS", help="Monte Carlo runs (0 = off)")
    s.add_argument("--duration-cv", type=float, default=0.25, help="coefficient of variation of match durations")
    s.add_argument("--max-advance", type=float, default=None,
                   help="max minutes a match may be called before its planned time (default: no limit)")
    ap.add_argument("--csv-sep", default=",", help="CSV separator (use ';' for French Excel)")
    ap.add_argument("--stop-file", type=Path, default=None,
                    help="when this file appears, stop optimizing and keep the best plan so far "
                         "(default: STOP in the output folder)")
    args = ap.parse_args(argv)

    doc = json.loads(args.tournament.read_text(encoding="utf-8"))
    ov = {k: getattr(args, k) for k in ("courts", "match_duration", "min_rest", "start_time",
                                         "end_time", "convocation_lead", "convocation_rounding")}
    P = Problem(doc, ov, round_order=not args.free_round_order)
    W = P.default_weights()
    if args.makespan_weight is not None:
        W.makespan = args.makespan_weight
        W.overtime = 10.0 * W.makespan
    W.ko, W.tiers = args.ko_wait_weight, parse_tiers(args.wait_tiers)
    W.free_wait, W.steps = args.free_wait, parse_tiers(args.wait_steps)
    if args.overtime_weight is not None:
        W.overtime = args.overtime_weight
    W.pool_spread, W.series_spread = args.pool_spread_weight, args.series_spread_weight
    out_dir = args.out_dir or Path("schedule_out")

    if args.plan:
        plan = Plan.from_json(P, json.loads(args.plan.read_text(encoding="utf-8")))
    else:
        solver = args.solver
        if solver in ("auto", "cpsat"):
            try:
                import ortools  # noqa: F401
                solver = "cpsat"
            except ImportError:
                if args.solver == "cpsat":
                    sys.exit("OR-Tools is not installed: pip install ortools (or use --solver heuristic)")
                solver = "heuristic"
        out_dir.mkdir(parents=True, exist_ok=True)
        stop_file = str(args.stop_file or (out_dir / "STOP"))
        if os.path.exists(stop_file):
            os.remove(stop_file)
        print(f"Optimizing {P.M} matches, {P.T} teams, {P.courts} courts ({solver}, {args.time_limit:g}s)... "
              f"[to stop early and keep the best plan: create {stop_file}]", flush=True)
        t_heur = args.time_limit if solver == "heuristic" else 0.5 * args.time_limit
        best_order, best_c = heuristic(P, W, t_heur, max(1, args.jobs), args.seed, stop_file=stop_file)
        start = dispatch(P, best_order, planning=True)
        info = "heuristic (dispatch + simulated annealing)"
        stopped = stop_requested(stop_file)
        if solver == "cpsat" and not stopped:
            res, status = solve_cpsat(P, W, start, max(1.0, args.time_limit - t_heur), args.workers, args.verbose,
                                      stop_file=stop_file)
            if res is not None and evaluate(P, W, res) <= best_c:
                start, info = res, f"CP-SAT ({status})"
            else:
                info += f" (CP-SAT {status} found nothing better)"
        if stopped or stop_requested(stop_file):
            info += " - stopped early"
            print("Stopped on request: using the best plan found so far.", flush=True)
        if os.path.exists(stop_file):
            os.remove(stop_file)                       # consumed: a new request can stop the simulation
        plan = Plan(P, start, info)

    errs = plan.validate()
    if errs:
        print("\nPLAN CHECK FAILED:")
        for e in errs[:20]:
            print("  -", e)
        sys.exit(1)
    met, st = write_outputs(P, W, plan, out_dir, args.csv_sep)
    lines = stats_text(P, W, plan, st)
    print("\n" + "\n".join(lines))
    sim = None
    if args.simulate:
        sim_lines, sim = simulate(P, W, plan, st, args.simulate, args.duration_cv, args.max_advance, args.seed,
                                  stop_file=str(args.stop_file or (out_dir / "STOP")))
        print("\n".join(sim_lines))
        lines += sim_lines
    sf = Path(args.stop_file or (out_dir / "STOP"))
    if sf.exists():
        sf.unlink()
    (out_dir / "statistics.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "statistics.json").write_text(json.dumps(stats_json(P, plan, st, sim, W), indent=1, default=str),
                                             encoding="utf-8")
    print(f"\nPlan checked (courts, rests, stage order, replay). Files in {out_dir}/: schedule.csv, "
          "convocations.csv, team_stats.csv, statistics.txt, statistics.json, schedule.html, plan.json")


if __name__ == "__main__":
    main()
