"""
Capacity tool.

  get_capacity(entity, as_of=None)
    - without as_of: current installed capacity from Master_Plants / Master_Stages / Master_Units
    - with as_of:    capacity on that date from Master_Part_Installed_Capacity
                     (parts commissioned on or before the date and not yet decommissioned)
"""
from datetime import date, datetime

from app.db.masters import get_masters
from app.tools.common import Entity, ToolError, ToolResult, fail, parse_date, round_value

FUELS = {"C": "Coal", "G": "Gas", "H": "Hydro", "S": "Solar", "W": "Wind", "PSP": "Pumped storage"}


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    return value


def get_capacity(entity, as_of=None) -> ToolResult:
    tool = "get_capacity"
    try:
        e = Entity.parse(entity)
        m = get_masters()
        notes: list[str] = []

        if as_of:
            d = parse_date(as_of)
            codes = set(e.plant_codes())
            parts = []
            for part in m.part_capacity:
                if part["PlantCode"] not in codes or part["CapacityStatus"] == "NON-COMMERCIAL":
                    continue
                comm, decomm = _as_date(part["PartCommDate"]), _as_date(part["DecommissionedDate"])
                if comm and comm <= d and (decomm is None or decomm > d):
                    parts.append(part)
            if e.level in ("stage", "unit"):
                notes.append("Capacity history is kept per plant; the plant total is shown.")
            total = sum(p["PartInstalledCapacityMW"] for p in parts)
            table = [{"part": p["PartName"], "plant": m.plant_name(p["PlantCode"]), "mw": round_value(p["PartInstalledCapacityMW"], "MW"),
                      "commissioned": _as_date(p["PartCommDate"]).isoformat()} for p in parts]
            notes.append("Non-commercial (trial) capacity is not included.")
            return ToolResult(tool, answer={"entity": e.label(), "capacity_mw": round_value(total, "MW"), "unit": "MW",
                                            "as_of": d.isoformat(), "parts": len(parts)},
                              table=table if e.level != "group" else None,
                              sources=["Master_Part_Installed_Capacity"], notes=notes)

        if e.level == "unit":
            u = next(u for u in m.units if u["PlantCode"] == e.plant_code and u["Unit"] == e.unit)
            return ToolResult(tool, answer={"entity": e.label(), "capacity_mw": round_value(u["CapacityMW"], "MW"), "unit": "MW",
                                            "commissioned": _as_date(u["CommissioningDate"]).isoformat() if u["CommissioningDate"] else None,
                                            "commercial": bool(u["CommercialStatus"])},
                              sources=["Master_Units"])
        if e.level == "stage":
            s = next((s for s in m.stages if s["PlantCode"] == e.plant_code and s["Stage"] == e.stage), None)
            if s is None:
                raise ToolError(f"{m.plant_name(e.plant_code)} has no {e.stage}.")
            return ToolResult(tool, answer={"entity": e.label(), "capacity_mw": round_value(s["CapacityMW"], "MW"),
                                            "unit": "MW", "units": s["Units"]}, sources=["Master_Stages"])
        if e.level == "plant":
            p = m.plants[e.plant_code]
            units = [u for u in m.units if u["PlantCode"] == e.plant_code and u["IsActive"] and not u["IsDeleted"]]
            table = [{"unit": m.unit_label(e.plant_code, u["Unit"]), "stage": u["Stage"], "mw": round_value(u["CapacityMW"], "MW")}
                     for u in sorted(units, key=lambda u: u["Unit"])]
            return ToolResult(tool, answer={"entity": e.label(), "capacity_mw": round_value(p["GrossCapMW"], "MW"), "unit": "MW",
                                            "units": len(units), "fuel": p["EnergyType"]},
                              table=table or None, sources=["Master_Plants", "Master_Units"])

        # group: total + breakdown by fuel
        by_fuel: dict[str, dict] = {}
        for code in e.plant_codes():
            p = m.plants[code]
            f = by_fuel.setdefault(FUELS.get(p["FuelType"], p["FuelType"]), {"fuel": FUELS.get(p["FuelType"], p["FuelType"]), "plants": 0, "mw": 0.0})
            f["plants"] += 1
            f["mw"] += p["GrossCapMW"] or 0
        table = sorted(({**f, "mw": round_value(f["mw"], "MW")} for f in by_fuel.values()), key=lambda f: -f["mw"])
        total = sum(f["mw"] for f in by_fuel.values())
        return ToolResult(tool, answer={"entity": e.label(), "capacity_mw": round_value(total, "MW"), "unit": "MW",
                                        "plants": sum(f["plants"] for f in by_fuel.values())},
                          table=table, sources=["Master_Plants"],
                          notes=["Current installed capacity of active plants (Master_Plants.GrossCapMW)."])
    except ToolError as exc:
        return fail(tool, str(exc))
    except StopIteration:
        return fail(tool, "Unit not found in the master data.")
