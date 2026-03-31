import json
import re
import subprocess
import sys
from pathlib import Path

import minisat_wrapper
import numpy as np
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


def get_default_branch_literal(solver):
    if hasattr(solver, "pick_default_branch_literal"):
        return solver.pick_default_branch_literal()
    return solver.default_branching_literal()


def run_wrapper_with_default_branching_cnf(
    cnf,
    *,
    clause_learning: bool = True,
    dpll: bool = False,
):
    solver = minisat_wrapper.MiniSAT(
        cnf,
        clause_learning=clause_learning,
        dpll=dpll,
    )
    token_count = len(solver.step())
    while solver.state == minisat_wrapper.STATE_UNRESOLVED:
        literal = get_default_branch_literal(solver)
        assert literal in solver.candidates
        token_count += len(solver.step(literal))
    return solver.state, {
        "conflicts": solver.conflicts,
        "decisions": solver.decisions,
        "propagations": solver.propagations,
    }, token_count


def run_wrapper_with_default_branching(
    path: Path,
    *,
    clause_learning: bool = True,
    dpll: bool = False,
):
    return run_wrapper_with_default_branching_cnf(
        parse_dimacs(path),
        clause_learning=clause_learning,
        dpll=dpll,
    )


def run_wrapper_with_step_done_cnf(
    cnf,
    *,
    clause_learning: bool = True,
    dpll: bool = False,
):
    solver = minisat_wrapper.MiniSAT(
        cnf,
        clause_learning=clause_learning,
        dpll=dpll,
    )
    token_count = 0
    if solver.state == minisat_wrapper.STATE_UNRESOLVED:
        token_count = solver.step_done()
    return solver.state, {
        "conflicts": solver.conflicts,
        "decisions": solver.decisions,
        "propagations": solver.propagations,
    }, token_count


def run_wrapper_with_step_done(
    path: Path,
    *,
    clause_learning: bool = True,
    dpll: bool = False,
):
    return run_wrapper_with_step_done_cnf(
        parse_dimacs(path),
        clause_learning=clause_learning,
        dpll=dpll,
    )


def run_wrapper_with_default_branching_subprocess(
    path: Path,
    *,
    clause_learning: bool = True,
    dpll: bool = False,
):
    script = """import json
import sys
from pathlib import Path

import minisat_wrapper

path = Path(sys.argv[1])
clause_learning = sys.argv[2] == '1'
dpll = sys.argv[3] == '1'
cnf = []
with path.open() as fh:
    for line in fh:
        line = line.strip()
        if not line or line[0] in {'c', 'p'}:
            continue
        literals = [int(token) for token in line.split()]
        cnf.append([literal for literal in literals if literal != 0])

solver = minisat_wrapper.MiniSAT(
    cnf,
    clause_learning=clause_learning,
    dpll=dpll,
)
solver.step()
while solver.state == minisat_wrapper.STATE_UNRESOLVED:
    literal = solver.pick_default_branch_literal() if hasattr(solver, 'pick_default_branch_literal') else solver.default_branching_literal()
    if literal not in solver.candidates:
        raise RuntimeError(f'invalid literal {literal} for candidates {solver.candidates}')
    solver.step(literal)

print(json.dumps({
    'state': solver.state,
    'stats': {
        'conflicts': solver.conflicts,
        'decisions': solver.decisions,
        'propagations': solver.propagations,
    },
}))
"""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(path),
            "1" if clause_learning else "0",
            "1" if dpll else "0",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = None
    if proc.returncode == 0:
        payload = json.loads(proc.stdout)
    return proc, payload


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


def test_step_done_none_finishes_search_and_discards_initial_tokens():
    solver = minisat_wrapper.MiniSAT([[1, 2]])

    assert solver.step_done() == 4

    assert solver.state == minisat_wrapper.STATE_SAT
    assert solver.candidates == []
    assert solver.default_branching_literal() is None
    assert solver.decisions == 2

    with pytest.raises(RuntimeError):
        solver.step()


def test_step_done_rejects_literal_argument():
    solver = minisat_wrapper.MiniSAT([[1, 2]])

    with pytest.raises(TypeError):
        solver.step_done(1)


def test_step_done_none_uses_reserved_default_branch_choice():
    solver = minisat_wrapper.MiniSAT([[1, 2]])
    assert solver.step() == ["D"]

    literal = get_default_branch_literal(solver)
    assert literal in solver.candidates

    assert solver.step_done() == 3

    assert solver.state == minisat_wrapper.STATE_SAT
    assert solver.candidates == []
    assert solver.default_branching_literal() is None


def test_get_vcg_exports_candidate_variable_clause_graph():
    solver = minisat_wrapper.MiniSAT([[1, 2], [-1, 2]])

    assert solver.step() == ["D"]

    x, edge_index, edge_attr = solver.get_vcg()

    assert isinstance(x, np.ndarray)
    assert isinstance(edge_index, np.ndarray)
    assert isinstance(edge_attr, np.ndarray)

    assert x.dtype == np.float32
    assert edge_index.dtype == np.int64
    assert edge_attr.dtype == np.float32

    assert x.shape == (4, 2)
    assert edge_index.shape == (2, 4)
    assert edge_attr.shape == (4, 2)
    assert x.tolist() == [
        [1.0, 0.0],
        [1.0, 0.0],
        [0.0, 1.0],
        [0.0, 1.0],
    ]

    edges = sorted(
        (
            int(source),
            int(target),
            tuple(float(value) for value in attr),
        )
        for source, target, attr in zip(edge_index[0], edge_index[1], edge_attr)
    )
    assert edges == [
        (0, 2, (0.0, 1.0)),
        (0, 3, (1.0, 0.0)),
        (1, 2, (0.0, 1.0)),
        (1, 3, (0.0, 1.0)),
    ]


def test_get_vcg_does_not_consume_reserved_default_choice():
    solver = minisat_wrapper.MiniSAT([[1, 2]])

    assert solver.step() == ["D"]
    literal = get_default_branch_literal(solver)

    x, edge_index, edge_attr = solver.get_vcg()

    assert x.shape == (3, 2)
    assert edge_index.shape == (2, 2)
    assert edge_attr.shape == (2, 2)
    assert solver.step(literal) == [-1, 2, "SAT"]


def test_get_vcg_returns_empty_graph_after_sat():
    solver = minisat_wrapper.MiniSAT([[1, 2]])

    assert solver.step() == ["D"]
    assert solver.step(1) == [1, "D"]
    assert solver.step(2) == [2, "SAT"]

    x, edge_index, edge_attr = solver.get_vcg()

    assert x.dtype == np.float32
    assert edge_index.dtype == np.int64
    assert edge_attr.dtype == np.float32

    assert x.shape == (0, 2)
    assert edge_index.shape == (2, 0)
    assert edge_attr.shape == (0, 2)


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

    assert solver.step(1) == [1, 2, "[BT]", -1, 2, "SAT"]
    assert solver.state == minisat_wrapper.STATE_SAT
    assert solver.candidates == []
    assert solver.conflicts == 1
    assert solver.decisions == 2


def test_cdcl_with_learning_emits_backtrack_snapshot():
    solver = minisat_wrapper.MiniSAT(
        [[-1, 2], [-1, -2], [1, 2]],
        clause_learning=True,
        dpll=False,
    )

    assert solver.step() == ["D"]
    assert solver.step(1) == [1, 2, "[BT]", "L", -1, "0", -1, 2, "SAT"]
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
    assert solver.step(1) == [1, 2, "[BT]", "L", -1, "0", -1, 2, "SAT"]
    assert solver.state == minisat_wrapper.STATE_SAT
    assert solver.candidates == []
    assert solver.conflicts == 1
    assert solver.decisions == 2


@pytest.mark.parametrize(
    ("clause_learning", "dpll"),
    [
        (False, False),
        (False, True),
        (True, False),
        (True, True),
    ],
)
def test_step_done_token_count_matches_tokenized_path_on_backtracking_formula(
    clause_learning: bool,
    dpll: bool,
):
    cnf = [[1, 2], [1, -2], [-1, 2]]

    step_done_state, step_done_stats, step_done_token_count = run_wrapper_with_step_done_cnf(
        cnf,
        clause_learning=clause_learning,
        dpll=dpll,
    )
    tokenized_state, tokenized_stats, tokenized_token_count = run_wrapper_with_default_branching_cnf(
        cnf,
        clause_learning=clause_learning,
        dpll=dpll,
    )

    assert step_done_state == tokenized_state
    assert step_done_stats == tokenized_stats
    assert step_done_token_count == tokenized_token_count


def test_default_branching_matches_native_solver_on_sat_example():
    path = EXAMPLES_DIR / "v5c24_sat.cnf"
    native_status, native_stats = run_native_solver(path)
    wrapper_state, wrapper_stats, _ = run_wrapper_with_default_branching(path)

    assert native_status == 10
    assert wrapper_state == minisat_wrapper.STATE_SAT
    assert wrapper_stats == native_stats



def test_default_branching_matches_native_solver_on_unsat_example():
    path = EXAMPLES_DIR / "v5c24_unsat.cnf"
    native_status, native_stats = run_native_solver(path)
    wrapper_state, wrapper_stats, _ = run_wrapper_with_default_branching(path)

    assert native_status == 20
    assert wrapper_state == minisat_wrapper.STATE_UNSAT
    assert wrapper_stats == native_stats


@pytest.mark.parametrize(
    ("filename", "native_status", "wrapper_state"),
    [
        ("v5c24_sat.cnf", 10, minisat_wrapper.STATE_SAT),
        ("v5c24_unsat.cnf", 20, minisat_wrapper.STATE_UNSAT),
    ],
)
def test_step_done_matches_native_solver_on_examples(
    filename: str,
    native_status: int,
    wrapper_state: int,
):
    path = EXAMPLES_DIR / filename
    expected_native_status, native_stats = run_native_solver(path)
    observed_wrapper_state, wrapper_stats, token_count = run_wrapper_with_step_done(path)
    tokenized_wrapper_state, _, expected_token_count = run_wrapper_with_default_branching(path)

    assert expected_native_status == native_status
    assert observed_wrapper_state == wrapper_state
    assert tokenized_wrapper_state == wrapper_state
    assert wrapper_stats == native_stats
    assert token_count == expected_token_count


@pytest.mark.parametrize(
    ("filename", "wrapper_state", "clause_learning", "dpll"),
    [
        ("v5c24_sat.cnf", minisat_wrapper.STATE_SAT, False, False),
        ("v5c24_sat.cnf", minisat_wrapper.STATE_SAT, False, True),
        ("v5c24_sat.cnf", minisat_wrapper.STATE_SAT, True, False),
        ("v5c24_sat.cnf", minisat_wrapper.STATE_SAT, True, True),
        ("v5c24_unsat.cnf", minisat_wrapper.STATE_UNSAT, False, False),
        ("v5c24_unsat.cnf", minisat_wrapper.STATE_UNSAT, False, True),
        ("v5c24_unsat.cnf", minisat_wrapper.STATE_UNSAT, True, False),
        ("v5c24_unsat.cnf", minisat_wrapper.STATE_UNSAT, True, True),
    ],
)
def test_step_done_token_count_matches_tokenized_path_on_examples_across_options(
    filename: str,
    wrapper_state: int,
    clause_learning: bool,
    dpll: bool,
):
    path = EXAMPLES_DIR / filename
    step_done_state, step_done_stats, step_done_token_count = run_wrapper_with_step_done(
        path,
        clause_learning=clause_learning,
        dpll=dpll,
    )
    tokenized_state, tokenized_stats, tokenized_token_count = run_wrapper_with_default_branching(
        path,
        clause_learning=clause_learning,
        dpll=dpll,
    )

    assert step_done_state == wrapper_state
    assert tokenized_state == wrapper_state
    assert step_done_stats == tokenized_stats
    assert step_done_token_count == tokenized_token_count


@pytest.mark.parametrize(
    ("filename", "native_status", "wrapper_state"),
    [
        ("v5c24_sat.cnf", 10, minisat_wrapper.STATE_SAT),
        ("v5c24_unsat.cnf", 20, minisat_wrapper.STATE_UNSAT),
    ],
)
def test_clause_learning_false_matches_native_solver_on_examples(
    filename: str,
    native_status: int,
    wrapper_state: int,
):
    path = EXAMPLES_DIR / filename
    expected_native_status, native_stats = run_native_solver(path)
    proc, payload = run_wrapper_with_default_branching_subprocess(
        path,
        clause_learning=False,
    )

    assert proc.returncode == 0, (
        f"wrapper subprocess failed for {filename} with return code {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )
    assert expected_native_status == native_status
    assert payload["state"] == wrapper_state
    assert payload["stats"] == native_stats
