"""Components library ops in a real FreeCAD worker: measuring a part file, placing it, standard parts."""
import base64
import json
import math
import os
import shutil
import struct

import pytest

from cadswarm.config import find_freecad_python
from cadswarm.library import standard
from cadswarm.worker_client import WorkerClient

try:
    find_freecad_python()
except RuntimeError:
    pytest.skip("FreeCAD not installed", allow_module_level=True)

PLATE = "c_plate_abc123"


@pytest.fixture(scope="module")
def worker(tmp_path_factory):
    root = tmp_path_factory.mktemp("lib")
    w = WorkerClient(str(root / "model.FCStd"))
    w.start()
    # A 40x20x5 plate with two M3 normal-clearance holes (3.4 mm), exported as the library part's STEP.
    w.call("create_extrusion", {"name": "Src", "points": [[0, 0], [40, 0], [40, 20], [0, 20]], "height": 5,
                                "holes": [{"x": 8, "y": 10, "r": 1.7}, {"x": 32, "y": 10, "r": 1.7}]})
    step = root / "src.step"
    w.call("export", {"path": str(step), "names": ["Src"]})
    folder = root / "library" / PLATE
    folder.mkdir(parents=True)
    shutil.copy(step, folder / "part.step")
    w.call("delete", {"name": "Src"})
    w.root = root
    yield w
    w.stop()


def test_analyze_measures_a_step_file_and_finds_its_holes(worker):
    r = worker.call("analyze_component", {"id": PLATE})["result"]
    b = r["bbox"]
    assert [round(b[3] - b[0], 2), round(b[4] - b[1], 2), round(b[5] - b[2], 2)] == [40, 20, 5]
    assert r["volume"] == pytest.approx(40 * 20 * 5 - 2 * math.pi * 1.7 ** 2 * 5, rel=1e-3)
    assert r["features"]["holes"] == [{"d": 3.4, "count": 2}]
    stl = base64.b64decode(r["mesh"])
    (count,) = struct.unpack("<I", stl[80:84])
    assert count > 10 and len(stl) == 84 + 50 * count


def test_analyze_refuses_ids_that_could_escape_the_library(worker):
    with pytest.raises(Exception) as e:
        worker.call("analyze_component", {"id": "../../model"})
    assert "bad_args" in str(e.value) or "not a library part" in str(e.value)


def test_insert_places_the_part_and_tags_where_it_came_from(worker):
    r = worker.call("insert_component", {"component": PLATE, "name": "Plate1",
                                         "placement": {"base": [100, 0, 0]}}, allowed=[])
    assert r["created"] == ["Plate1"]
    obj = worker.call("get_object", {"name": "Plate1"})["result"]
    assert obj["placement"]["base"] == [100, 0, 0]
    assert obj["component"] == PLATE
    assert obj["bbox"] == [100, 0, 0, 140, 20, 5]


def test_every_standard_family_builds_a_valid_solid(worker):
    for i, (name, fam) in enumerate(standard.families().items()):
        size = next(iter(fam["sizes"]))
        args = {"family": name, "size": size, "name": f"Std{i}"}
        if "length" in fam["params"]:
            args["length"] = fam["lengths"][2]
        r = worker.call("insert_standard_part", args, allowed=[])
        assert r["created"] == [f"Std{i}"], name
        assert r["result"]["volume"] > 0, name
        assert not r["broken"], name


def test_a_608_bearing_has_its_catalog_size(worker):
    worker.call("insert_standard_part", {"family": "ball_bearing", "size": "608", "name": "B608"}, allowed=[])
    pr = worker.call("printability", {"names": ["B608"]})["result"]
    assert pr["objects"][0]["size"] == [22, 22, 7]


def test_standard_screw_hangs_its_shank_below_the_head(worker):
    worker.call("insert_standard_part", {"family": "iso4762", "size": "M3", "length": 12, "name": "Bolt"}, allowed=[])
    pr = worker.call("printability", {"names": ["Bolt"]})["result"]
    assert pr["objects"][0]["size"] == [5.5, 5.5, 15]


def test_unknown_standard_parts_are_refused(worker):
    for args in ({"family": "iso9999", "size": "M3", "name": "X1"},
                 {"family": "iso4762", "size": "M7", "length": 10, "name": "X2"},
                 {"family": "iso4762", "size": "M3", "name": "X3"}):
        with pytest.raises(Exception):
            worker.call("insert_standard_part", args, allowed=[])


def test_team_settings_drive_mesh_and_clash_tolerances(worker):
    coarse = worker.call("mesh", {"names": ["B608"], "_settings": [{"setting": "mesh_deviation", "num": 1.0}]})
    fine = worker.call("mesh", {"names": ["B608"], "_settings": [{"setting": "mesh_deviation", "num": 0.01}]})
    size = lambda r: len(base64.b64decode(r["result"]["meshes"]["B608"]))
    assert size(fine) > size(coarse)
    worker.call("insert_standard_part", {"family": "ball_bearing", "size": "608", "name": "B608b",
                                         "placement": {"base": [21.9, 0, 0]}}, allowed=[])
    small = worker.call("interference", {"names": ["B608", "B608b"]})["result"]["clashes"]
    ignored = worker.call("interference", {"names": ["B608", "B608b"],
                                           "_settings": [{"setting": "interference_min_vol", "num": 50}]})
    assert small and not ignored["result"]["clashes"]


def test_the_params_sheet_cannot_be_deleted(worker):
    """A helper deleted Params twice in one demo; every set_param after that failed until a restart."""
    with pytest.raises(Exception) as e:
        worker.call("delete", {"name": "Params"}, allowed=["Params"])
    assert "not_allowed" in str(e.value) or "can't be deleted" in str(e.value)
    worker.call("set_param", {"alias": "wall_t", "value": "2.4"}, allowed=["Params"])
