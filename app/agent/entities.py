"""
Entity resolver: turns what the user wrote ("singrouli", "Rihand unit 3", "coal plants")
into an Entity with real plant codes.

Matching order (decision 2, Solution_to_Query.xlsx):
  1. exact name: station name, short name, OPPMS name, stage names, aliases, or a name
     listed as ambiguous in semantic/aliases.yaml (e.g. "dadri" = coal / gas / solar)
  2. "contains" match: every word the user wrote appears in the plant name ("singrauli solar")
  3. fuzzy match (rapidfuzz) to catch spelling mistakes
When several plants fit:
  a. a fuel named in the question decides ("Dadri gas", "coal stock at Dadri")
  b. approved rule for EVERY plant: a plain name means the main conventional station
     (coal / gas / hydro) - e.g. "Singrauli" = Singrauli coal, not Singrauli Solar PV
     or Singrauli Small Hydro; those must be named explicitly. The answer says so.
  c. otherwise (two conventional plants, e.g. Dadri coal vs gas, or only renewable
     plants, e.g. Bhadla / Bhadla-II) the user is asked. The largest plant is never
     picked just because it is the largest.
"""
import re
from dataclasses import dataclass, field

import yaml
from rapidfuzz import fuzz, process

from app.config import PROJECT_ROOT
from app.db.masters import get_masters
from app.tools.common import Entity, ToolError

GENERIC_WORDS = {"plant", "station", "ntpc", "the", "of", "power", "project"}
FUEL_WORD = {"C": "coal", "G": "gas", "H": "hydro", "S": "solar", "W": "wind", "PSP": "pumped storage"}
FUEL_SCOPE = {"coal": ["C"], "gas": ["G"], "hydro": ["H"], "solar": ["S"], "wind": ["W"], "renewable": ["S", "W", "H", "PSP"]}
GROUP_SCOPE = {"ntpc": {}, "ntpc_standalone": {"company": "standalone"}, "jv": {"company": "jv"}}
CONVENTIONAL = {"Coal", "Gas", "Hydro"}      # Master_Plants.EnergyType; Solar, Wind, Small Hydro, PSP must be named
FUEL_IN_TEXT = {"coal": "C", "thermal": "C", "gas": "G", "solar": "S", "wind": "W", "hydro": "H"}
AUTO_PICK_SCORE, SUGGEST_SCORE = 88, 70


@dataclass
class Resolution:
    entity: Entity | None = None
    clarification: str | None = None          # question to ask the user
    options: list[str] = field(default_factory=list)   # plant codes offered in the clarification
    notes: list[str] = field(default_factory=list)


def normalize(text: str) -> str:
    text = re.sub(r"[-_.,/()]", " ", str(text).lower())
    words = [w for w in text.split() if w not in GENERIC_WORDS]
    return " ".join(words)


class NameIndex:
    """All names that point to a plant (or a plant stage)."""

    def __init__(self):
        m = get_masters()
        aliases = yaml.safe_load(open(PROJECT_ROOT / "semantic" / "aliases.yaml", encoding="utf-8"))
        self.active = set(m.plants_in_scope({"company": "all"}))
        self.names: dict[str, set[tuple[str, str | None]]] = {}

        def add(name, code, stage=None):
            n = normalize(name or "")
            if n and code in self.active:
                self.names.setdefault(n, set()).add((code, stage))

        for code, p in m.plants.items():
            for name in (p.get("StationName"), p.get("StationShortName"), p.get("StationOPPMS")):
                add(name, code)
        for code, names in (aliases.get("plants") or {}).items():
            for name in names:
                add(name, str(code))
        plant_names = {normalize(p.get("StationName") or "") for p in m.plants.values()}
        for s in m.stages:
            if not s.get("IsActive") or s.get("IsDeleted"):
                continue
            multi_stage = sum(1 for x in m.stages if x["PlantCode"] == s["PlantCode"] and x.get("IsActive")) > 1
            for name in (s.get("StageName"), s.get("StageNameRealView"), s.get("StageNameDescription")):
                n = normalize(name or "")
                if n and n not in plant_names:
                    add(name, s["PlantCode"], s["Stage"] if multi_stage else None)
        self.ambiguous = {normalize(k): [str(c) for c in v] for k, v in (aliases.get("ambiguous") or {}).items()}
        self.choices = list(self.names)


_index: NameIndex | None = None
_index_masters = None


def get_index() -> NameIndex:
    global _index, _index_masters
    m = get_masters()
    if _index is None or _index_masters is not m:   # rebuild when the masters are reloaded
        _index, _index_masters = NameIndex(), m
    return _index


def _describe(code: str) -> str:
    p = get_masters().plants[code]
    return f"{get_masters().plant_name(code)} ({FUEL_WORD.get(p['FuelType'], p['FuelType'])}, {round(p['GrossCapMW'] or 0):,} MW)"


def _choose(codes: list[str], said: str, fuel_hint: str | None = None) -> tuple[str | None, str | None, list[str], list[str]]:
    """Several plants fit the name: decide (rules a-c in the module docstring) or ask.
    Returns (code, question, options, notes)."""
    m = get_masters()
    codes = sorted(set(codes), key=lambda c: -(m.plants[c]["GrossCapMW"] or 0))   # order only for display
    if len(codes) == 1:
        return codes[0], None, [], []
    # a. the question names a fuel
    if fuel_hint:
        same = [c for c in codes if m.plants[c]["FuelType"] == fuel_hint]
        if len(same) == 1:
            return same[0], None, [], [f"Taken as {_describe(same[0])}, the {FUEL_WORD[fuel_hint]} plant."]
        codes = same or codes
    # b. a plain name means the main conventional station
    conventional = [c for c in codes if m.plants[c].get("EnergyType") in CONVENTIONAL]
    if len(conventional) == 1:
        others = " or ".join(_describe(c) for c in codes if c != conventional[0])
        return conventional[0], None, [], [f"Taken as {_describe(conventional[0])}; for {others}, name it with its type "
                                           f"(e.g. '{said.strip().title()} solar')."]
    # c. genuinely ambiguous
    options = ", ".join(_describe(c) for c in codes[:-1]) + f" or {_describe(codes[-1])}"
    return None, f"Which {said.strip().title()} do you mean: {options}?", codes, []


def _fuel_in_text(text: str) -> str | None:
    words = set(normalize(text).split())
    found = {code for word, code in FUEL_IN_TEXT.items() if word in words}
    return found.pop() if len(found) == 1 else None


def resolve(plant: str | None = None, unit=None, stage=None, group: str | None = None,
            fuel: str | None = None, region: str | None = None, fuel_hint: str | None = None) -> Resolution:
    """Resolve planner arguments to an Entity. With no plant and no group/fuel/region, entity is None.
    fuel_hint: 'C'/'G'/... when the question itself points to a fuel (e.g. a coal-stock question)."""
    said_ntpc = bool(plant) and "ntpc" in str(plant).lower() and normalize(plant) == ""
    if not plant or not normalize(plant):
        if not (group or fuel or region or said_ntpc):
            return Resolution()                       # nothing named: caller uses context or asks
        scope = dict(GROUP_SCOPE.get(group or "ntpc", {}))
        if fuel:
            scope["fuel"] = FUEL_SCOPE.get(fuel, [fuel])
        if region:
            scope["region"] = region
        return Resolution(entity=Entity(level="group", scope=scope))

    idx = get_index()
    said = str(plant)
    norm = normalize(said)
    hint = _fuel_in_text(said) or fuel_hint
    notes: list[str] = []
    code, stage_from_name = None, None
    words = set(norm.split())
    contains = {name: entries for name, entries in idx.names.items()
                if len(norm) >= 3 and words <= set(name.split())} if norm not in idx.names else {}

    if norm in idx.ambiguous:
        candidates = [c for c in idx.ambiguous[norm] if c in idx.active]
        code, question, options, notes = _choose(candidates, said, hint)
        if question:
            return Resolution(clarification=question, options=options)
    elif norm in idx.names or contains:
        matches = idx.names[norm] if norm in idx.names else set().union(*contains.values())
        codes = sorted({c for c, _ in matches})
        code, question, options, notes = _choose(codes, said, hint)
        if question:
            return Resolution(clarification=question, options=options)
        stages = {s for c, s in matches if c == code and s}
        stage_from_name = stages.pop() if len(stages) == 1 else None
    else:
        best: dict[str, tuple[float, str | None, str]] = {}
        for name, score, _ in process.extract(norm, idx.choices, scorer=fuzz.WRatio, limit=10):
            for c, s in idx.names[name]:
                if c not in best or score > best[c][0]:
                    best[c] = (score, s, name)
        ranked = sorted(best.items(), key=lambda kv: -kv[1][0])
        if not ranked or ranked[0][1][0] < SUGGEST_SCORE:
            return Resolution(clarification=f"I could not find a plant called '{said}'. Which plant do you mean?")
        top_score = ranked[0][1][0]
        close = [c for c, (sc, _, _) in ranked if sc >= top_score - 3]
        if top_score >= AUTO_PICK_SCORE:
            code, question, options, notes = _choose(close, said, hint)
            if question:
                return Resolution(clarification=question, options=options)
            stage_from_name = best[code][1]
            if normalize(get_masters().plant_name(code)) != norm and not notes:
                notes.append(f"'{said}' was matched to {get_masters().plant_name(code)}.")
        else:
            options = [c for c, _ in ranked[:3]]
            names = " or ".join(_describe(c) for c in options)
            return Resolution(clarification=f"Did you mean {names}?", options=options)

    try:
        entity = Entity(level="plant", plant_code=code,
                        stage=f"STAGE{int(stage)}" if stage else stage_from_name,
                        unit=str(unit) if unit else None).normalised()
    except (ToolError, ValueError) as exc:
        return Resolution(clarification=f"{exc} Which unit or stage do you mean?")
    if entity.level == "stage":
        m = get_masters()
        if not any(s["PlantCode"] == code and s["Stage"] == entity.stage for s in m.stages):
            stages = sorted(s["Stage"].replace("STAGE", "") for s in m.stages if s["PlantCode"] == code and s.get("IsActive"))
            return Resolution(clarification=f"{m.plant_name(code)} has stage(s) {', '.join(stages)}. Which one do you mean?")
    return Resolution(entity=entity, notes=notes)


def mask_plants(text: str) -> str:
    """Replace plant names with 'PLANT' so example search matches the KIND of question, not the plant."""
    idx = get_index()
    words = normalize(text).split()
    out, i = [], 0
    while i < len(words):
        for n in (3, 2, 1):
            phrase = " ".join(words[i:i + n])
            if len(words[i:i + n]) == n and len(phrase) > 2 and (phrase in idx.names or phrase in idx.ambiguous):
                out.append("PLANT")
                i += n
                break
        else:
            out.append(words[i])
            i += 1
    return " ".join(out)


def match_option(message: str, options: list[str]) -> str | None:
    """After a clarifying question: did the reply pick one of the offered plants? ('the coal one', 'gas', '2')"""
    m = get_masters()
    text = normalize(message)
    words = set(text.split())
    hits = []
    for i, code in enumerate(options, 1):
        p = m.plants[code]
        name_words = set(normalize(p.get("StationName") or "").split())
        fuel = FUEL_WORD.get(p["FuelType"], "")
        if str(i) in words or (fuel and fuel in words) or (name_words and name_words <= words):
            hits.append(code)
    return hits[0] if len(hits) == 1 else None
