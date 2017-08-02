from __future__ import absolute_import, print_function, division
from six.moves import map, range, zip

import numpy
import itertools
from functools import partial, reduce
from collections import OrderedDict
from gem.optimise import make_sum, make_product
from gem.refactorise import Monomial, collect_monomials
from gem.unconcatenate import unconcatenate
from gem.node import traversal
from gem.gem import IndexSum, Failure, Sum, one
from gem.utils import groupby

from tsfc.logging import logger

import tsfc.spectral as spectral


Integrals = spectral.Integrals


def flatten(var_reps, index_cache):
    """Flatten mode-specific intermediate representation to a series of
    assignments.

    :arg var_reps: series of (return variable, [integral representation]) pairs
    :arg index_cache: cache :py:class:`dict` for :py:func:`unconcatenate`

    :returns: series of (return variable, GEM expression root) pairs
    """
    try:
        from firedrake import Citations
        Citations().register("Luporini2016")
    except ImportError:
        pass
    assignments = unconcatenate([(variable, reduce(Sum, reps))
                                 for variable, reps in var_reps],
                                cache=index_cache)

    def group_key(assignment):
        variable, expression = assignment
        return variable.free_indices

    for argument_indices, assignment_group in groupby(assignments, group_key):
        variables, expressions = zip(*assignment_group)
        expressions = optimise_expressions(expressions, argument_indices)
        for var, expr in zip(variables, expressions):
            yield (var, expr)


finalise_options = dict(remove_componenttensors=False)


def optimise_expressions(expressions, argument_indices):
    """Perform loop optimisations on GEM DAGs

    :arg expressions: list of GEM DAGs
    :arg argument_indices: tuple of argument indices

    :returns: list of optimised GEM DAGs
    """
    # Skip optimisation for if Failure node is present
    for n in traversal(expressions):
        if isinstance(n, Failure):
            return expressions

    # Apply argument factorisation unconditionally
    classifier = partial(spectral.classify, set(argument_indices))
    monomial_sums = collect_monomials(expressions, classifier)
    return [optimise_monomial_sum(ms, argument_indices) for ms in monomial_sums]


def monomial_sum_to_expression(monomial_sum):
    """Convert a monomial sum to a GEM expression.

    :arg monomial_sum: an iterable of :class:`Monomial`s

    :returns: GEM expression
    """
    indexsums = []  # The result is summation of indexsums
    # Group monomials according to their sum indices
    groups = groupby(monomial_sum, key=lambda m: frozenset(m.sum_indices))
    # Create IndexSum's from each monomial group
    for _, monomials in groups:
        sum_indices = monomials[0].sum_indices
        products = [make_product(monomial.atomics + (monomial.rest,)) for monomial in monomials]
        indexsums.append(IndexSum(make_sum(products), sum_indices))
    return make_sum(indexsums)


def index_extent(factor, argument_indices):
    """Compute the product of the extents of argument indices of a GEM expression

    :arg factor: GEM expression
    :arg argument_indices: set of argument indices

    :returns: product of extents of argument indices
    """
    return numpy.prod([i.extent for i in factor.free_indices if i in argument_indices])


def find_optimal_atomics(monomials, argument_indices):
    """Find optimal atomic common subexpressions, which produce least number of
    terms in the resultant IndexSum when factorised.

    :arg monomials: A list of :class:`Monomial`s, all of which should have
                    the same sum indices
    :arg argument_indices: tuple of argument indices

    :returns: list of atomic GEM expressions
    """
    atomics = tuple(OrderedDict.fromkeys(itertools.chain(*(monomial.atomics for monomial in monomials))))

    def cost(solution):
        extent = sum(map(lambda atomic: index_extent(atomic, argument_indices), solution))
        # Prefer shorter solutions, but larger extents
        return (len(solution), -extent)

    optimal_solution = set(atomics)  # pessimal but feasible solution
    solution = set()

    max_it = 1 << 12
    it = iter(range(max_it))

    def solve(idx):
        while idx < len(monomials) and solution.intersection(monomials[idx].atomics):
            idx += 1

        if idx < len(monomials):
            if len(solution) < len(optimal_solution):
                for atomic in monomials[idx].atomics:
                    solution.add(atomic)
                    solve(idx + 1)
                    solution.remove(atomic)
        else:
            if cost(solution) < cost(optimal_solution):
                optimal_solution.clear()
                optimal_solution.update(solution)
            next(it)

    try:
        solve(0)
    except StopIteration:
        logger.warning("Solution to ILP problem may not be optimal: search "
                       "interrupted after examining %d solutions.", max_it)

    return tuple(atomic for atomic in atomics if atomic in optimal_solution)


def factorise_atomics(monomials, optimal_atomics, argument_indices):
    """Group and factorise monomials using a list of atomics as common
    subexpressions. Create new monomials for each group and optimise them recursively.

    :arg monomials: an iterable of :class:`Monomial`s, all of which should have
                    the same sum indices
    :arg optimal_atomics: list of tuples of atomics to be used as common subexpression
    :arg argument_indices: tuple of argument indices

    :returns: an iterable of :class:`Monomials`s after factorisation
    """
    if not optimal_atomics or len(monomials) <= 1:
        return monomials

    # Group monomials with respect to each optimal atomic
    def group_key(monomial):
        for oa in optimal_atomics:
            if oa in monomial.atomics:
                return oa
        assert False, "Expect at least one optimal atomic per monomial."
    factor_group = groupby(monomials, key=group_key)

    # We should not drop monomials
    assert sum(len(ms) for _, ms in factor_group) == len(monomials)

    sum_indices = next(iter(monomials)).sum_indices
    new_monomials = []
    for oa, monomials in factor_group:
        # Create new MonomialSum for the factorised out terms
        sub_monomials = []
        for monomial in monomials:
            atomics = list(monomial.atomics)
            atomics.remove(oa)  # remove common factor
            sub_monomials.append(Monomial((), tuple(atomics), monomial.rest))
        # Continue to factorise the remaining expression
        sub_monomials = optimise_monomials(sub_monomials, argument_indices)
        if len(sub_monomials) == 1:
            # Factorised part is a product, we add back the common atomics then
            # add to new MonomialSum directly rather than forming a product node
            # Retaining the monomial structure enables applying associativity
            # when forming GEM nodes later.
            sub_monomial, = sub_monomials
            new_monomials.append(
                Monomial(sum_indices, (oa,) + sub_monomial.atomics, sub_monomial.rest))
        else:
            # Factorised part is a summation, we need to create a new GEM node
            # and multiply with the common factor
            node = monomial_sum_to_expression(sub_monomials)
            # If the free indices of the new node intersect with argument indices,
            # add to the new monomial as `atomic`, otherwise add as `rest`.
            # Note: we might want to continue to factorise with the new atomics
            # by running optimise_monoials twice.
            if set(argument_indices) & set(node.free_indices):
                new_monomials.append(Monomial(sum_indices, (oa, node), one))
            else:
                new_monomials.append(Monomial(sum_indices, (oa, ), node))
    return new_monomials


def optimise_monomial_sum(monomial_sum, argument_indices):
    """Choose optimal common atomic subexpressions and factorise a
    :class:`MonomialSum` object to create a GEM expression.

    :arg monomial_sum: a :class:`MonomialSum` object
    :arg argument_indices: tuple of argument indices

    :returns: factorised GEM expression
    """
    groups = groupby(monomial_sum, key=lambda m: frozenset(m.sum_indices))
    new_monomials = []
    for _, monomials in groups:
        new_monomials.extend(optimise_monomials(monomials, argument_indices))
    return monomial_sum_to_expression(new_monomials)


def optimise_monomials(monomials, argument_indices):
    """Choose optimal common atomic subexpressions and factorise an iterable
    of monomials.

    :arg monomials: a list of :class:`Monomial`s, all of which should have
                    the same sum indices
    :arg argument_indices: tuple of argument indices

    :returns: an iterable of factorised :class:`Monomials`s
    """
    assert len(set(frozenset(m.sum_indices) for m in monomials)) <= 1,\
        "All monomials required to have same sum indices for factorisation"

    optimal_atomics = find_optimal_atomics(monomials, argument_indices)
    return factorise_atomics(monomials, optimal_atomics, argument_indices)