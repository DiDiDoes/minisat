import minisat_wrapper


def test_unsat_construction():
    solver = minisat_wrapper.MiniSAT([[1], [-1]])
    assert solver.state == minisat_wrapper.STATE_UNSAT
    assert solver.candidates == []


def test_sat_via_two_steps():
    solver = minisat_wrapper.MiniSAT([[1, 2]])
    assert solver.state == minisat_wrapper.STATE_UNRESOLVED
    assert solver.candidates == [1, -1, 2, -2]

    solver.step(1)
    assert solver.state == minisat_wrapper.STATE_UNRESOLVED
    assert solver.candidates == [2, -2]

    solver.step(2)
    assert solver.state == minisat_wrapper.STATE_SAT
    assert solver.candidates == []
    assert solver.decisions == 2
    assert solver.propagations >= 0
