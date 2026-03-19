#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include "minisat/core/Solver.h"

namespace py = pybind11;

namespace {

constexpr int kStateUnresolved = 0;
constexpr int kStateSat = 10;
constexpr int kStateUnsat = 20;

class PyMiniSAT : private Minisat::Solver {
public:
    explicit PyMiniSAT(const std::vector<std::vector<int>>& cnf) {
        load_cnf(cnf);
        begin_search();
    }

    int state() const {
        return state_;
    }

    const std::vector<int>& candidates() const {
        return candidates_;
    }

    std::uint64_t conflicts_count() const {
        return conflicts;
    }

    std::uint64_t decisions_count() const {
        return decisions;
    }

    std::uint64_t propagations_count() const {
        return propagations;
    }

    py::object default_branching_literal() const {
        if (state_ != kStateUnresolved)
            return py::none();

        const Minisat::Lit next = peek_default_branch_lit();
        if (next == Minisat::lit_Undef)
            return py::none();

        return py::int_(encode_literal(next));
    }

    void step(int literal) {
        if (state_ != kStateUnresolved)
            throw std::runtime_error("step() is only valid while the solver is unresolved");
        if (!is_candidate(literal))
            throw std::invalid_argument("literal is not a valid branching candidate");

        decisions++;
        newDecisionLevel();
        uncheckedEnqueue(decode_existing_literal(literal));
        settle();
    }

private:
    int state_ = kStateUnresolved;
    std::vector<int> candidates_;
    bool search_initialized_ = false;

    static int encode_literal(Minisat::Lit literal) {
        const int variable = Minisat::var(literal) + 1;
        return Minisat::sign(literal) ? -variable : variable;
    }

    static Minisat::Lit make_literal_unchecked(int literal) {
        const int variable = std::abs(literal) - 1;
        return Minisat::mkLit(variable, literal < 0);
    }

    bool activity_lt(Minisat::Var left, Minisat::Var right) const {
        return activity[left] > activity[right];
    }

    static int heap_left(int index) {
        return index * 2 + 1;
    }

    static int heap_right(int index) {
        return (index + 1) * 2;
    }

    void percolate_down_snapshot(std::vector<Minisat::Var>& heap, int index) const {
        const Minisat::Var value_at_index = heap[static_cast<std::size_t>(index)];
        while (heap_left(index) < static_cast<int>(heap.size())) {
            int child = heap_left(index);
            const int right = heap_right(index);
            if (right < static_cast<int>(heap.size()) && activity_lt(heap[static_cast<std::size_t>(right)], heap[static_cast<std::size_t>(child)]))
                child = right;
            if (!activity_lt(heap[static_cast<std::size_t>(child)], value_at_index))
                break;
            heap[static_cast<std::size_t>(index)] = heap[static_cast<std::size_t>(child)];
            index = child;
        }
        heap[static_cast<std::size_t>(index)] = value_at_index;
    }

    Minisat::Var remove_min_snapshot(std::vector<Minisat::Var>& heap) const {
        const Minisat::Var minimum = heap.front();
        heap.front() = heap.back();
        heap.pop_back();
        if (!heap.empty())
            percolate_down_snapshot(heap, 0);
        return minimum;
    }

    Minisat::Lit peek_default_branch_lit() const {
        std::vector<Minisat::Var> heap_snapshot;
        heap_snapshot.reserve(static_cast<std::size_t>(order_heap.size()));
        for (int index = 0; index < order_heap.size(); ++index)
            heap_snapshot.push_back(order_heap[index]);

        Minisat::Var next = Minisat::var_Undef;
        double seed = random_seed;

        if (Minisat::Solver::drand(seed) < random_var_freq && !heap_snapshot.empty()) {
            next = heap_snapshot[static_cast<std::size_t>(Minisat::Solver::irand(seed, static_cast<int>(heap_snapshot.size())))];
        }

        while (next == Minisat::var_Undef || value(next) != Minisat::l_Undef || !decision[next]) {
            if (heap_snapshot.empty()) {
                next = Minisat::var_Undef;
                break;
            }
            next = remove_min_snapshot(heap_snapshot);
        }

        if (next == Minisat::var_Undef)
            return Minisat::lit_Undef;
        if (user_pol[next] != Minisat::l_Undef)
            return Minisat::mkLit(next, user_pol[next] == Minisat::l_True);
        if (rnd_pol)
            return Minisat::mkLit(next, Minisat::Solver::drand(seed) < 0.5);
        return Minisat::mkLit(next, polarity[next]);
    }

    Minisat::Lit decode_existing_literal(int literal) const {
        if (literal == 0)
            throw std::invalid_argument("literal 0 is not allowed");

        const int variable = std::abs(literal) - 1;
        if (variable < 0 || variable >= nVars())
            throw std::invalid_argument("literal references a variable outside the loaded CNF");

        return make_literal_unchecked(literal);
    }

    void load_cnf(const std::vector<std::vector<int>>& cnf) {
        int max_variable = 0;
        for (const auto& clause : cnf) {
            for (int literal : clause) {
                if (literal == 0)
                    throw std::invalid_argument("CNF clauses may not contain literal 0");
                max_variable = std::max(max_variable, std::abs(literal));
            }
        }

        while (nVars() < max_variable)
            newVar();

        for (const auto& clause : cnf) {
            Minisat::vec<Minisat::Lit> minisat_clause;
            for (int literal : clause)
                minisat_clause.push(make_literal_unchecked(literal));
            addClause_(minisat_clause);
            if (!okay())
                break;
        }
    }

    void begin_search() {
        budgetOff();
        assumptions.clear();
        clearInterrupt();
        model.clear();
        conflict.clear();

        if (!okay()) {
            state_ = kStateUnsat;
            candidates_.clear();
            return;
        }

        solves++;
        max_learnts = nClauses() * learntsize_factor;
        if (max_learnts < min_learnts_lim)
            max_learnts = min_learnts_lim;

        learntsize_adjust_confl = learntsize_adjust_start_confl;
        learntsize_adjust_cnt = static_cast<int>(learntsize_adjust_confl);
        search_initialized_ = true;

        settle();
    }

    bool is_candidate(int literal) const {
        for (int candidate : candidates_) {
            if (candidate == literal)
                return true;
        }
        return false;
    }

    void refresh_candidates() {
        candidates_.clear();
        candidates_.reserve(static_cast<std::size_t>(nVars()) * 2);
        for (int variable = 0; variable < nVars(); ++variable) {
            if (!decision[variable] || value(variable) != Minisat::l_Undef)
                continue;
            candidates_.push_back(variable + 1);
            candidates_.push_back(-(variable + 1));
        }
    }

    void store_model() {
        model.clear();
        model.growTo(nVars());
        for (int variable = 0; variable < nVars(); ++variable)
            model[variable] = value(variable);
    }

    void settle() {
        if (!search_initialized_)
            throw std::runtime_error("internal error: search used before initialization");

        for (;;) {
            Minisat::CRef confl = propagate();
            if (confl != Minisat::CRef_Undef) {
                conflicts++;
                if (decisionLevel() == 0) {
                    ok = false;
                    candidates_.clear();
                    state_ = kStateUnsat;
                    return;
                }

                int backtrack_level = 0;
                Minisat::vec<Minisat::Lit> learnt_clause;
                analyze(confl, learnt_clause, backtrack_level);
                cancelUntil(backtrack_level);

                if (learnt_clause.size() == 1) {
                    uncheckedEnqueue(learnt_clause[0]);
                } else {
                    Minisat::CRef cr = ca.alloc(learnt_clause, true);
                    learnts.push(cr);
                    attachClause(cr);
                    claBumpActivity(ca[cr]);
                    uncheckedEnqueue(learnt_clause[0], cr);
                }

                varDecayActivity();
                claDecayActivity();

                if (--learntsize_adjust_cnt == 0) {
                    learntsize_adjust_confl *= learntsize_adjust_inc;
                    learntsize_adjust_cnt = static_cast<int>(learntsize_adjust_confl);
                    max_learnts *= learntsize_inc;
                }

                continue;
            }

            if (decisionLevel() == 0 && !simplify()) {
                ok = false;
                candidates_.clear();
                state_ = kStateUnsat;
                return;
            }

            if (learnts.size() - nAssigns() >= max_learnts)
                reduceDB();

            refresh_candidates();
            if (candidates_.empty()) {
                decisions++;
                store_model();
                state_ = kStateSat;
                return;
            }

            state_ = kStateUnresolved;
            return;
        }
    }
};

}  // namespace

PYBIND11_MODULE(minisat_wrapper, m) {
    m.doc() = "Step-wise pybind11 wrapper around MiniSAT";

    py::class_<PyMiniSAT>(m, "MiniSAT")
        .def(py::init<const std::vector<std::vector<int>>&>(), py::arg("cnf"))
        .def_property_readonly("state", &PyMiniSAT::state)
        .def_property_readonly("candidates", &PyMiniSAT::candidates)
        .def_property_readonly("conflicts", &PyMiniSAT::conflicts_count)
        .def_property_readonly("decisions", &PyMiniSAT::decisions_count)
        .def_property_readonly("propagations", &PyMiniSAT::propagations_count)
        .def("default_branching_literal", &PyMiniSAT::default_branching_literal)
        .def("step", &PyMiniSAT::step, py::arg("literal"));

    m.attr("STATE_UNRESOLVED") = py::int_(kStateUnresolved);
    m.attr("STATE_SAT") = py::int_(kStateSat);
    m.attr("STATE_UNSAT") = py::int_(kStateUnsat);
}
