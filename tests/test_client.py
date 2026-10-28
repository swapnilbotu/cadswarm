"""The small stdlib helpers every part of CADSwarm writes state with."""
import json
import threading

from cadswarm.client import save_json


def test_two_writers_of_one_file_never_leave_half_a_file(tmp_path):
    """A runner restart races the runner it replaces (and the panel starts helpers too): both rewrite
    runner.json. A shared `<path>.tmp` made them write each other's temp file and die on a Windows
    PermissionError, which is how a runner used to vanish mid-run."""
    path = str(tmp_path / "runner.json")
    problems = []

    def write(who):
        for i in range(60):
            try:
                save_json(path, {"writer": who, "i": i, "pad": "x" * 4000})
            except OSError as e:  # the old code raised here
                problems.append(e)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not problems, problems
    data = json.load(open(path, encoding="utf-8"))
    assert set(data) == {"writer", "i", "pad"} and len(data["pad"]) == 4000
    assert not list(tmp_path.glob("*.tmp")), "a temp file was left behind"
