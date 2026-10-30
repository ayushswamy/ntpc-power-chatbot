"""Entity resolver tests - master data comes from the recorded fixture (no database)."""
from pathlib import Path

import pytest

from app import clock
from app.db.masters import reset_masters
from app.db.runner import ReplayRunner, set_runner

FIXTURE = Path(__file__).parent / "fixtures" / "tools_fixture.json"
pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason="fixtures not recorded yet")


@pytest.fixture(scope="module", autouse=True)
def replay():
    runner = ReplayRunner(FIXTURE)
    set_runner(runner)
    clock.set_fixed_now(runner.now)
    reset_masters()
    yield
    clock.set_fixed_now(None)
    set_runner(None)
    reset_masters()


def res(**kw):
    from app.agent.entities import resolve
    return resolve(**kw)


def test_plain_name_means_main_conventional_station():
    r = res(plant="Singrauli")
    assert r.entity.plant_code == "1004" and r.entity.level == "plant"
    assert r.notes and "Singrauli" in r.notes[0]          # says coal was chosen over solar/small hydro
    assert res(plant="Kawas").entity.plant_code == "1020"          # gas, not Kawas solar
    assert res(plant="Gandhar").entity.plant_code == "1021"        # Jhanor-Gandhar gas, not Gandhar solar
    assert res(plant="Talcher").entity.plant_code == "1026"        # Talcher Kaniha coal (Talcher Thermal inactive)


def test_explicit_renewable_names():
    assert res(plant="Singrauli Solar").entity.plant_code == "2160"
    assert res(plant="Singrauli Small Hydro").entity.plant_code == "2230"


def test_fuel_decides():
    assert res(plant="Dadri", fuel_hint="C").entity.plant_code == "1011"   # "coal stock at Dadri"
    assert res(plant="Dadri gas").entity.plant_code == "1014"


def test_genuinely_ambiguous_asks():
    for name in ("Nabinagar", "Bhadla", "Bhilai"):          # two coal plants / only solar plants
        r = res(plant=name)
        assert r.entity is None and r.clarification, name


def test_typo():
    assert res(plant="singrouli").entity.plant_code == "1004"
    assert res(plant="vindhyanchal").entity.plant_code == "1018"


def test_alias_and_short_name():
    assert res(plant="Kanti").entity.plant_code == "1087"
    assert res(plant="SSTPS").entity.plant_code == "1004"
    assert res(plant="Talcher Kaniha").entity.plant_code == "1026"


def test_ambiguous_asks():
    r = res(plant="Dadri")
    assert r.entity is None and r.clarification and set(r.options) == {"1011", "1014", "2060"}


def test_unit_and_stage():
    u = res(plant="Rihand", unit=3).entity
    assert (u.level, u.unit, u.stage) == ("unit", "UNIT03", "STAGE2")
    s = res(plant="Rihand", stage=2).entity
    assert (s.level, s.stage) == ("stage", "STAGE2")
    assert res(plant="Barh", stage=5).clarification


def test_groups():
    g = res(group="ntpc", fuel="coal").entity
    assert g.level == "group" and g.scope == {"fuel": ["C"]}
    assert res(plant="NTPC").entity.level == "group"
    assert res().entity is None


def test_not_found():
    r = res(plant="Xyzabcpur")
    assert r.entity is None and "could not find" in r.clarification


def test_match_option():
    from app.agent.entities import match_option
    assert match_option("the coal one", ["1011", "1014", "2060"]) == "1011"
    assert match_option("gas", ["1011", "1014", "2060"]) == "1014"
    assert match_option("2", ["1011", "1014", "2060"]) == "1014"
    assert match_option("no idea", ["1011", "1014", "2060"]) is None
