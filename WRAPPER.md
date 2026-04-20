# MiniSAT Python Wrapper Design

## Goal

Expose MiniSAT as a step-wise Python object that accepts a CNF formula directly from Python data structures, allows an external controller to drive branching decisions one step at a time, and can emit the same trajectory-token stream that `src.dpll.DPLL.step()` returns.

The wrapper presents the solver as:

```python
solver = MiniSAT(cnf, clause_learning=True, dpll=False)
```

where `cnf` is a `list[list[int]]`. Each inner list is a clause, and each integer is a DIMACS-style literal:

- `1` means variable `x1`
- `-1` means literal `not x1`
- `2` means variable `x2`
- `0` is not allowed inside clauses

Example:

```python
cnf = [
    [1, -2],
    [2, 3],
    [-1, -3],
]
solver = MiniSAT(cnf, clause_learning=True, dpll=False)
```

## Public API

The Python wrapper exposes a single stateful class:

```python
class MiniSAT:
    def __init__(
        self,
        cnf: list[list[int]],
        clause_learning: bool = True,
        dpll: bool = False,
    ) -> None: ...

    @property
    def state(self) -> int: ...

    @property
    def candidates(self) -> list[int]: ...

    @property
    def conflicts(self) -> int: ...

    @property
    def decisions(self) -> int: ...

    @property
    def propagations(self) -> int: ...

    def step(self, literal: int | None = None) -> list[int | str]: ...
    def step_done(self) -> int: ...

    def get_vcg(self) -> tuple["np.ndarray", "np.ndarray", "np.ndarray"]: ...
    def get_sequence(self) -> list[int | str]: ...
```

## Search Options

The wrapper should expose two constructor-only search options:

- `clause_learning`: defaults to `True`
- `dpll`: defaults to `False`

These options affect only the internal conflict-handling behavior after the CNF has been loaded. They do not change the CNF input format, the public state codes, or the meaning of `candidates`.

### `clause_learning`

`clause_learning` controls whether conflicts produce new learnt clauses in the internal MiniSAT database.

- when `True`, the wrapper keeps the current behavior of deriving a conflict clause and adding it to `learnts`
- when `False`, the wrapper must not allocate, attach, or enqueue via a newly learnt clause

This option does not disable propagation, simplification, or the existing original clauses.

### `dpll`

`dpll` controls backtracking strategy after a conflict.

- when `False`, the wrapper keeps its current CDCL-style backjumping behavior
- when `True`, the wrapper must backtrack chronologically and try the complement of the most recent decision literal before returning control to Python

With `dpll=True`, one call to `step(literal)` may internally consume both polarities of the same decision variable before the wrapper pauses again.

### Interaction of the Options

The two options should be independent:

- `clause_learning=False, dpll=False`: no learning, but still use MiniSAT-style conflict analysis and backjumping
- `clause_learning=True, dpll=False`: current wrapper behavior
- `clause_learning=False, dpll=True`: closest to textbook DPLL
- `clause_learning=True, dpll=True`: chronological backtracking with learnt-clause retention

The last combination is intentionally not pure DPLL. It is still useful as an explicit mode because the requested behavior for `dpll` is about chronological undo and complement retry, not about forcing learning off.

## Construction

`MiniSAT(cnf, clause_learning=True, dpll=False)` performs these actions:

1. Create an internal MiniSAT `Solver`.
2. Scan the CNF and allocate enough solver variables to cover the largest absolute literal.
3. Add every clause to the solver.
4. Store the chosen values of `clause_learning` and `dpll` in the wrapper instance.
5. Run unit propagation until the solver reaches a stable branching point or a terminal result.
6. Initialize all public properties.

Construction is therefore not a passive load step. When the object is returned:

- `state == 20` if the formula is already inconsistent
- `state == 10` if all variables are fixed without any further branching
- `state == 0` if the solver is waiting for an external branching decision

The two option values are fixed for the lifetime of the solver instance.

## State Model

`state` uses integer codes:

- `0`: unresolved, and the solver is paused at a branching point
- `10`: satisfiable
- `20`: unsatisfiable

The wrapper only exposes observable states after propagation has quiesced. In other words, Python never sees an intermediate state where propagation is still in progress.

### Meaning of `state == 0`

When `state` is `0`:

- all forced implications from the current trail have already been propagated
- no conflict is currently pending
- the solver is not finished
- the next action must be an external branching choice from `candidates`

## Branching Candidates

`candidates` is a `list[int]` describing every valid branching literal at the current pause point.

Design rules:

- `candidates` is meaningful only when `state == 0`
- if `state` is `10` or `20`, `candidates` is `[]`
- each entry is a literal encoded with the same signed-int convention as the input CNF
- every entry is currently unassigned and can be legally chosen as the next decision literal

The intended interpretation is:

- a positive value `k` means branch on `xk = True`
- a negative value `-k` means branch on `xk = False`

The wrapper should define `candidates` from the solver's current decision frontier, not from all literals in the original problem. In practice, this means the list should contain the literals that are actually admissible as the next branch after all current implications have been applied.

For determinism, the wrapper should keep a stable ordering. A reasonable choice is:

1. group by variable index
2. emit positive then negative literal for each currently branchable variable

Example:

```python
solver.candidates == [1, -1, 4, -4, 7, -7]
```

This means the solver is currently willing to branch on variables `1`, `4`, or `7`, with either polarity.

## Statistics

The wrapper exposes three read-only counters:

- `conflicts`
- `decisions`
- `propagations`

These should mirror MiniSAT's internal counters and therefore be monotonic over the lifetime of the object.

Expected meanings:

- `conflicts`: number of conflicts encountered during search
- `decisions`: number of explicit branching decisions taken
- `propagations`: number of implied assignments performed by Boolean constraint propagation

These counters should be updated:

- after construction
- after every call to `step`
- after every call to `step_done`

When `dpll=True`, the wrapper's automatic complement retries should not increment `decisions`. To stay consistent with MiniSat, they should be treated like implied assignments performed during internal conflict recovery rather than as external branching decisions.

## `default_branching_literal()`

`default_branching_literal()` returns the literal that MiniSAT itself would branch on next at the current pause point.

Design rules:

- if `state == 0`, the return value is either one of the literals in `candidates` or `None` if no branch is available
- if `state` is `10` or `20`, the return value is `None`
- calling `default_branching_literal()` must not mutate solver state, advance the search, consume heap entries, or change the random seed

This method exists so a Python controller can exactly replay MiniSAT's own default branching policy by repeatedly calling:

```python
literal = solver.default_branching_literal()
solver.step(literal)
```

When `dpll=True`, this method still reports the next branching literal at a stable external pause point. It does not expose the internal complement retry that may happen during conflict recovery.

## `step(literal)`

`step(literal: int)` applies one external branching choice and then lets MiniSAT run until the next externally visible pause point.

### Precondition

`step` may only be called when `state == 0`.

The input `literal` must be one of the values currently present in `candidates`. If not, the wrapper should raise `ValueError`.

### Execution

`step(literal)` should perform the following sequence:

1. Validate that the solver is unresolved and paused at a branch point.
2. Validate that `literal` is an allowed branching candidate.
3. Push `literal` as the next decision on the MiniSAT trail.
4. Resume the internal search loop.
5. Continue through propagation and conflict handling as needed.
6. Stop only when one of the following becomes true:
   - the solver proves SAT
   - the solver proves UNSAT
   - propagation is complete and a new external branching choice is required
7. Refresh `state`, `candidates`, `conflicts`, `decisions`, and `propagations`.

Conflict handling depends on the constructor options:

- if `dpll == False`, use the current wrapper strategy
- if `clause_learning == True`, conflicts may add learnt clauses
- if `clause_learning == False`, conflicts must not add learnt clauses
- if `dpll == True`, a conflict must not immediately return to Python after undoing one level; the wrapper must first try the complement of the most recent unresolved decision literal and continue propagation

### Postcondition

After `step`, exactly one of these is true:

- `state == 10` and `candidates == []`
- `state == 20` and `candidates == []`
- `state == 0` and `candidates` contains the next legal branching literals

### Important semantic point

One call to `step` does not mean "perform one internal MiniSAT iteration". It means "commit one external decision, then run MiniSAT until the next decision point or terminal result".

This keeps the Python API aligned with CDCL behavior:

- the caller chooses decision literals
- MiniSAT still owns propagation, learning, backjumping, and termination

## `step_done()`

`step_done() -> int` runs the solver all the way to a terminal result using MiniSAT's own default branching heuristic for every remaining decision.

This method exists for the "finish the solve, get the final counters, and know how long the remaining trajectory would have been" use case. It does not return any tokens, and it must not store the full token stream in memory. Instead, it returns the number of tokens that would have been emitted by the tokenized interface from the current state through termination.

### Precondition

`step_done` may only be called when `state == 0`.

The method takes no literal argument. It should simply resume from the current settled pause point and let MiniSAT choose all subsequent branch literals internally.

### Execution

`step_done()` should perform the following sequence:

1. Validate that the solver is unresolved and paused at a branch point.
2. Account for any tokenized prefix that is still pending from construction-time root propagation.
3. Resume the internal search loop.
4. Whenever a branching choice is needed during the same call, use MiniSAT's normal default branching heuristic instead of returning control to Python.
5. Continue until the solver proves SAT or UNSAT.
6. Refresh `state`, `candidates`, `conflicts`, `decisions`, and `propagations`.
7. Return the total number of tokens that would have been emitted from this point through termination, without storing those tokens.

Conflict handling should still respect the constructor options:

- `clause_learning` still controls whether learnt clauses are retained
- `dpll` still controls whether conflicts backjump non-chronologically or recover chronologically

`step_done` changes only who chooses future branch literals during that call. It does not change the configured conflict policy.

The returned token count should include every token that a fully tokenized run would have emitted from the current state:

- any still-pending constructor-time initial trace
- default-heuristic branching literal tokens
- propagated literal tokens
- backtrack markers and replay tokens
- learnt-clause tokens when `clause_learning=True`
- the final control token such as `"SAT"` or `"UNSAT"`

### Postcondition

After `step_done`, exactly one of these is true:

- `state == 10` and `candidates == []`
- `state == 20` and `candidates == []`

The method returns an integer token count and does not expose any intermediate pause points. Because the solver is terminal after this call, `candidates` should be empty.

### Important semantic point

`step_done()` from the initial constructor-created pause point should be observationally equivalent to just letting the wrapper's configured solver run to completion on its own. With the default options `clause_learning=True, dpll=False`, this should match ordinary MiniSAT solving behavior.

The returned count is about the remaining tokenized trajectory, not about what is materialized in memory. `step_done()` should therefore count tokens but avoid storing the full token list.

When `dpll=True`, the ownership split changes slightly: MiniSAT still owns propagation and termination, but the wrapper replaces MiniSAT's non-chronological backjumping with an explicit chronological branch-flip policy.

## Error Handling

The wrapper should reject malformed CNF input during construction.

Recommended validation:

- `cnf` must be a list of lists
- every literal must be a non-zero integer
- empty clauses are allowed and should immediately make the solver UNSAT
- `clause_learning` and `dpll` should be accepted as booleans

Recommended stepping errors:

- raise `RuntimeError` if `step` is called when `state != 0`
- raise `RuntimeError` if `step_done` is called when `state != 0`
- raise `ValueError` if a literal requested by `step` is not in `candidates`

## Implementation Notes

The main design requirement is that the wrapper exposes a paused-search interface, while MiniSAT is naturally written around an internal CDCL loop. The C++ binding layer therefore needs an adapter that can:

- stop after propagation reaches a decision point
- accept an externally supplied decision literal
- resume the normal MiniSAT search procedure
- optionally suppress learnt-clause insertion
- optionally replace non-chronological backjumping with chronological branch flipping

Conceptually, the adapter behaves like a restricted version of `search()` where branch selection is delegated to Python instead of always calling MiniSAT's internal `pickBranchLit()`.

### Current Control Flow

The current wrapper already differs from upstream MiniSAT in one important way: it does not use the outer restart loop from `solve_()`. Instead, `step()` pushes one external decision and `settle()` runs propagation and conflict handling until a new stable pause point is reached.

This is the correct place to add `clause_learning` and `dpll`, because both requested options only affect the inner conflict path.

### Planned Wrapper State

To support the new options cleanly, the wrapper should add:

- a stored `clause_learning_` boolean
- a stored `dpll_` boolean
- a wrapper-side decision stack that records each explicit branch literal and whether its complement has already been tried

The decision stack is needed only because `dpll=True` cannot be implemented as a naive "always negate the current decision literal" rule. Without extra bookkeeping, the solver would oscillate forever between `x` and `not x` on repeated conflicts at the same level.

A reasonable internal structure is:

```cpp
struct DecisionFrame {
    Minisat::Lit literal;
    bool tried_complement;
};
```

and then:

```cpp
std::vector<DecisionFrame> decision_frames_;
```

### Planned `step()` Changes

Before enqueuing the user-supplied decision, `step()` should push a new decision frame whose `literal` is the chosen branch and whose `tried_complement` flag is `false`.

Decision counting should remain consistent with MiniSat:

- increment `decisions` for the external branch chosen by Python
- when `dpll=True`, do not increment `decisions` for the wrapper's automatic complement retry
- the complement retry should instead be documented and implemented as an implied assignment during internal conflict recovery

### Planned Conflict Path

The implementation should split the current conflict block into two modes.

#### Mode 1: `dpll == False`

This remains structurally close to the current code:

- run MiniSAT conflict analysis
- if `clause_learning == True`, attach the learnt clause as today
- if `clause_learning == False`, skip allocating and attaching the learnt clause
- preserve the current non-chronological backtrack target computed by analysis

In this mode, `clause_learning=False` changes only clause insertion. It does not change the backjump target or the fact that conflict analysis is still used to decide how far to backtrack.

#### Mode 2: `dpll == True`

This mode replaces MiniSAT's backjumping with wrapper-managed chronological recovery:

1. On conflict, if `decisionLevel() == 0`, report UNSAT.
2. Look at the most recent decision frame.
3. If that frame has not yet tried its complement:
   - save the original decision literal
   - backtrack one decision level
   - mark the frame as having tried its complement
   - create a fresh decision level
   - enqueue the complement literal
   - continue propagation without returning to Python
4. If that frame has already tried its complement:
   - backtrack one decision level
   - pop the exhausted frame
   - continue the same process with the next older decision frame
5. If no decision frames remain, report UNSAT.

This behavior matches the requested policy "undo the last decision and try the complement of the last decision literal" without creating an infinite flip loop.

### Interaction Between `dpll` and `clause_learning`

When `dpll=True`, clause learning should still be controlled independently:

- if `clause_learning=False`, the conflict path can skip `analyze(...)` entirely and just perform chronological recovery
- if `clause_learning=True`, the wrapper may still call `analyze(...)` to derive a learnt clause, but it must ignore MiniSAT's computed backjump level and must not use the asserting-literal enqueue as the next branch choice

In other words, under `dpll=True`, learnt clauses may be retained, but branch recovery is still governed by the wrapper's chronological policy.

### Candidate Refresh Semantics

`candidates` should remain an external branching frontier only. In particular:

- when `dpll=False`, behavior stays unchanged
- when `dpll=True`, the wrapper must not pause and expose `candidates` in between "conflict" and "retry the complement of the last decision"
- `candidates` should only be recomputed after all mandatory internal propagation and any automatic DPLL branch flips have settled

### Database Reduction

`reduceDB()` should only matter when learnt clauses exist.

- if `clause_learning=True`, keep the current learnt-database maintenance behavior
- if `clause_learning=False`, `learnts` should remain empty, so reduction should become a no-op in practice

The wrapper should keep MiniSAT as the source of truth for:

- clause storage
- assignment trail
- unit propagation
- conflict analysis
- learnt clauses
- backtracking level
- statistics

Python should only observe:

- current terminal status
- current legal branch choices
- aggregate search counters

The only internal state that Python does not observe directly but that the wrapper now needs to maintain is the decision-frame stack used for `dpll=True`.

## `get_vcg()`

`get_vcg()` returns a read-only snapshot of the current variable-clause graph for the internal solver state:

```python
x, edge_index, edge_attr = solver.get_vcg()
```

It must not mutate solver state, trigger propagation, or consume any pending default branch choice. It should reflect the same quiescent solver snapshot that Python already sees through `state` and `candidates`.

### Return Values

`get_vcg()` returns:

- `x: [N, 2]`
- `edge_index: [2, E]`
- `edge_attr: [E, 2]`

Recommended dtypes:

- `x`: `float32`
- `edge_index`: `int64`
- `edge_attr`: `float32`

### Node Set

The graph is bipartite and contains variable nodes followed by clause nodes. `x[i]` is a one-hot node-type feature:

- `[1, 0]` means variable node
- `[0, 1]` means clause node

#### Variable Nodes

The first `V` nodes represent candidate variables only. Use one node per candidate variable, not one node per candidate literal. Derive this prefix from the current `candidates` frontier, deduplicated by absolute variable index while preserving first-appearance order. With the current wrapper behavior, this yields ascending variable order.

Example:

```python
solver.candidates == [1, -1, 4, -4, 7, -7]
```

then the variable-node prefix is `x1`, `x4`, `x7` in that order. Non-candidate variables must not appear as nodes even if they still occur in clauses.

#### Clause Nodes

Clause nodes follow the variable-node prefix. Include every clause that is not currently satisfied:

- original clauses from `clauses`
- learned clauses from `learnts`

For deterministic indexing, iterate original clauses first and learned clauses second, preserving current internal storage order within each group.

Filtering rules:

- skip removed clauses
- skip satisfied clauses
- include unresolved clauses
- include falsified clauses if they are present in a visible UNSAT snapshot

### Edge Set

Each edge represents literal inclusion between a candidate-variable node and an included clause node. Use one directed incidence edge per retained literal occurrence:

- source: variable node
- target: clause node

Do not duplicate reverse edges in the first version. If a downstream consumer wants an undirected graph, it can symmetrize the result outside the wrapper.

`edge_attr[e]` is the literal-sign one-hot feature:

- `[0, 1]` means positive literal
- `[1, 0]` means negative literal

Important consequence: if an included clause contains only non-candidate variables, the clause node is still present but contributes no edges. This is intentional because the requested graph excludes non-candidate variable nodes.

### Terminal-State Semantics

`get_vcg()` should be callable in all visible wrapper states.

- when `state == 0`, variable nodes come from the current candidate frontier and clause nodes come from all currently unsatisfied original and learned clauses
- when `state == 10`, there are no candidate variables and all clauses should be satisfied, so the expected result is an empty graph with shapes `(0, 2)`, `(2, 0)`, and `(0, 2)`
- when `state == 20`, the variable-node prefix is empty and clause nodes represent all clauses still unsatisfied in the terminal snapshot

## `get_sequence()`

`get_sequence()` returns a read-only snapshot of the same solver state as `get_vcg()`, but as a flat token sequence instead of a graph structure:

```python
tokens = solver.get_sequence()
```

It must not mutate solver state, trigger propagation, or consume any pending default branch choice. It reflects the same quiescent solver snapshot as `get_vcg()`.

### Return Value

`get_sequence()` returns a `list[int | str]`. The sequence has the following structure:

```
["[BOS]", <clause_1_literals...>, "0", <clause_2_literals...>, "0", ..., "[SEP]"]
```

- `"[BOS]"` opens the sequence
- each clause is represented as its literals in order, followed by the string `"0"` as a clause terminator
- `"[SEP]"` closes the sequence

Literal encoding uses the same signed-integer convention as the CNF input and the rest of the trajectory stream:

- a positive integer `k` means variable `xk` appears positively in the clause
- a negative integer `-k` means variable `xk` appears negatively in the clause

### Clause Filtering and Ordering

`get_sequence()` uses exactly the same clause set as `get_vcg()`:

- scan `clauses` first, then `learnts`
- skip removed clauses
- skip satisfied clauses
- include unresolved clauses
- include falsified clauses present in a visible UNSAT snapshot

This guarantees that the sequence and the graph always describe the same set of clauses in the same order. A consumer that processes both outputs can align clause indices directly.

### Literal Filtering

`get_sequence()` applies the same candidate-variable filter as `get_vcg()`. Within each included clause, only literals whose variable appears in `candidates` are emitted. Literals whose variable is not a candidate are skipped silently.

This means a clause node with no candidate-variable literals still contributes its terminating `"0"` token, producing an empty clause entry `"0"` in the sequence. This mirrors the `get_vcg()` behavior where such a clause node is present but contributes no edges.

### Example

If `get_vcg()` would emit two clause nodes for `x1 ∨ x2` and `¬x1 ∨ ¬x2`, then `get_sequence()` returns:

```python
["[BOS]", 1, 2, "0", -1, -2, "0", "[SEP]"]
```

### Terminal-State Semantics

`get_sequence()` should be callable in all visible wrapper states.

- when `state == 0`, the sequence contains all currently unsatisfied original and learned clauses
- when `state == 10`, all clauses are satisfied, so the sequence is `["[BOS]", "[SEP]"]`
- when `state == 20`, the sequence contains all clauses still unsatisfied in the terminal snapshot, with no filtering by candidate variables

### Implementation Notes

`get_sequence()` can be implemented as a thin companion to `get_vcg()`. The clause-scanning loop is identical; the difference is that instead of recording node ids and edges, each included clause emits its literals directly into a `std::vector<py::object>`.

- allocate the output vector
- push `"[BOS]"`
- for each included clause, push each literal as a signed int, then push `"0"`
- push `"[SEP]"`
- return the vector as a `py::list`

The pybind11 binding is:

```cpp
py::list get_sequence() const;
```

bound with:

```cpp
.def("get_sequence", &PyMiniSAT::get_sequence)
```

No NumPy dependency is introduced by this method.

## VCG Implementation Plan

The current wrapper structure is a good fit for this feature because `PyMiniSAT` privately inherits `Minisat::Solver` and can inspect `candidates_`, `clauses`, `learnts`, `ca`, `isRemoved(...)`, and `satisfied(...)` directly. The VCG should be assembled on demand when `get_vcg()` is called rather than cached across search steps.

### 1. Add the Binding Entry Point

- add `#include <pybind11/numpy.h>` in `python/minisat_wrapper.cpp`
- add `py::tuple get_vcg() const;` to `PyMiniSAT`
- bind it with `.def("get_vcg", &PyMiniSAT::get_vcg)`

NumPy arrays are the right surface here because the requested API is shape-oriented and is meant for external graph tooling.

### 2. Build the Candidate-Variable Prefix

Implement a helper that walks `candidates_`, deduplicates by absolute variable index, and records a node id for each retained candidate variable. A simple representation is:

```cpp
std::vector<int> candidate_variables;
std::vector<int> var_to_node(nVars(), -1);
```

This keeps `get_vcg()` exactly aligned with the wrapper current notion of branchable variables instead of recomputing branchability independently.

### 3. Collect Included Clauses

Scan `clauses` first and `learnts` second. For each `CRef cr`:

1. skip it if `isRemoved(cr)`
2. read `Clause& clause = ca[cr]`
3. skip it if `satisfied(clause)`
4. otherwise assign the next clause-node id

This uses MiniSAT own live-clause and satisfaction checks, which is safer than reconstructing clause state in wrapper code.

### 4. Emit Node Features

After counting `V` candidate-variable nodes and `C` clause nodes, allocate `x` with shape `[V + C, 2]`, fill rows `[0, V)` with `[1, 0]`, and fill rows `[V, V + C)` with `[0, 1]`. No additional node features are needed in the first version.

### 5. Emit Literal-Incidence Edges

For every included clause node, iterate through its literals. For each literal:

1. compute `var = Minisat::var(lit)`
2. look up `node = var_to_node[var]`
3. skip it if `node == -1`
4. append one edge from `node` to the clause node
5. append `[0, 1]` for a positive literal or `[1, 0]` for a negative literal

This yields `edge_index: [2, E]` and `edge_attr: [E, 2]`, where `E` is the number of retained candidate-variable literal occurrences across all included clauses.

### 6. Materialize NumPy Arrays

Use flat C++ buffers and copy them into `py::array_t` values:

- `std::vector<float>` for `x`
- `std::vector<std::int64_t>` for `edge_index`
- `std::vector<float>` for `edge_attr`

Return them as `py::make_tuple(x_array, edge_index_array, edge_attr_array)`. A zero-copy design is not necessary because this graph is a fresh snapshot.

### 7. Packaging Follow-Up

If `get_vcg()` returns NumPy arrays, the package should declare NumPy as a runtime dependency in addition to the existing `pybind11` build dependency. That change belongs in packaging metadata, not in MiniSAT logic, but it should be part of the implementation checklist.

### 8. Test Plan

Add wrapper-level tests that verify both shape and content:

- unresolved state: candidate variables appear first, node features match the requested one-hot encoding, only unsatisfied clauses are included, and edge signs match literal polarity
- SAT state: returns empty arrays with shapes `(0, 2)`, `(2, 0)`, and `(0, 2)`
- UNSAT state: returns clause nodes for unsatisfied clauses and an empty variable-node prefix
- learned-clause case with `clause_learning=True`: at least one learned clause appears after original clauses

The tests should assert deterministic ordering because downstream graph consumers will often rely on stable node indexing.

### 9. Complexity and Caching

`get_vcg()` should remain an on-demand snapshot. The candidate frontier, clause satisfaction, and learned database can all change after each `step`, so caching would add invalidation complexity without much benefit. The expected cost per call is linear in the number of candidate literals inspected, live clauses scanned, and exported literal incidences.

## Binding Options

The binding choice matters because MiniSAT is written in C++, not C. Approaches that can bind C++ classes directly are much simpler for this design than approaches that can only call a C ABI.

### Option 1: `pybind11`

Key implementation:

- write a small C++ adapter class such as `PyMiniSAT` that owns `Minisat::Solver`
- implement CNF loading, state refresh, candidate extraction, and `step(int literal)` in C++
- expose the class with `pybind11::class_`
- convert `list[list[int]]` to native vectors in the binding boundary

Advantages:

- best fit for a C++ solver with stateful object lifetime
- clean mapping from C++ methods and properties to Python methods and properties
- easy exception translation into Python `ValueError` and `RuntimeError`
- no separate C shim is needed
- good long-term maintainability for a nontrivial API

Disadvantages:

- adds a C++ binding dependency
- requires a compiled extension module, so packaging is more involved than pure Python
- template-heavy error messages can be noisy during build failures

Assessment:

- this is the most practical choice for the wrapper described here

### Option 2: CPython C/C++ extension using `Python.h`

Key implementation:

- define a custom Python object type whose payload stores a pointer to a C++ wrapper object
- write `tp_new`, `tp_dealloc`, property getters, and the `step` method manually
- convert Python lists into native vectors with direct CPython API calls
- raise Python exceptions with `PyErr_SetString`

Advantages:

- no third-party binding dependency
- maximum control over memory layout, object lifetime, and low-level performance
- stable and explicit ABI usage from Python's perspective

Disadvantages:

- highest implementation effort
- reference counting, type slots, and error handling are easy to get wrong
- significantly more boilerplate than `pybind11`
- harder for future contributors to maintain

Assessment:

- viable if external dependencies must be minimized, but not the fastest way to ship this wrapper

### Option 3: `Cython`

Key implementation:

- write a `.pyx` layer with a `cdef class MiniSAT`
- either bind C++ classes directly in Cython or wrap a small C/C++ adapter API
- implement conversion from Python CNF lists to C++ vectors inside the Cython layer
- expose Python properties and `step()` in the `.pyx` class

Advantages:

- less boilerplate than a raw CPython extension
- can get close to handwritten-extension performance
- good middle ground if some logic should live in a Python-like syntax

Disadvantages:

- introduces another language/toolchain into the project
- C++ interop is workable but less straightforward than `pybind11` for idiomatic class bindings
- generated C/C++ can be harder to debug

Assessment:

- a reasonable alternative, especially if the project already uses Cython, but not clearly better than `pybind11` here

### Option 4: `cffi`

Key implementation:

- first create a flat C ABI shim around the C++ solver, for example:
  - `minisat_new()`
  - `minisat_free()`
  - `minisat_load_cnf(...)`
  - `minisat_get_state(...)`
  - `minisat_get_candidates(...)`
  - `minisat_step(...)`
- expose only opaque pointers and plain C integers across the ABI
- call that C layer from Python using `cffi`

Advantages:

- cleaner foreign-function interface than `ctypes`
- good if the same C API might later be used by multiple languages
- Python-side binding code stays relatively small

Disadvantages:

- the real work moves into the C shim, which must still solve object lifetime, buffers, and error reporting
- cannot directly wrap the MiniSAT C++ classes without that shim
- more moving parts than `pybind11` for a Python-only target

Assessment:

- sensible when you explicitly want a reusable C ABI, not when Python is the only consumer

### Option 5: `ctypes`

Key implementation:

- same C ABI shim required as for `cffi`
- compile the shim into a shared library
- load it from Python with `ctypes.CDLL`
- manually define argument and return types, then wrap them in a Python class

Advantages:

- part of the Python standard library
- no Python build-time dependency beyond compiling the native shared library
- useful for quick experiments against a very small C API

Disadvantages:

- not suitable for direct C++ class binding
- requires the most manual marshaling on the Python side among FFI approaches
- easy to make mistakes with pointer ownership and buffer sizes
- awkward for returning dynamic data like `candidates`
- weaker ergonomics for exception propagation and rich object APIs

Assessment:

- acceptable only if the exported API is extremely small and C-style; for this wrapper design it is noticeably less attractive than `pybind11` or `cffi`

### Option 6: `SWIG`

Key implementation:

- write a SWIG interface file around a C++ adapter class or around a C ABI shim
- generate the Python wrapper code automatically
- maintain hand-written typemaps for nested-list CNF conversion if needed

Advantages:

- can automate a large amount of wrapper generation
- useful when the same native interface must be exposed to several languages

Disadvantages:

- generated bindings are often less idiomatic than hand-designed Python APIs
- typemap customization becomes the real work for nontrivial data conversion
- usually overkill for a single compact Python wrapper

Assessment:

- not recommended unless multi-language binding generation is a project goal

## Recommended Architecture By Approach

### If using `pybind11`

Implement:

- a C++ `PyMiniSAT` wrapper class
- direct Python property bindings for `state`, `candidates`, `conflicts`, `decisions`, and `propagations`
- a `step(int)` method that validates input and resumes the solver

Why this works well:

- the Python API and the C++ object model line up directly
- no extra ABI layer is required

### If using `Python.h` directly

Implement:

- a C++ adapter class that owns MiniSAT logic
- a CPython object type that stores a pointer to that adapter
- explicit getters and methods in the CPython slot table

Why this works well:

- full control with no third-party dependency

Why it is costly:

- every conversion and error path must be written manually

### If using `Cython`

Implement:

- a `cdef class MiniSAT`
- Cython declarations for the adapter class or MiniSAT headers
- Python-to-native conversion logic in `.pyx`

Why this works well:

- easier than a raw CPython extension while still compiled

Why it is middling:

- another toolchain layer without a clear benefit over `pybind11` for this API

### If using `cffi` or `ctypes`

Implement:

- a dedicated C ABI shim around the C++ solver
- opaque handle management
- buffer-based candidate export, such as a two-call pattern:
  1. query candidate count
  2. fill a caller-provided integer buffer
- integer error codes or string-based last-error retrieval

Why this works well:

- isolates Python from C++ details
- can support other languages later

Why it is heavier:

- the C shim becomes a second wrapper that must be designed, tested, and maintained

## Recommendation

For this wrapper design, `pybind11` is the strongest default choice.

Reasons:

- the API is object-oriented and stateful
- MiniSAT is already C++
- the wrapper needs custom logic around stepping, candidate extraction, and validation
- Python-friendly exceptions and properties matter more than minimizing one small dependency

If the project explicitly wants a stable language-neutral native API, then a C shim plus `cffi` is the next most defensible design. `ctypes` is usually only justified if keeping the Python side dependency-free is more important than ergonomics and maintainability.

## Example Session

```python
solver = MiniSAT([[1, 2], [-1, 3], [-2, -3]])

assert solver.state == 0
print(solver.candidates)     # for example: [1, -1, 2, -2, 3, -3]
print(solver.decisions)      # 0 or current internal count after initial propagation

solver.step(1)

if solver.state == 0:
    print(solver.candidates) # next branching frontier
    token_count = solver.step_done()  # finish with MiniSAT's default branching heuristic
    print(token_count)
    print(solver.conflicts)           # final statistics are now available
elif solver.state == 10:
    print("SAT")
else:
    print("UNSAT")
```

## Summary

This wrapper defines a controlled CDCL interface:

- Python provides the CNF directly as `list[list[int]]`
- Python chooses each branching literal through `step(literal)`
- Python can hand control back permanently through `step_done()`
- MiniSAT performs all propagation between branch points, with optional learnt-clause insertion
- `state`, `candidates`, and the solver statistics always describe the fully settled current search position
- `clause_learning` controls whether conflicts add learnt clauses
- `dpll` controls whether conflicts backjump normally or instead retry the complement of the latest decision in chronological order
- `pybind11` is the most natural binding approach unless the project specifically wants a reusable C ABI

## Trajectory Token Emission Investigation

The current wrapper implementation in `minisat/python/minisat_wrapper.cpp` already matches the pure DPLL solver on the important search-state pieces in `clause_learning=False, dpll=True` mode:

- both solvers expose the same `state`, `candidates`, `conflicts`, `decisions`, and `propagations`
- both solvers consume one external branch literal per `step(literal)` call
- both solvers perform internal chronological complement retries without incrementing `decisions`

What the wrapper does **not** expose yet in the current implementation is the trajectory itself. `src.dpll.DPLL.step()` returns a token list, while the MiniSAT wrapper currently only mutates internal state. In the planned API, that tokenized interface belongs to `step(...)`, while `step_done()` returns only the count of the remaining tokenized trajectory.

### Backtrack Token Contract

The observable contract in `src/dpll.py` is still the baseline vocabulary for the wrapper:

- explicit branch literals are emitted as signed integers
- each newly implied literal discovered by propagation is emitted as a signed integer
- `"D"` is emitted only when the solver pauses for the next external branch choice
- `"[BT]"` is emitted whenever conflict handling changes the live trail and the wrapper resumes search from a backtracked snapshot
- after `"[BT]"`, the solver replays its surviving assignment trail, prefixing only true external decisions with `"D"`
- terminal calls emit `"SAT"` or `"UNSAT"`

The replay has mode-specific meaning:

- when `dpll=True`, the replayed trail includes the wrapper's immediate complement retry for the most recent unresolved decision
- when `dpll=False`, the replayed trail reflects MiniSAT's non-chronological backjump target plus the newly enqueued asserting literal

There is one important API wrinkle: DPLL also uses `step(None)` for the initial root-level propagation trace. The MiniSAT wrapper currently performs that propagation inside construction, so matching DPLL exactly requires a buffered initial token trace that can be returned on the first `step(None)` call.

### Learnt-Clause Token Extension

For `clause_learning=True`, the wrapper should extend the backtrack token stream so the learnt clause becomes observable to Python.

The intended placement is:

- emit `"[BT]"` first
- emit `"L"` immediately after `"[BT]"` to mark a learnt-clause block
- emit the learnt clause literals after `"L"`
- emit the replayed surviving trail after the learnt clause block

The learnt clause block uses the same signed-integer literal encoding as the CNF input and the rest of the trajectory stream:

- each literal in the learnt clause is emitted as one signed-integer token
- the clause is terminated by the string token `"0"`

A multi-literal learnt clause can therefore appear as:

```python
["[BT]", "L", -3, 5, "0", "D", 1, -2, 3]
```

A unit learnt clause uses the same format:

```python
["[BT]", "L", -1, "0", -1, 2, "SAT"]
```

These examples mean:

- MiniSAT backtracked
- it learnt the clause `(-x3 v x5)`
- it then replayed the surviving live trail, where `1` was an external decision, `-2` was already implied before the conflict, and `3` is the wrapper's internal complement retry

This extension is intentionally a MiniSAT-specific superset of the base DPLL token contract. Exact parity with `src.dpll.DPLL.step()` still applies only to `clause_learning=False, dpll=True`.

Two scope boundaries should be explicit in the documentation:

- this learnt-clause block belongs to any emitted `"[BT]"` segment, regardless of whether the backtrack came from chronological retry (`dpll=True`) or CDCL backjumping (`dpll=False`)
- the replayed trail after that block still depends on mode: `dpll=True` shows the complement retry, while `dpll=False` shows the backjumped trail with the asserting literal

### Recommended Public API Change

To make token emission part of the contract, the wrapper API should change to:

```python
class MiniSAT:
    def step(self, literal: int | None = None) -> list[int | str]: ...
    def step_done(self) -> int: ...
```

Behavior:

- the first `step(None)` returns the constructor-time propagation tokens and then one of `"D"`, `"SAT"`, or `"UNSAT"`
- later `step(literal)` calls return the chosen branch literal, all implied literals discovered before the next pause, and the final control token for that pause point
- calling `step(None)` after the initial buffered trace has been consumed should raise `ValueError`
- calling `step(...)` after termination should still raise `RuntimeError`
- `step_done()` should resume from the current pause point and run to completion using MiniSAT's own branching heuristic
- `step_done()` should return the number of tokens that would have been emitted across that remaining solve, including default-heuristic branch literals, without materializing the token list

A pybind11-friendly signature is:

```cpp
py::list step(py::object literal = py::none());
std::uint64_t step_done();
```

### Where To Hook Token Emission In MiniSAT

The existing wrapper control flow already has the right top-level boundaries:

- constructor: `begin_search()`
- external branch step: `step(int literal)`
- finish-to-terminal step: `step_done()`
- inner search loop: `settle()`
- conflict recovery: `handle_cdcl_conflict(...)` and `handle_dpll_conflict(...)`

The least invasive design is to thread a per-call token buffer through those wrapper-managed methods instead of rewriting MiniSAT's internal `propagate()` loop.

Recommended changes:

- change `begin_search()` so it records constructor-time tokens into `pending_initial_tokens_`
- change `step(...)` so it creates a fresh token buffer for each external call and returns it
- add `step_done()` so it shares the same validation and search machinery, counts the tokens that would have been emitted, and never pauses at a later branch point
- change `settle(...)` to append new implied literals after each call to `propagate()`
- change `handle_cdcl_conflict(...)` so it emits the same `"[BT]"` plus surviving-trail replay shape after a non-chronological backjump and asserting-literal enqueue
- change `handle_dpll_conflict(...)` so it emits `"[BT]", "L", lit1, lit2, ..., "0"` when `clause_learning=True`, and only then the replayed trail snapshot

### Extra State The Wrapper Needs

MiniSAT's native trail is not enough to reconstruct the backtrack token stream exactly.

The current wrapper already stores a `decision_frames_` stack, which is enough for chronological branch flipping. Exact backtrack replay needs two more pieces of wrapper-side state:

```cpp
std::vector<py::object> pending_initial_tokens_;
std::vector<bool> trail_is_external_decision_;
bool initial_tokens_consumed_ = false;
```

Why `trail_is_external_decision_` is necessary:

- `trail_lim` tells us where decision levels start, but not whether the first literal at that level came from Python or from an automatic complement retry
- MiniSAT records both external decisions and internal retries with `uncheckedEnqueue(...)`
- the DPLL token format prefixes only external decisions with `"D"` during a backtrack replay

Without that metadata, the wrapper cannot faithfully reproduce the `"[BT]", ..., "D", lit, ...` snapshot format from `src.dpll.DPLL`.

To expose learnt clauses on that same path, the wrapper also needs a small transient buffer for the most recently analyzed clause on the active conflict:

```cpp
std::vector<int> pending_learnt_clause_tokens_;
```

That buffer should be populated from the `Minisat::vec<Minisat::Lit>` returned by `analyze(...)` only for the duration of the current conflict-handling step. It does not need to become persistent solver state beyond the emitted token stream.

### Minimal Helper Functions

A clean implementation can stay entirely in the wrapper by adding helpers like:

```cpp
void emit_new_propagations(py::list& tokens, int old_trail_size);
void emit_learnt_clause(TokenBuffer& tokens, const Minisat::vec<Minisat::Lit>& learnt_clause) const;
void emit_backtrack_snapshot(py::list& tokens) const;
void cancel_until_with_metadata(int level);
```

Expected responsibilities:

- `emit_new_propagations(...)` scans `trail[old_trail_size:]` after each `propagate()` call, appends the new implied literals in order, and extends `trail_is_external_decision_` with `false` for those new assignments
- `emit_learnt_clause(...)` appends `"L"`, then each learnt-clause literal in order, and then appends `"0"` as an end-of-clause separator
- `emit_backtrack_snapshot(...)` replays the current live trail, inserting `"D"` only where `trail_is_external_decision_[i]` is `true`
- `cancel_until_with_metadata(...)` calls MiniSAT's `cancelUntil(level)` and then shrinks `trail_is_external_decision_` to the new `trail.size()`

This keeps the implementation aligned with the current wrapper architecture in [`minisat/python/minisat_wrapper.cpp`](/home/chengdicao/repos/SATLM/minisat/python/minisat_wrapper.cpp:64) and [`minisat/python/minisat_wrapper.cpp`](/home/chengdicao/repos/SATLM/minisat/python/minisat_wrapper.cpp:326).

### Planned `handle_cdcl_conflict(...)` Flow

The CDCL conflict path should emit the same backtrack marker and surviving-trail replay shape as the DPLL path, even though the recovery policy is different.

The intended sequence is:

1. Call `analyze(confl, learnt_clause, backtrack_level)`.
2. Undo to `backtrack_level` with `cancel_until_with_metadata(backtrack_level)`.
3. Enqueue the asserting literal, either directly for a unit learnt clause or via the learnt or ephemeral reason clause for a longer conflict clause.
4. Mark that asserting literal as non-external in `trail_is_external_decision_`.
5. Emit `"[BT]"`, then emit `"L", lit1, lit2, ..., "0"` when `clause_learning=True`, and then replay the resulting live trail snapshot.
6. Return to `settle(...)` so any further implications appear after the replay.

In this mode the replay does not show a complement retry. Instead, it shows the trail that survives MiniSAT's non-chronological backjump together with the freshly asserted literal that resumes propagation. A unit learnt clause therefore appears as `"[BT]", lit, 0, lit, ...`, where the first `lit` belongs to the learnt-clause block and the second `lit` is the same asserting literal replayed on the live trail.

### Planned `handle_dpll_conflict(...)` Flow

The learned-clause token feature belongs in the existing DPLL conflict path because that is where the wrapper already owns chronological undo and trail replay.

The intended sequence is:

1. If `clause_learning=True`, call `analyze(confl, learnt_clause, ignored_backtrack_level)`.
2. If the learnt clause has more than one literal, keep the current MiniSAT behavior of allocating, attaching, and bumping the learnt clause.
3. Start the emitted backtrack segment with `"[BT]"`.
4. If `clause_learning=True`, emit the analyzed learnt clause as `"L", lit1, lit2, ..., 0`.
5. Undo one chronological decision level with `cancel_until_with_metadata(decisionLevel() - 1)`.
6. If the latest decision has not yet tried its complement, enqueue that complement as an internal non-external assignment.
7. Replay the surviving trail snapshot, prefixing only original Python-driven decisions with `"D"`.
8. Return to `settle(...)` so propagation continues from the flipped branch.

Two details matter here:

- the learnt-clause tokens must be emitted before the trail replay so the consumer can interpret them as metadata about the backtrack event rather than as assignments already present on the live trail
- the complement literal itself should still appear only inside the replayed trail, not inside the learnt-clause block

### Scope Of Exact Compatibility

Exact token parity should be specified against `clause_learning=False, dpll=True` first.

That is the only mode where the current MiniSAT wrapper is intentionally matching the pure DPLL solver's search behavior. In the other modes:

- `dpll=False` now shares the same `"[BT]"` plus surviving-trail replay shape, but the replay encodes MiniSAT's non-chronological backjump rather than DPLL's chronological complement retry
- `clause_learning=True` can extend each `"[BT]"` segment with a learnt-clause block `"L", lit1, lit2, ..., 0` before the replayed trail

Those modes can still share the same base vocabulary for literals, `"D"`, `"[BT]"`, `"SAT"`, and `"UNSAT"`, but they should be documented as supersets rather than exact replicas of `src.dpll.DPLL`.

### Practical Outcome

The main implementation takeaway is straightforward:

- the existing wrapper search logic is already close enough for token emission
- the required work is primarily API and bookkeeping, not a rewrite of MiniSAT's solver core
- the critical missing pieces are a returned token buffer, a buffered initial trace for `step(None)`, and wrapper-side trail metadata for backtrack replay
