# MiniSAT Python Wrapper Design

## Goal

Expose MiniSAT as a step-wise Python object that accepts a CNF formula directly from Python data structures and allows an external controller to drive branching decisions one step at a time.

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

    def step(self, literal: int) -> None: ...
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

When `dpll=True`, the final bullet changes slightly: MiniSAT still owns propagation and termination, but the wrapper replaces MiniSAT's non-chronological backjumping with an explicit chronological branch-flip policy.

## Error Handling

The wrapper should reject malformed CNF input during construction.

Recommended validation:

- `cnf` must be a list of lists
- every literal must be a non-zero integer
- empty clauses are allowed and should immediately make the solver UNSAT
- `clause_learning` and `dpll` should be accepted as booleans

Recommended `step` errors:

- raise `RuntimeError` if `step` is called when `state != 0`
- raise `ValueError` if the requested literal is not in `candidates`

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
elif solver.state == 10:
    print("SAT")
else:
    print("UNSAT")
```

## Summary

This wrapper defines a controlled CDCL interface:

- Python provides the CNF directly as `list[list[int]]`
- Python chooses each branching literal through `step(literal)`
- MiniSAT performs all propagation between branch points, with optional learnt-clause insertion
- `state`, `candidates`, and the solver statistics always describe the fully settled current search position
- `clause_learning` controls whether conflicts add learnt clauses
- `dpll` controls whether conflicts backjump normally or instead retry the complement of the latest decision in chronological order
- `pybind11` is the most natural binding approach unless the project specifically wants a reusable C ABI
