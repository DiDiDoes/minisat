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

    static Minisat::Lit make_literal_unchecked(int literal) {
        const int variable = std::abs(literal) - 1;
        return Minisat::mkLit(variable, literal < 0);
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
        .def("step", &PyMiniSAT::step, py::arg("literal"));

    m.attr("STATE_UNRESOLVED") = py::int_(kStateUnresolved);
    m.attr("STATE_SAT") = py::int_(kStateSat);
    m.attr("STATE_UNSAT") = py::int_(kStateUnsat);
}
