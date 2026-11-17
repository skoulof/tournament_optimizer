#!/usr/bin/env python3
"""
tournament_setup.py - propose a format for every series of a badminton
tournament (pools -> knockout, or a single pool for small series), check that
the whole event fits in the day, and write the explicit match list that
scheduler.py optimizes.

    python tournament_setup.py event.toml                  # propose + write tournament.json
    python tournament_setup.py event.toml --pick B=2       # force the 2nd proposal for series B
    python tournament_setup.py event.toml -o other.json --top 8

A "team" is whatever plays a match (a single player or a pair).
Config can be TOML (built in), JSON, or YAML (needs PyYAML).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from dataclasses import dataclass
from pathlib import Path

FORMAT_VERSION = 1

DEFAULT_EVENT = {
    "name": "Badminton tournament",
    "start_time": "09:00",
    "end_time": None,          # optional: used to check that everything fits
    "courts": 7,
    "match_duration": 30,      # mean minutes per match, warm-up and changeover included
    "min_rest": 20,            # TIME_DELTA: minutes between the end of a match and the team's next start
    "convocation_lead": 30,    # convocation = first match time - lead
    "convocation_rounding": 5, # convocation times are rounded down to this many minutes
}

DEFAULT_SETUP = {
    "pool_sizes": [3, 4, 5],        # allowed pool sizes
    "preferred_pool_size": 4,
    "qualifiers_per_pool": [1, 2],  # allowed numbers of teams leaving each pool
    "preferred_qualifiers": 2,
    "single_pool_max": 6,           # series up to this size may play a single round robin
    "min_matches_per_team": 2,      # guaranteed matches (pool phase)
    "planning_efficiency": 0.85,    # share of court time realistically usable (KO tails, rest gaps)
    "weights": {"pool_size": 1.0, "qualifiers": 1.5, "byes": 2.0},
}

ROUND_NAMES = {2: "F", 4: "SF", 8: "QF"}
ROUND_LONG = {"F": "Final", "SF": "Semi-final", "QF": "Quarter-final"}
RANK_LABEL = {1: "1st", 2: "2nd", 3: "3rd"}


# --------------------------------------------------------------------------- utils
def load_config(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".toml":
        import tomllib
        return tomllib.loads(text)
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError:
            sys.exit("Reading YAML needs PyYAML (pip install pyyaml); TOML and JSON work without it.")
        return yaml.safe_load(text) or {}
    if suffix == ".json":
        return json.loads(text)
    sys.exit(f"Unsupported config format '{suffix}' (use .toml, .json, .yaml)")


def parse_hhmm(value) -> int:
    h, m = str(value).strip().split(":")
    return int(h) * 60 + int(m)


def fmt_hhmm(minutes: float) -> str:
    minutes = int(round(minutes))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def fmt_dur(minutes: float) -> str:
    minutes = int(round(minutes))
    return f"{minutes // 60}h{minutes % 60:02d}"


def rank_label(rank: int) -> str:
    return RANK_LABEL.get(rank, f"{rank}th")


def round_name(lines: int) -> str:
    return ROUND_NAMES.get(lines, f"R{lines}")


def pool_rounds(size: int) -> int:
    """Rounds needed for a round robin of `size` teams."""
    return size - 1 if size % 2 == 0 else size


# --------------------------------------------------------------------------- formats
@dataclass
class Candidate:
    kind: str                      # "single_pool" | "pools_ko"
    n_teams: int
    pool_sizes: list[int]
    qualifiers: int = 0            # per pool
    bracket_size: int = 0
    byes: int = 0
    pool_matches: int = 0
    ko_matches: int = 0
    ko_rounds: int = 0
    min_matches: int = 0           # guaranteed matches per team
    max_matches: int = 0           # matches of a finalist
    court_minutes: int = 0
    critical_path: int = 0         # shortest possible duration of the series (unlimited courts)
    penalty: float = 0.0
    forced: bool = False

    @property
    def matches(self) -> int:
        return self.pool_matches + self.ko_matches

    @property
    def n_pools(self) -> int:
        return len(self.pool_sizes)

    def describe(self) -> str:
        if self.kind == "single_pool":
            return f"single pool of {self.n_teams}"
        counts: dict[int, int] = {}
        for s in self.pool_sizes:
            counts[s] = counts.get(s, 0) + 1
        sizes = " + ".join(f"{c}x{s}" for s, c in sorted(counts.items(), reverse=True))
        q = self.n_pools * self.qualifiers
        bye = f", {self.byes} bye{'s' if self.byes > 1 else ''}" if self.byes else ""
        return (f"{self.n_pools} pools ({sizes}), top {self.qualifiers} -> "
                f"KO of {q} (draw {self.bracket_size}{bye})")

    def format_dict(self) -> dict:
        d = {"type": self.kind, "pool_sizes": self.pool_sizes, "description": self.describe()}
        if self.kind == "pools_ko":
            d.update(qualifiers_per_pool=self.qualifiers, bracket_size=self.bracket_size,
                     byes=self.byes, ko_rounds=self.ko_rounds)
        return d


def split_evenly(n: int, pools: int) -> list[int]:
    base, extra = divmod(n, pools)
    return [base + 1] * extra + [base] * (pools - extra)   # bigger pools first


def evaluate(c: Candidate, duration: int, rest: int, st: dict) -> Candidate:
    w = {**DEFAULT_SETUP["weights"], **st.get("weights", {})}
    c.pool_matches = sum(s * (s - 1) // 2 for s in c.pool_sizes)
    r = max(pool_rounds(s) for s in c.pool_sizes)
    if c.kind == "pools_ko":
        q_total = c.n_pools * c.qualifiers
        c.bracket_size = 1 << math.ceil(math.log2(q_total))
        c.byes = c.bracket_size - q_total
        c.ko_matches = q_total - 1
        c.ko_rounds = int(math.log2(c.bracket_size))
    c.min_matches = min(c.pool_sizes) - 1
    c.max_matches = max(c.pool_sizes) - 1 + c.ko_rounds
    c.court_minutes = c.matches * duration
    c.critical_path = r * duration + (r - 1) * rest + c.ko_rounds * (rest + duration)

    pen = sum(w["pool_size"] * abs(s - st["preferred_pool_size"]) for s in c.pool_sizes)
    if c.kind == "pools_ko":
        pen += w["qualifiers"] * abs(c.qualifiers - st["preferred_qualifiers"])
        pen += w["byes"] * c.byes / c.bracket_size
    else:
        pen = 0.0
    c.penalty = round(pen + 1e-5 * c.court_minutes, 6)   # tiny tie-break: shorter wins
    return c


def candidates_for(n: int, duration: int, rest: int, st: dict) -> list[Candidate]:
    sizes_ok = set(st["pool_sizes"])
    lo, hi = min(sizes_ok), max(sizes_ok)
    out: list[Candidate] = []
    for pools in range(max(2, math.ceil(n / hi)), n // lo + 1):
        sizes = split_evenly(n, pools)
        if not set(sizes) <= sizes_ok or min(sizes) - 1 < st["min_matches_per_team"]:
            continue
        for q in st["qualifiers_per_pool"]:
            if q < 1 or q >= min(sizes) or pools * q < 2:
                continue
            out.append(evaluate(Candidate("pools_ko", n, sizes, qualifiers=q), duration, rest, st))
    if n >= 2 and (n <= st["single_pool_max"] or not out):
        out.append(evaluate(Candidate("single_pool", n, [n]), duration, rest, st))
    out.sort(key=lambda c: (c.penalty, c.court_minutes))
    return out


# --------------------------------------------------------------------------- building
def serpentine(team_ids: list[str], sizes: list[int]) -> list[list[str]]:
    """Distribute seeded teams over pools in snake order, respecting pool sizes."""
    pools: list[list[str]] = [[] for _ in sizes]
    it = iter(team_ids)
    row = 0
    remaining = len(team_ids)
    while remaining:
        order = range(len(sizes)) if row % 2 == 0 else range(len(sizes) - 1, -1, -1)
        for p in order:
            if remaining and len(pools[p]) < sizes[p]:
                pools[p].append(next(it))
                remaining -= 1
        row += 1
    return pools


def round_robin(teams: list[str]) -> list[list[tuple[str, str]]]:
    """Circle method. The top-seeded pair meets in the last round."""
    t: list[str | None] = list(teams)
    if len(t) % 2:
        t.append(None)
    n = len(t)
    rounds = []
    for _ in range(n - 1):
        pairs = [(t[i], t[n - 1 - i]) for i in range(n // 2)]
        rounds.append([(a, b) for a, b in pairs if a is not None and b is not None])
        t = [t[0], t[-1]] + t[1:-1]
    return rounds


def seed_order(size: int) -> list[int]:
    """Standard bracket line order of seeds, e.g. 8 -> [1, 8, 4, 5, 2, 7, 3, 6]."""
    order = [1]
    while len(order) < size:
        m = len(order) * 2
        order = [x for s in order for x in (s, m + 1 - s)]
    return order


def meet_round(a: int, b: int) -> int:
    """Round (1 = first) in which bracket lines a and b would meet."""
    return (a ^ b).bit_length()


def place_qualifiers(n_pools: int, q: int, size: int) -> dict[int, tuple[int, int]]:
    """Map bracket line -> (pool index, rank). Pool winners take seeds 1..P (byes go to
    the top seeds); lower ranks are arranged to meet their own pool's teams as late as possible."""
    order = seed_order(size)
    line_of_seed = {s: i for i, s in enumerate(order)}
    placed: dict[int, tuple[int, int]] = {}
    for p in range(n_pools):
        placed[line_of_seed[p + 1]] = (p, 1)
    for rank in range(2, q + 1):
        lines = [line_of_seed[s] for s in range((rank - 1) * n_pools + 1, rank * n_pools + 1)]
        same_pool = {p: [ln for ln, (pp, _) in placed.items() if pp == p] for p in range(n_pools)}

        def quality(assign):
            rounds = [min(meet_round(assign[p], o) for o in same_pool[p]) for p in range(n_pools)]
            return (min(rounds), sum(rounds))

        assign = {p: lines[n_pools - 1 - p] for p in range(n_pools)}   # reversed: good start
        best = quality(assign)
        improved = True
        while improved:
            improved = False
            for a in range(n_pools):
                for b in range(a + 1, n_pools):
                    assign[a], assign[b] = assign[b], assign[a]
                    qa = quality(assign)
                    if qa > best:
                        best, improved = qa, True
                    else:
                        assign[a], assign[b] = assign[b], assign[a]
        for p, ln in assign.items():
            placed[ln] = (p, rank)
    return placed


def build_series(name: str, team_names: list[str], cand: Candidate, duration: int | None):
    teams = [{"id": f"{name}-{i + 1:02d}", "name": tn, "seed": i + 1} for i, tn in enumerate(team_names)]
    ids = [t["id"] for t in teams]
    groups = serpentine(ids, cand.pool_sizes)
    pools, matches = [], []
    pool_match_ids: list[list[str]] = []
    for p, members in enumerate(groups):
        pname = "P" if cand.kind == "single_pool" else f"P{p + 1}"
        pools.append({"name": pname, "teams": members})
        for t in teams:
            if t["id"] in members:
                t["pool"] = pname
        mids, k = [], 0
        for r, pairs in enumerate(round_robin(members), start=1):
            for a, b in pairs:
                k += 1
                mid = f"{name}-{pname}-{k}"
                matches.append({"id": mid, "series": name, "stage": "pool", "pool": pname,
                                "round": r, "teams": [a, b]})
                mids.append(mid)
        pool_match_ids.append(mids)

    if cand.kind == "pools_ko":
        placed = place_qualifiers(cand.n_pools, cand.qualifiers, cand.bracket_size)
        current = []
        for line in range(cand.bracket_size):
            if line in placed:
                p, rank = placed[line]
                current.append({"label": f"{rank_label(rank)} {pools[p]['name']}",
                                "after": pool_match_ids[p],
                                "source": {"type": "pool", "pool": pools[p]["name"], "rank": rank}})
            else:
                current.append(None)   # bye
        rnd = 0
        while len(current) > 1:
            rnd += 1
            rname = round_name(len(current))
            nxt, k = [], 0
            for i in range(0, len(current), 2):
                a, b = current[i], current[i + 1]
                if a is None or b is None:
                    nxt.append(a or b)
                    continue
                k += 1
                mid = f"{name}-{rname}-{k}" if rname != "F" else f"{name}-F"
                matches.append({"id": mid, "series": name, "stage": "ko", "round": rname,
                                "round_index": rnd, "slots": [a, b]})
                nxt.append({"label": f"Winner {mid}", "after": [mid],
                            "source": {"type": "winner", "match": mid}})
            current = nxt

    series = {"name": name, "n_teams": len(teams), "match_duration": duration,
              "format": cand.format_dict(), "teams": teams, "pools": pools}
    return series, matches


# --------------------------------------------------------------------------- fit
def estimate_day(chosen: dict[str, Candidate], courts: int, eff: float) -> float:
    court_min = sum(c.court_minutes for c in chosen.values())
    longest = max((c.critical_path for c in chosen.values()), default=0)
    return max(court_min / (courts * eff), longest)


def fit_to_day(chosen, options, courts, eff, available):
    """Greedy: while the estimate exceeds the day, switch the series whose cheaper option
    costs the least format quality per minute saved. Does nothing if even the shortest
    formats cannot fit (then more courts or a longer day are needed)."""
    changes = []
    cheapest = {n: (c if c.forced else min(options[n], key=lambda o: o.court_minutes)) for n, c in chosen.items()}
    if estimate_day(cheapest, courts, eff) > available:
        return None
    while estimate_day(chosen, courts, eff) > available:
        best = None
        for name, cur in chosen.items():
            if cur.forced:
                continue
            for alt in options[name]:
                saved = cur.court_minutes - alt.court_minutes
                if saved <= 0:
                    continue
                ratio = (alt.penalty - cur.penalty) / saved
                if best is None or ratio < best[0]:
                    best = (ratio, name, alt)
        if best is None:
            break
        _, name, alt = best
        changes.append((name, chosen[name], alt))
        chosen[name] = alt
    return changes


# --------------------------------------------------------------------------- example generator
RANKS = ["N1", "N2", "N3", "R4", "R5", "R6", "D7", "D8", "D9", "P10", "P11", "P12", "NC"]
DISCIPLINES = {"SH": "men's singles", "SD": "women's singles", "DH": "men's doubles",
               "DD": "women's doubles", "MX": "mixed doubles"}
_MEN = ("Lucas Hugo Louis Gabriel Arthur Jules Adam Raphaël Léo Nathan Tom Théo Paul Noah Ethan Maël Sacha Mathis "
        "Nolan Baptiste Antoine Maxime Thomas Alexandre Julien Nicolas Romain Quentin Clément Victor Martin Simon "
        "Samuel Axel Enzo Timéo Kylian Robin Mathéo Aurélien Benjamin Florian Guillaume Pierre Vincent Damien Olivier "
        "Yann Loïc Bastien Adrien Rémi Cédric Sébastien Corentin Gaël Hadrien Erwan Malo Lilian").split()
_WOMEN = ("Emma Jade Louise Alice Chloé Lina Léa Rose Anna Mia Inès Ambre Julia Manon Zoé Camille Lou Léna Sarah "
          "Clara Juliette Agathe Eva Margaux Pauline Marie Laura Lucie Mathilde Charlotte Elise Océane Anaïs Justine "
          "Morgane Émilie Audrey Céline Hélène Nina Romane Maëlys Lola Élodie Claire Sophie Aurélie Noémie Capucine "
          "Victoire Salomé Gaëlle Maïwenn Apolline Coline Estelle Héloïse Solène Ludivine Cassandra").split()
_LAST = ("MARTIN BERNARD THOMAS PETIT ROBERT RICHARD DURAND DUBOIS MOREAU LAURENT SIMON MICHEL LEFEBVRE LEROY ROUX "
         "DAVID BERTRAND MOREL FOURNIER GIRARD BONNET DUPONT LAMBERT FONTAINE ROUSSEAU VINCENT MULLER LEFEVRE FAURE "
         "ANDRE MERCIER BLANC GUERIN BOYER GARNIER CHEVALIER FRANCOIS LEGRAND GAUTHIER GARCIA PERRIN ROBIN CLEMENT "
         "MORIN NICOLAS HENRY ROUSSEL MATHIEU GAUTIER MASSON MARCHAND DUVAL DENIS DUMONT LEMAIRE NOEL MEYER DUFOUR "
         "MEUNIER BRUN BLANCHARD GIRAUD JOLY RIVIERE LUCAS BRUNET GAILLARD BARBIER ARNAUD MARTINEZ GERARD ROCHE "
         "RENARD SCHMITT ROY LEROUX COLIN VIDAL CARON PICARD ROGER FABRE AUBERT LEMOINE RENAUD DUMAS LACROIX OLIVIER "
         "PHILIPPE BOURGEOIS BENOIT REY LECLERC PAYET ROLLAND GUILLAUME LECOMTE HUBERT LE-GALL KERGOAT").split()
_LEVEL_FACTOR = {"N": 0.45, "R": 0.75, "D": 1.0, "P": 1.0, "NC": 0.6}
_DISC_FACTOR = {"SH": 1.15, "SD": 0.6, "DH": 1.0, "DD": 0.6, "MX": 0.95}


def rank_groups(best: str = "R4", worst: str = "NC", per_series: int = 2) -> list[list[str]]:
    """Consecutive FFBaD rankings grouped per series, best first: R4..NC by 2 -> R4/R5, R6/D7, ..., P12/NC."""
    i, j = RANKS.index(best), RANKS.index(worst)
    if i > j:
        i, j = j, i
    span = RANKS[i:j + 1]
    per_series = max(1, int(per_series))
    return [span[k:k + per_series] for k in range(0, len(span), per_series)]


def generate_example(disciplines=("SH", "SD", "DH", "DD", "MX"), best="R4", worst="NC", ranks_per_series=2,
                     n_series=None, min_teams=6, max_teams=24, names=True, seed=None) -> dict:
    """Random FFBaD-like event: one series per discipline and ranking group (e.g. 'SH R4/R5', 'MX P12/NC'),
    with team counts that are larger in the popular levels/disciplines. Team names are invented."""
    rng = random.Random(seed)
    disciplines = [d for d in DISCIPLINES if d in set(disciplines)] or list(DISCIPLINES)
    groups = rank_groups(best, worst, ranks_per_series)
    combos = [(g, d) for g in groups for d in disciplines]
    if n_series and 0 < int(n_series) < len(combos):
        keep = set(rng.sample(range(len(combos)), int(n_series)))
        combos = [c for k, c in enumerate(combos) if k in keep]
    lo, hi = sorted((max(2, int(min_teams)), max(2, int(max_teams))))
    used: set[str] = set()

    def person(women: bool) -> tuple[str, str]:
        for _ in range(200):
            first, last = rng.choice(_WOMEN if women else _MEN), rng.choice(_LAST)
            if (first, last) not in used:
                used.add((first, last))
                return first, last
        first, last = rng.choice(_WOMEN if women else _MEN), rng.choice(_LAST) + f"-{len(used)}"
        used.add((first, last))
        return first, last

    series = []
    for group, disc in combos:
        level = "NC" if group[0] == "NC" else group[0][0]
        f = _LEVEL_FACTOR[level] * _DISC_FACTOR[disc]
        n = round(lo + (hi - lo) * min(1.0, rng.random() * f * 1.1))
        n = max(lo, min(hi, n))
        name = f"{disc} {'/'.join(group)}"
        s = {"name": name}
        if names:
            teams = []
            for _ in range(n):
                if disc in ("SH", "SD"):
                    first, last = person(disc == "SD")
                    teams.append(f"{first} {last}")
                else:
                    a = person(disc == "DD")
                    b = person(disc in ("DD", "MX"))
                    teams.append(f"{a[0][0]}. {a[1]} / {b[0][0]}. {b[1]}")
            s["teams"] = teams
        else:
            s["teams"] = n
        series.append(s)
    return {"series": series}


# --------------------------------------------------------------------------- API
class SetupError(ValueError):
    """Invalid configuration (message is meant for the user)."""


def normalize_config(cfg: dict) -> dict:
    """Config with every default filled in: {'event': ..., 'setup': ..., 'series': [...]}"""
    ev = {**DEFAULT_EVENT, **(cfg.get("event") or {})}
    st = {**DEFAULT_SETUP, **(cfg.get("setup") or {})}
    st["weights"] = {**DEFAULT_SETUP["weights"], **(st.get("weights") or {})}
    series = [dict(s) for s in (cfg.get("series") or [])]
    return {"event": ev, "setup": st, "series": series}


def candidate_dict(c: Candidate, index: int) -> dict:
    return {"index": index, "kind": c.kind, "description": c.describe(), "pool_sizes": c.pool_sizes,
            "n_pools": c.n_pools, "qualifiers": c.qualifiers, "bracket_size": c.bracket_size, "byes": c.byes,
            "matches": c.matches, "pool_matches": c.pool_matches, "ko_matches": c.ko_matches,
            "min_matches": c.min_matches, "max_matches": c.max_matches, "court_minutes": c.court_minutes,
            "critical_path": c.critical_path, "penalty": c.penalty}


def run_setup(cfg: dict, picks: dict | None = None, no_fit: bool = False) -> dict:
    """Propose formats for every series and build the tournament document.
    picks: {series name: proposal number (1-based) or its description}. Raises SetupError on bad input."""
    cfg = normalize_config(cfg)
    ev, st, series_cfg = cfg["event"], cfg["setup"], cfg["series"]
    picks = picks or {}
    if not series_cfg:
        raise SetupError("No series defined.")
    try:
        duration, rest, courts = int(ev["match_duration"]), int(ev["min_rest"]), int(ev["courts"])
        parse_hhmm(ev["start_time"])
        if ev.get("end_time"):
            parse_hhmm(ev["end_time"])
    except (ValueError, TypeError, KeyError) as e:
        raise SetupError(f"Invalid event settings: {e}") from e
    if courts < 1 or duration < 1:
        raise SetupError("Courts and match duration must be at least 1.")
    if not st["pool_sizes"] or not st["qualifiers_per_pool"]:
        raise SetupError("Allowed pool sizes and qualifiers per pool must not be empty.")
    options: dict[str, list[Candidate]] = {}
    chosen: dict[str, Candidate] = {}
    names: dict[str, list[str]] = {}
    durations: dict[str, int | None] = {}

    for s in series_cfg:
        name = str(s.get("name", "")).strip()
        if not name:
            raise SetupError("Every series needs a name.")
        if name in options:
            raise SetupError(f"Series name '{name}' is used twice.")
        teams = s.get("teams")
        if isinstance(teams, list):
            names[name] = [str(t).strip() for t in teams if str(t).strip()]
        else:
            try:
                names[name] = [f"{name}-{i + 1:02d}" for i in range(int(teams))]
            except (TypeError, ValueError) as e:
                raise SetupError(f"Series {name}: 'teams' must be a number or a list of names.") from e
        n = len(names[name])
        if n < 2:
            raise SetupError(f"Series {name} needs at least 2 teams.")
        md = s.get("match_duration")
        durations[name] = int(md) if md not in (None, "", 0) else None
        sd = durations[name] or duration
        opts = candidates_for(n, sd, rest, st)
        # explicit format requests in the config
        if s.get("single_pool") is True:
            opts = [c for c in opts if c.kind == "single_pool"] or \
                   [evaluate(Candidate("single_pool", n, [n]), sd, rest, st)]
        if s.get("single_pool") is False:
            opts = [c for c in opts if c.kind != "single_pool"]
        if s.get("pools") not in (None, ""):
            opts = [c for c in opts if c.kind == "pools_ko" and c.n_pools == int(s["pools"])]
        if s.get("qualifiers") not in (None, ""):
            opts = [c for c in opts if c.kind == "pools_ko" and c.qualifiers == int(s["qualifiers"])]
        if not opts:
            raise SetupError(f"Series {name}: no format matches the constraints given.")
        options[name] = opts
        forced = (s.get("pools") not in (None, "") or s.get("qualifiers") not in (None, "")
                  or s.get("single_pool") in (True, False))
        pick = picks.get(name)
        if isinstance(pick, str) and not pick.strip().isdigit():
            # pick given by its description (stable when the ranking changes); ignored if gone
            match = [c for c in opts if c.describe() == pick]
            pick = opts.index(match[0]) + 1 if match else None
        if pick:
            idx = int(pick)
            if not 1 <= idx <= len(opts):
                raise SetupError(f"Series {name}: proposal {idx} does not exist (only {len(opts)}).")
            chosen[name] = opts[idx - 1]
            forced = True
        else:
            chosen[name] = opts[0]
        chosen[name].forced = forced

    eff = float(st["planning_efficiency"])
    available = None
    if ev.get("end_time"):
        available = parse_hhmm(ev["end_time"]) - parse_hhmm(ev["start_time"])
    changes, impossible = [], False
    if available and not no_fit:
        changes = fit_to_day(chosen, options, courts, eff, available)
        if changes is None:
            changes, impossible = [], True

    out_series, out_matches = [], []
    for s in series_cfg:
        name = str(s["name"]).strip()
        ser, mts = build_series(name, names[name], chosen[name], durations[name])
        out_series.append(ser)
        out_matches.extend(mts)
    doc = {"format_version": FORMAT_VERSION, "generated_by": "tournament_setup.py",
           "event": {k: ev[k] for k in DEFAULT_EVENT}, "series": out_series, "matches": out_matches}

    court_min = sum(c.court_minutes for c in chosen.values())
    est = estimate_day(chosen, courts, eff)
    start = parse_hhmm(ev["start_time"])
    estimate = {"matches": sum(c.matches for c in chosen.values()), "court_minutes": court_min,
                "lower_bound": court_min / courts, "estimated_day": est, "efficiency": eff,
                "estimated_end": fmt_hhmm(start + est), "available": available,
                "fits": None if available is None else est <= available, "impossible": impossible,
                "courts_needed": math.ceil(court_min / (eff * available)) if available else None,
                "longest_series": max(c.critical_path for c in chosen.values())}
    return {"config": cfg, "estimate": estimate, "doc": doc,
            "series": [{"name": name, "n_teams": len(names[name]), "forced": chosen[name].forced,
                        "chosen": options[name].index(chosen[name]) + 1,
                        "options": [candidate_dict(c, i + 1) for i, c in enumerate(options[name])]}
                       for name in options],
            "changes": [{"series": name, "from": old.describe(), "to": new.describe()}
                        for name, old, new in changes]}


def report_text(res: dict, top: int = 5) -> str:
    ev, est = res["config"]["event"], res["estimate"]
    courts = int(ev["courts"])
    out = [f"Event: {ev['name']}   courts: {courts}   mean match: {ev['match_duration']} min   "
           f"min rest: {ev['min_rest']} min   start: {ev['start_time']}"
           + (f"   end: {ev['end_time']}" if ev.get("end_time") else ""), ""]
    for s in res["series"]:
        out.append(f"Series {s['name']}  ({s['n_teams']} teams)")
        out.append(f"   {'#':>2}  {'format':<60} {'matches':>7} {'per team':>9} {'court time':>10} "
                   f"{'min length':>10} {'penalty':>7}")
        shown = s["options"][: max(top, 1)]
        if s["chosen"] > len(shown):
            shown = shown + [s["options"][s["chosen"] - 1]]
        for c in shown:
            mark = "*" if c["index"] == s["chosen"] else " "
            per_team = (f"{c['min_matches']}-{c['max_matches']}" if c["max_matches"] != c["min_matches"]
                        else f"{c['min_matches']}")
            out.append(f" {mark} {c['index']:>2}  {c['description']:<60} {c['matches']:>7} {per_team:>9} "
                       f"{fmt_dur(c['court_minutes']):>10} {fmt_dur(c['critical_path']):>10} {c['penalty']:>7.2f}")
        if len(s["options"]) > len(shown):
            out.append(f"       ... {len(s['options']) - len(shown)} more (use --top)")
        out.append("")
    for ch in res["changes"]:
        out.append(f"! To fit the day, series {ch['series']} switched from '{ch['from']}' to '{ch['to']}'")
    out.append(f"Chosen (*): {est['matches']} matches, {fmt_dur(est['court_minutes'])} of court time, "
               f"lower bound {fmt_dur(est['lower_bound'])} on {courts} courts, "
               f"estimated day ~{fmt_dur(est['estimated_day'])} (efficiency {est['efficiency']:.0%})")
    if est["available"]:
        out.append(f"Estimated end ~{est['estimated_end']} vs end time {ev['end_time']}: "
                   f"{'OK' if est['fits'] else 'DOES NOT FIT'}")
        if not est["fits"]:
            if est["longest_series"] > est["available"]:
                out.append(f"  A series alone needs {fmt_dur(est['longest_series'])} even with unlimited courts: "
                           "choose a shorter format.")
            else:
                out.append(f"  With these formats you would need about {est['courts_needed']} courts, or end "
                           f"around {est['estimated_end']}.")
            if est["impossible"]:
                out.append("  Even the shortest formats do not fit, so the preferred formats were kept.")
    out.append("Run scheduler.py on the output for the real timetable.")
    return "\n".join(out)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def config_to_toml(cfg: dict) -> str:
    """Write a config (as returned by normalize_config) back to TOML."""
    cfg = normalize_config(cfg)
    lines = ["# Event for tournament_setup.py (times HH:MM, durations in minutes)", "", "[event]"]
    for k, v in cfg["event"].items():
        if v is not None and v != "":
            lines.append(f"{k} = {_toml_value(v)}")
    lines += ["", "[setup]"]
    for k, v in cfg["setup"].items():
        if k != "weights":
            lines.append(f"{k} = {_toml_value(v)}")
    lines += ["", "[setup.weights]"]
    for k, v in cfg["setup"]["weights"].items():
        lines.append(f"{k} = {_toml_value(v)}")
    for s in cfg["series"]:
        lines += ["", "[[series]]"]
        for k in ("name", "teams", "match_duration", "pools", "qualifiers", "single_pool"):
            v = s.get(k)
            if v is not None and v != "":
                lines.append(f"{k} = {_toml_value(v)}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", type=Path, help="event description (.toml / .json / .yaml)")
    ap.add_argument("-o", "--output", type=Path, default=Path("tournament.json"))
    ap.add_argument("--pick", action="append", default=[], metavar="SERIES=N",
                    help="use proposal N (as numbered in the listing) for SERIES; repeatable")
    ap.add_argument("--top", type=int, default=5, help="proposals listed per series (default 5)")
    ap.add_argument("--no-fit", action="store_true", help="do not adapt formats to the end time")
    args = ap.parse_args(argv)

    picks = {}
    for p in args.pick:
        k, _, v = p.partition("=")
        picks[k.strip()] = int(v)
    try:
        res = run_setup(load_config(args.config), picks, args.no_fit)
    except SetupError as e:
        sys.exit(str(e))
    print(report_text(res, args.top))
    args.output.write_text(json.dumps(res["doc"], indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {args.output} ({len(res['doc']['matches'])} matches)")


if __name__ == "__main__":
    main()
