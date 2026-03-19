# MiniSAT Python Wrapper Design

## Goal

Expose MiniSAT as a step-wise Python object that accepts a CNF formula directly from Python data structures and allows an external controller to drive branching decisions one step at a time.

The wrapper presents the solver as:

```python
solver = MiniSAT(cnf)
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
solver = MiniSAT(cnf)
```

## Public API

The Python wrapper exposes a single stateful class:

```python
class MiniSAT:
    def __init__(self, cnf: list[list[int]]) -> None: ...

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

## Construction

`MiniSAT(cnf)` performs these actions:

1. Create an internal MiniSAT `Solver`.
2. Scan the CNF and allocate enough solver variables to cover the largest absolute literal.
3. Add every clause to the solver.
4. Run unit propagation until the solver reaches a stable branching point or a terminal result.
5. Initialize all public properties.

Construction is therefore not a passive load step. When the object is returned:

- `state == 20` if the formula is already inconsistent
- `state == 10` if all variables are fixed without any further branching
- `state == 0` if the solver is waiting for an external branching decision

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
5. Continue through propagation, conflict analysis, learnt-clause insertion, and backtracking as needed.
6. Stop only when one of the following becomes true:
   - the solver proves SAT
   - the solver proves UNSAT
   - propagation is complete and a new external branching choice is required
7. Refresh `state`, `candidates`, `conflicts`, `decisions`, and `propagations`.

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

## Error Handling

The wrapper should reject malformed CNF input during construction.

Recommended validation:

- `cnf` must be a list of lists
- every literal must be a non-zero integer
- empty clauses are allowed and should immediately make the solver UNSAT

Recommended `step` errors:

- raise `RuntimeError` if `step` is called when `state != 0`
- raise `ValueError` if the requested literal is not in `candidates`

## Implementation Notes

The main design requirement is that the wrapper exposes a paused-search interface, while MiniSAT is naturally written around an internal CDCL loop. The C++ binding layer therefore needs an adapter that can:

- stop after propagation reaches a decision point
- accept an externally supplied decision literal
- resume the normal MiniSAT search procedure

Conceptually, the adapter behaves like a restricted version of `search()` where branch selection is delegated to Python instead of always calling MiniSAT's internal `pickBranchLit()`.

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
- MiniSAT performs all propagation and learning between branch points
- `state`, `candidates`, and the solver statistics always describe the fully settled current search position
- `pybind11` is the most natural binding approach unless the project specifically wants a reusable C ABI
