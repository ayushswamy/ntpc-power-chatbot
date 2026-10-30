"""
Master data: plants, stages, units, outage codes, capacity history.

These tables are small (< 500 rows each), so they are loaded once through the
safe runner and kept in memory for 6 hours. Tools use them to:
  - turn codes into names ("1004" -> "Singrauli"),
  - list the plants in a group ("NTPC total", "coal plants", "NR region"),
  - map the other plant codes used by some tables (SCADA code, short name).
"""
import threading
import time

import yaml

from app.config import PROJECT_ROOT, settings
from app.db.runner import get_runner

RELOAD_SECONDS = 6 * 3600


def pretty_name(name: str) -> str:
    """'DADRI-COAL' -> 'Dadri-Coal', 'SINGRAULI' -> 'Singrauli'; acronyms ('NSTPS', 'AASPL') and
    mixed-case names are kept as they are."""
    if not name.isupper():
        return name
    letters = "".join(ch for ch in name if ch.isalpha())
    looks_like_acronym = (letters.endswith(("PL", "CL"))                                   # ...Pvt Ltd / Co Ltd
                          or (len(letters) <= 5 and not any(v in letters[1:] for v in "AEIOU")))
    return name if looks_like_acronym else name.title()


class Masters:
    def __init__(self, plants: list[dict], stages: list[dict], units: list[dict],
                 outage_codes: list[dict], part_capacity: list[dict]):
        self.plants = {p["PlantCode"]: p for p in plants}
        self.stages = stages
        self.units = units
        self.part_capacity = part_capacity
        self.outage_codes = {(int(o["out_cd"]), int(o["out_subcd"])): o for o in outage_codes
                             if o["out_cd"] is not None and o["out_subcd"] is not None}
        self.outage_areas = {k[0]: (o["out_main_desc"] or "").strip() for k, o in self.outage_codes.items()}
        self.by_scada = {p["PlantCodeScada"]: p["PlantCode"] for p in plants if p.get("PlantCodeScada")}
        # Data_Trips.PLN_CD uses the short name (SSTPS), the OPPMS name (NPGC, Patratu) or a few special codes
        self.by_short_name = {}     # lower-case name -> PlantCode (for matching)
        self._original = {}         # lower-case name -> name as spelled in the data (for queries)
        aliases = yaml.safe_load(open(PROJECT_ROOT / "semantic" / "aliases.yaml", encoding="utf-8"))
        names = [(n, p["PlantCode"]) for p in plants for n in (p.get("StationOPPMS"), p.get("StationShortName"))]
        names += [(n, str(c)) for n, c in (aliases.get("trip_codes") or {}).items()]
        for name, code in names:                      # later entries win: short name over OPPMS, manual last
            if name and name.strip():
                self.by_short_name[name.strip().lower()] = code
                self._original[name.strip().lower()] = name.strip()

    def trip_codes(self, plant_code: str) -> list[str]:
        """All PLN_CD values that belong to a plant (for Data_Trips queries)."""
        return sorted({self._original[k] for k, code in self.by_short_name.items() if code == plant_code})

    # ---------------------------------------------------------------- names
    def plant_name(self, code: str) -> str:
        p = self.plants.get(str(code))
        return pretty_name(p["StationName"]) if p else f"plant {code}"

    def stage_name(self, code: str, stage: str) -> str:
        for s in self.stages:
            if s["PlantCode"] == str(code) and s["Stage"] == stage:
                return s["StageNameDescription"] or s["StageName"]
        return f"{self.plant_name(code)} {stage}"

    def unit_label(self, code: str, unit: str) -> str:
        number = unit.upper().replace("UNIT", "").lstrip("0") or unit
        return f"{self.plant_name(code)} Unit {number}"

    def stage_of_unit(self, code: str, unit: str) -> str | None:
        for u in self.units:
            if u["PlantCode"] == str(code) and u["Unit"] == unit:
                return u["Stage"]
        return None

    def resolve_code(self, code) -> str | None:
        """PlantCode, or a SCADA code used by some PI rows -> PlantCode."""
        code = str(code)
        if code in self.plants:
            return code
        return self.by_scada.get(code)

    # ---------------------------------------------------------------- groups
    def plants_in_scope(self, scope: dict | None = None) -> list[str]:
        """
        Active plant codes matching a scope, e.g.
          {}                                   -> NTPC total (NTPC_TOTAL_SCOPE: group or standalone)
          {"company": "standalone"}            -> NTPC-owned only
          {"company": "jv"}                    -> JVs / subsidiaries only
          {"fuel": ["C"]}                      -> coal plants (C, G, H, S, W, PSP)
          {"region": "NR"}                     -> RegionNTPC starting with NR
          {"pithead": "PH"}                    -> PH or NPH
        """
        scope = scope or {}
        company = scope.get("company", settings.ntpc_total_scope)
        result = []
        for code, p in self.plants.items():
            if not p.get("IsActive") or p.get("IsDeleted") or not (p.get("GrossCapMW") or 0) > 0:
                continue
            if company == "standalone" and p["CompanyGrp"] != "NTPC":
                continue
            if company == "jv" and p["CompanyGrp"] != "JV":
                continue
            if scope.get("fuel") and p["FuelType"] not in scope["fuel"]:
                continue
            if scope.get("region") and not (p["RegionNTPC"] or "").upper().startswith(scope["region"].upper()):
                continue
            if scope.get("pithead") and p["PHNPH"] != scope["pithead"]:
                continue
            result.append(code)
        return sorted(result)

    def scope_label(self, scope: dict | None = None) -> str:
        scope = scope or {}
        company = scope.get("company", settings.ntpc_total_scope)
        parts = [{"group": "NTPC Group (NTPC + JVs/subsidiaries)", "standalone": "NTPC standalone",
                  "jv": "NTPC JVs/subsidiaries"}.get(company, "NTPC")]
        fuels = {"C": "coal", "G": "gas", "H": "hydro", "S": "solar", "W": "wind", "PSP": "pumped storage"}
        if scope.get("fuel"):
            parts.append("/".join(fuels.get(f, f) for f in scope["fuel"]) + " plants")
        if scope.get("region"):
            parts.append(f"region {scope['region']}")
        if scope.get("pithead"):
            parts.append({"PH": "pithead", "NPH": "non-pithead"}.get(scope["pithead"], scope["pithead"]))
        return ", ".join(parts)


_masters: Masters | None = None
_loaded_at = 0.0
_lock = threading.Lock()


def load_masters_from_db() -> Masters:
    run = get_runner().run
    plants = run("SELECT TOP 1000 PlantCode, PlantCodeScada, StationName, StationShortName, StationOPPMS, GrossCapMW, "
                 "CommCapMW, Stages, Units, CompanyGrp, CompanySbGrp, CompanySbSbGrp, RegionNTPC, FuelType, EnergyType, "
                 "PHNPH, IsActive, IsDeleted FROM dbo.Master_Plants", purpose="masters: plants").rows
    stages = run("SELECT TOP 1000 PlantCode, Stage, StageName, StageNameRealView, StageNameDescription, Units, CapacityMW, "
                 "PCode, IsActive, IsDeleted FROM dbo.Master_Stages", purpose="masters: stages").rows
    units = run("SELECT TOP 2000 PlantCode, Stage, Unit, CapacityMW, CommissioningDate, DecommissioningDate, "
                "CommercialStatus, IsActive, IsDeleted FROM dbo.Master_Units", purpose="masters: units").rows
    codes = run("SELECT TOP 1000 out_cd, out_subcd, out_main_desc, out_sub_desc FROM dbo.Master_OutageCodes",
                purpose="masters: outage codes").rows
    parts = run("SELECT TOP 2000 PlantCode, PartSequence, PartName, PartInstalledCapacityMW, PartCommDate, "
                "DecommissionedDate, CapacityStatus, IsActive FROM dbo.Master_Part_Installed_Capacity",
                purpose="masters: capacity history").rows
    return Masters(plants, stages, units, codes, parts)


def get_masters() -> Masters:
    global _masters, _loaded_at
    with _lock:
        if _masters is None or time.monotonic() - _loaded_at > RELOAD_SECONDS:
            _masters = load_masters_from_db()
            _loaded_at = time.monotonic()
        return _masters


def reset_masters() -> None:
    """Tests: forget the loaded masters (e.g. after switching the runner)."""
    global _masters
    with _lock:
        _masters = None
