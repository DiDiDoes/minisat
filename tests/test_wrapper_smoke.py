import re
import subprocess
from pathlib import Path

import minisat_wrapper
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "examples"
NATIVE_SOLVER = REPO_ROOT / "build/release/bin/minisat_core"
STAT_RE = re.compile(r"^(conflicts|decisions|propagations)\s*:\s*(\d+)", re.MULTILINE)


def parse_dimacs(path: Path):
    cnf = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line or line[0] in {"c", "p"}:
                continue
            literals = [int(token) for token in line.split()]
            clause = [literal for literal in literals if literal != 0]
            cnf.append(clause)
    return cnf


def ensure_native_solver():
    if NATIVE_SOLVER.exists():
        return
    subprocess.run(["make", "cr"], cwd=REPO_ROOT, check=True)


def run_native_solver(path: Path):
    ensure_native_solver()
    proc = subprocess.run(
        [str(NATIVE_SOLVER), str(path)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    stats = {name: int(value) for name, value in STAT_RE.findall(proc.stdout)}
    return proc.returncode, stats


def run_wrapper_with_default_branching(path: Path):
    solver = minisat_wrapper.MiniSAT(parse_dimacs(path))
    solver.step()
    while solver.state == minisat_wrapper.STATE_UNRESOLVED:
        literal = solver.default_branching_literal()
        assert literal in solver.candidates
        solver.step(literal)
    return solver.state, {
        "conflicts": solver.conflicts,
        "decisions": solver.decisions,
        "propagations": solver.propagations,
    }


def test_first_call_must_consume_initial_trace():
    solver = minisat_wrapper.MiniSAT([[1, 2]])

    with pytest.raises(ValueError):
        solver.step(1)


def test_unsat_construction():
    solver = minisat_wrapper.MiniSAT([[1], [-1]])
    assert solver.state == minisat_wrapper.STATE_UNSAT
    assert solver.candidates == []
    assert solver.default_branching_literal() is None
    assert solver.step() == ["UNSAT"]

    with pytest.raises(RuntimeError):
        solver.step()


def test_sat_via_two_steps():
    solver = minisat_wrapper.MiniSAT([[1, 2]])
    assert solver.step() == ["D"]
    assert solver.state == minisat_wrapper.STATE_UNRESOLVED
    assert solver.candidates == [1, -1, 2, -2]

    assert solver.step(1) == [1, "D"]
    assert solver.state == minisat_wrapper.STATE_UNRESOLVED
    assert solver.candidates == [2, -2]

    assert solver.step(2) == [2, "SAT"]
    assert solver.state == minisat_wrapper.STATE_SAT
    assert solver.candidates == []
    assert solver.default_branching_literal() is None
    assert solver.decisions == 3
    assert solver.propagations >= 0


def test_default_branching_literal_replays_minisat_choice():
    solver = minisat_wrapper.MiniSAT([[1, 2]])
    assert solver.step() == ["D"]
    literal = solver.default_branching_literal()

    assert literal == -1
    assert literal in solver.candidates

    assert solver.step(literal) == [-1, 2, "SAT"]
    assert solver.state == minisat_wrapper.STATE_SAT
    assert solver.candidates == []
    assert solver.default_branching_literal() is None
    assert solver.decisions == 2


def test_clause_learning_false_still_solves_conflict_formula():
    solver = minisat_wrapper.MiniSAT(
        [[-1, 2], [-1, -2], [1, 2]],
        clause_learning=False,
    )

    assert solver.step() == ["D"]
    assert solver.state == minisat_wrapper.STATE_UNRESOLVED
    assert solver.candidates == [1, -1, 2, -2]

    assert solver.step(1) == [1, 2, -1, 2, "SAT"]
    assert solver.state == minisat_wrapper.STATE_SAT
    assert solver.candidates == []
    assert solver.conflicts == 1
    assert solver.decisions == 2


def test_dpll_retries_complement_without_counting_decision():
    solver = minisat_wrapper.MiniSAT(
        [[-1, 2], [-1, -2], [1, 2]],
        clause_learning=False,
        dpll=True,
    )

    assert solver.step() == ["D"]
    assert solver.step(1) == [1, 2, "[BT]", -1, 2, "SAT"]
    assert solver.state == minisat_wrapper.STATE_SAT
    assert solver.candidates == []
    assert solver.conflicts == 1
    assert solver.decisions == 2


def test_dpll_can_still_learn_clauses():
    solver = minisat_wrapper.MiniSAT(
        [[-1, 2], [-1, -2], [1, 2]],
        dpll=True,
    )

    assert solver.step() == ["D"]
    assert solver.step(1) == [1, 2, "[BT]", -1, 2, "SAT"]
    assert solver.state == minisat_wrapper.STATE_SAT
    assert solver.candidates == []
    assert solver.conflicts == 1
    assert solver.decisions == 2


def test_default_branching_matches_native_solver_on_sat_example():
    path = EXAMPLES_DIR / "v5c24_sat.cnf"
    native_status, native_stats = run_native_solver(path)
    wrapper_state, wrapper_stats = run_wrapper_with_default_branching(path)

    assert native_status == 10
    assert wrapper_state == minisat_wrapper.STATE_SAT
    assert wrapper_stats == native_stats



def test_default_branching_matches_native_solver_on_unsat_example():
    path = EXAMPLES_DIR / "v5c24_unsat.cnf"
    native_status, native_stats = run_native_solver(path)
    wrapper_state, wrapper_stats = run_wrapper_with_default_branching(path)

    assert native_status == 20
    assert wrapper_state == minisat_wrapper.STATE_UNSAT
    assert wrapper_stats == native_stats
