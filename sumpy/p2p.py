from __future__ import division, absolute_import

__copyright__ = """
Copyright (C) 2012 Andreas Kloeckner
Copyright (C) 2018 Alexandru Fikl
"""

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import six
from six.moves import range

import numpy as np
import loopy as lp
from loopy.version import MOST_RECENT_LANGUAGE_VERSION
from pymbolic import var

from pytools import memoize_method, memoize_in
from sumpy.tools import KernelComputation, KernelCacheWrapper


__doc__ = """

Particle-to-particle
--------------------

.. autoclass:: P2PBase
.. autoclass:: P2P
.. autoclass:: P2PMatrixGenerator
.. autoclass:: P2PMatrixBlockGenerator
.. autoclass:: P2PFromCSR

"""


# LATER:
# - Optimization for source == target (postpone)

# {{{ p2p base class

class P2PBase(KernelComputation, KernelCacheWrapper):
    def __init__(self, ctx, kernels, exclude_self, strength_usage=None,
            value_dtypes=None,
            options=[], name=None, device=None):
        """
        :arg kernels: list of :class:`sumpy.kernel.Kernel` instances
        :arg strength_usage: A list of integers indicating which expression
          uses which source strength indicator. This implicitly specifies the
          number of strength arrays that need to be passed.
          Default: all kernels use the same strength.
        """
        KernelComputation.__init__(self, ctx, kernels, strength_usage,
                value_dtypes,
                name, options, device)

        self.exclude_self = exclude_self

        from pytools import single_valued
        self.dim = single_valued(knl.dim for knl in self.kernels)

    def get_cache_key(self):
        return (type(self).__name__, tuple(self.kernels), self.exclude_self,
                tuple(self.strength_usage), tuple(self.value_dtypes))

    def get_loopy_insns_and_result_names(self):
        from sumpy.symbolic import make_sym_vector
        dvec = make_sym_vector("d", self.dim)

        from sumpy.assignment_collection import SymbolicAssignmentCollection
        sac = SymbolicAssignmentCollection()

        result_names = [
                sac.assign_unique("knl%d" % i,
                    knl.postprocess_at_target(
                        knl.postprocess_at_source(
                            knl.get_expression(dvec),
                            dvec),
                        dvec)
                    )
                for i, knl in enumerate(self.kernels)]

        sac.run_global_cse()

        from sumpy.codegen import to_loopy_insns
        loopy_insns = to_loopy_insns(six.iteritems(sac.assignments),
                vector_names=set(["d"]),
                pymbolic_expr_maps=[
                        knl.get_code_transformer() for knl in self.kernels],
                retain_names=result_names,
                complex_dtype=np.complex128  # FIXME
                )

        return loopy_insns, result_names

    def get_strength_or_not(self, isrc, kernel_idx):
        return var("strength").index((self.strength_usage[kernel_idx], isrc))

    def get_kernel_exprs(self, result_names):
        from pymbolic import var

        isrc_sym = var("isrc")
        exprs = [var(name) * self.get_strength_or_not(isrc_sym, i)
                 for i, name in enumerate(result_names)]

        if self.exclude_self:
            from pymbolic.primitives import If, Variable
            exprs = [If(Variable("is_self"), 0, expr) for expr in exprs]

        return [lp.Assignment(id=None,
                    assignee="pair_result_%d" % i, expression=expr,
                    temp_var_type=lp.auto)
                for i, expr in enumerate(exprs)]

    def get_default_src_tgt_arguments(self):
        from sumpy.tools import gather_loopy_source_arguments
        return ([
                lp.GlobalArg("sources", None,
                    shape=(self.dim, "nsources")),
                lp.GlobalArg("targets", None,
                   shape=(self.dim, "ntargets")),
                lp.ValueArg("nsources", None),
                lp.ValueArg("ntargets", None)] +
                ([lp.GlobalArg("target_to_source", None, shape=("ntargets",))]
                    if self.exclude_self else []) +
                gather_loopy_source_arguments(self.kernels))

    def get_kernel(self):
        raise NotImplementedError

    def get_optimized_kernel(self, targets_is_obj_array, sources_is_obj_array):
        # FIXME
        knl = self.get_kernel()

        if sources_is_obj_array:
            knl = lp.tag_array_axes(knl, "sources", "sep,C")
        if targets_is_obj_array:
            knl = lp.tag_array_axes(knl, "targets", "sep,C")

        knl = lp.split_iname(knl, "itgt", 1024, outer_tag="g.0")
        return knl


# }}}


# {{{ P2P point-interaction calculation

class P2P(P2PBase):
    """Direct applier for P2P interactions."""

    default_name = "p2p_apply"

    def get_kernel(self):
        loopy_insns, result_names = self.get_loopy_insns_and_result_names()
        kernel_exprs = self.get_kernel_exprs(result_names)
        arguments = (
            self.get_default_src_tgt_arguments() +
            [
                lp.GlobalArg("strength", None,
                    shape="nstrengths, nsources", dim_tags="sep,C"),
                lp.GlobalArg("result", None,
                    shape="nresults, ntargets", dim_tags="sep,C")
            ])

        loopy_knl = lp.make_kernel(["""
            {[itgt, isrc, idim]: \
                0 <= itgt < ntargets and \
                0 <= isrc < nsources and \
                0 <= idim < dim}
            """],
            self.get_kernel_scaling_assignments()
            + ["for itgt, isrc"]
            + ["<> d[idim] = targets[idim, itgt] - sources[idim, isrc]"]
            + ["<> is_self = (isrc == target_to_source[itgt])"
                if self.exclude_self else ""]
            + loopy_insns + kernel_exprs
            + ["""
                result[{i}, itgt] = knl_{i}_scaling * \
                    simul_reduce(sum, isrc, pair_result_{i}) {{inames=itgt}}
               """.format(i=iknl)
               for iknl in range(len(self.kernels))]
            + ["end"],
            arguments,
            assumptions="nsources>=1 and ntargets>=1",
            name=self.name,
            fixed_parameters=dict(
                dim=self.dim,
                nstrengths=self.strength_count,
                nresults=len(self.kernels)),
            lang_version=MOST_RECENT_LANGUAGE_VERSION)

        loopy_knl = lp.tag_inames(loopy_knl, "idim*:unr")

        for knl in self.kernels:
            loopy_knl = knl.prepare_loopy_kernel(loopy_knl)

        return loopy_knl

    def __call__(self, queue, targets, sources, strength, **kwargs):
        from pytools.obj_array import is_obj_array
        knl = self.get_cached_optimized_kernel(
                targets_is_obj_array=(
                    is_obj_array(targets) or isinstance(targets, (tuple, list))),
                sources_is_obj_array=(
                    is_obj_array(sources) or isinstance(sources, (tuple, list))))

        return knl(queue, sources=sources, targets=targets, strength=strength,
                **kwargs)

# }}}


# {{{ P2P matrix writer

class P2PMatrixGenerator(P2PBase):
    """Generator for P2P interaction matrix entries."""

    default_name = "p2p_matrix"

    def get_strength_or_not(self, isrc, kernel_idx):
        return 1

    def get_kernel(self):
        loopy_insns, result_names = self.get_loopy_insns_and_result_names()
        kernel_exprs = self.get_kernel_exprs(result_names)
        arguments = (
            self.get_default_src_tgt_arguments() +
            [lp.GlobalArg("result_%d" % i, dtype,
                shape="ntargets,nsources")
             for i, dtype in enumerate(self.value_dtypes)])

        loopy_knl = lp.make_kernel(["""
            {[itgt, isrc, idim]: \
                0 <= itgt < ntargets and \
                0 <= isrc < nsources and \
                0 <= idim < dim}
            """],
            self.get_kernel_scaling_assignments()
            + ["for itgt, isrc"]
            + ["<> d[idim] = targets[idim, itgt] - sources[idim, isrc]"]
            + ["<> is_self = (isrc == target_to_source[itgt])"
                if self.exclude_self else ""]
            + loopy_insns + kernel_exprs
            + ["""
                result_{i}[itgt, isrc] = \
                    knl_{i}_scaling * pair_result_{i} {{inames=isrc:itgt}}
                """.format(i=iknl)
                for iknl in range(len(self.kernels))]
            + ["end"],
            arguments,
            assumptions="nsources>=1 and ntargets>=1",
            name=self.name,
            fixed_parameters=dict(dim=self.dim),
            lang_version=MOST_RECENT_LANGUAGE_VERSION)

        loopy_knl = lp.tag_inames(loopy_knl, "idim*:unr")

        for knl in self.kernels:
            loopy_knl = knl.prepare_loopy_kernel(loopy_knl)

        return loopy_knl

    def __call__(self, queue, targets, sources, **kwargs):
        from pytools.obj_array import is_obj_array
        knl = self.get_cached_optimized_kernel(
                targets_is_obj_array=(
                    is_obj_array(targets) or isinstance(targets, (tuple, list))),
                sources_is_obj_array=(
                    is_obj_array(sources) or isinstance(sources, (tuple, list))))

        return knl(queue, sources=sources, targets=targets, **kwargs)

# }}}


# {{{ P2P matrix block writer

class P2PMatrixLinearIndex(object):
    def __init__(self, queue, tgtindices, srcindices, tgtranges, srcranges):
        self.queue = queue
        self.tgtindices = tgtindices
        self.tgtranges = tgtranges
        self.srcindices = srcindices
        self.srcranges = srcranges

    @memoize_method
    def block_ranges(self):
        @memoize_in(self, "linear_cumsum_knl")
        def cumsum():
            loopy_knl = lp.make_kernel(
                "{[i, j]: 0 <= i < nranges and 0 <= j <= i}",
                """
                blkranges[0] = 0
                blkranges[i + 1] = reduce(sum, j, \
                    (srcranges[j + 1] - srcranges[j]) * \
                    (tgtranges[j + 1] - tgtranges[j])) \
                """,
                [
                    lp.GlobalArg("tgtranges", None, shape="nranges + 1"),
                    lp.GlobalArg("srcranges", None, shape="nranges + 1"),
                    lp.GlobalArg("blkranges", np.int32, shape="nranges + 1"),
                    lp.ValueArg("nranges", None)
                ],
                name="linear_cumsum_knl",
                default_offset=lp.auto,
                lang_version=MOST_RECENT_LANGUAGE_VERSION)

            loopy_knl = lp.realize_reduction(loopy_knl, force_scan=True,
                force_outer_iname_for_scan="i")
            return loopy_knl

        _, (blkranges,) = cumsum()(self.queue,
            tgtranges=self.tgtranges, srcranges=self.srcranges);

        return blkranges

    @memoize_method
    def linear_indices(self):
        @memoize_in(self, "linear_index_knl")
        def linear_index():
            loopy_knl = lp.make_kernel([
                "{[irange]: 0 <= irange < nranges}",
                "{[itgt, isrc]: 0 <= itgt < ntgtblock and 0 <= isrc < nsrcblock}"
                ],
                """
                for irange
                    <> ntgtblock = tgtranges[irange + 1] - tgtranges[irange]
                    <> nsrcblock = srcranges[irange + 1] - srcranges[irange]

                    for itgt, isrc
                        <> imat = blkranges[irange] + (nsrcblock * itgt + isrc)

                        rowindices[imat] = tgtindices[tgtranges[irange] + itgt] \
                            {id_prefix=write_index}
                        colindices[imat] = srcindices[srcranges[irange] + isrc] \
                            {id_prefix=write_index}
                    end
                end
                """,
                [
                    lp.GlobalArg("srcindices", None),
                    lp.GlobalArg("tgtindices", None),
                    lp.GlobalArg("srcranges", None, shape="nranges + 1"),
                    lp.GlobalArg("tgtranges", None, shape="nranges + 1"),
                    lp.GlobalArg("blkranges", None, shape="nranges + 1"),
                    lp.GlobalArg("rowindices", None, shape="nresults"),
                    lp.GlobalArg("colindices", None, shape="nresults"),
                    lp.ValueArg("nresults", None),
                    lp.ValueArg("nranges", None),
                    "..."
                ],
                name="linear_index_knl",
                default_offset=lp.auto,
                silenced_warnings="write_race(write_index*)",
                lang_version=MOST_RECENT_LANGUAGE_VERSION)
            loopy_knl = lp.split_iname(loopy_knl, "irange", 128, outer_tag="g.0")

            return loopy_knl

        blkranges = self.block_ranges()
        _, (rowindices, colindices) = linear_index()(self.queue,
            tgtindices=self.tgtindices, srcindices=self.srcindices,
            tgtranges=self.tgtranges, srcranges=self.srcranges,
            blkranges=blkranges, nresults=blkranges[-1])

        return rowindices, colindices, blkranges


class P2PMatrixBlockGenerator(P2PBase):
    """Generator for a subset of P2P interaction matrix entries."""

    default_name = "p2p_block"

    def get_strength_or_not(self, isrc, kernel_idx):
        return 1

    def get_kernel(self):
        loopy_insns, result_names = self.get_loopy_insns_and_result_names()
        kernel_exprs = self.get_kernel_exprs(result_names)
        arguments = (
            self.get_default_src_tgt_arguments() +
            [
                lp.GlobalArg("srcindices", None, shape="nresults"),
                lp.GlobalArg("tgtindices", None, shape="nresults"),
                lp.ValueArg("nresults", None)
            ] +
            [lp.GlobalArg("result_%d" % i, dtype, shape="nresults")
             for i, dtype in enumerate(self.value_dtypes)])

        loopy_knl = lp.make_kernel(
            "{[imat, idim]: 0 <= imat < nresults and 0 <= idim < dim}",
            self.get_kernel_scaling_assignments()
            + ["""
                for imat
                    <> d[idim] = targets[idim, tgtindices[imat]] - \
                                 sources[idim, srcindices[imat]]
            """]
            + ["""
                    <> is_self = (srcindices[imat] ==
                                  target_to_source[tgtindices[imat]])
                """ if self.exclude_self else ""]
            + loopy_insns + kernel_exprs
            + ["""
                    result_{i}[imat] = \
                        knl_{i}_scaling * pair_result_{i} \
                            {{id_prefix=write_p2p}}
                """.format(i=iknl)
                for iknl in range(len(self.kernels))]
            + ["end"],
            arguments,
            assumptions="nresults>=1",
            silenced_warnings="write_race(write_p2p*)",
            name=self.name,
            fixed_parameters=dict(dim=self.dim),
            lang_version=MOST_RECENT_LANGUAGE_VERSION)

        loopy_knl = lp.tag_inames(loopy_knl, "idim*:unr")
        loopy_knl = lp.add_dtypes(loopy_knl,
            dict(nsources=np.int32, ntargets=np.int32))

        for knl in self.kernels:
            loopy_knl = knl.prepare_loopy_kernel(loopy_knl)

        return loopy_knl

    def get_optimized_kernel(self, targets_is_obj_array, sources_is_obj_array):
        # FIXME
        knl = self.get_kernel()

        if sources_is_obj_array:
            knl = lp.tag_array_axes(knl, "sources", "sep,C")
        if targets_is_obj_array:
            knl = lp.tag_array_axes(knl, "targets", "sep,C")

        knl = lp.split_iname(knl, "imat", 1024, outer_tag="g.0")
        return knl

    def __call__(self, queue, targets, sources, index_set, **kwargs):
        """Construct a set of blocks from the full P2P interaction matrix.
        The relevant subset of indices for block :math:`i` is given by the
        row indices `tgtindices[tgtranges[i]:tgtranges[i + 1]]` and the
        column indices `srcindices[srcranges[i]:srcranges[i + 1]]`.

        :arg tgtindices: list of row indices for all blocks.
        :arg srcindices: list of column indices for all blocks.
        :arg tgtranges: list used to index `tgtindices` in each block.
        :arg srcindices: list used to index `srcindices` in each block.

        :return: a tuple containing `(kernel_1_block, ... kernel_n_block,
        rowindices, colindices, blkranges)`. The set `(rowindices, colindices)`
        can be used to place each element of each block in the full matrix.
        Each `kernel_i_block` is a one-dimensional array containing all the
        blocks, i.e. the ith kernel evaluated at each of the input
        source-target pairs. To retrieve a given block :math:`j`, one can
        use `kernel_i_block[blkranges[j]:blkranges[j + 1]]`, which can then
        be reshaped into the original block size using the `tgtranges` and
        `srcranges` index lists.
        """
        from pytools.obj_array import is_obj_array
        knl = self.get_cached_optimized_kernel(
                targets_is_obj_array=(
                    is_obj_array(targets) or isinstance(targets, (tuple, list))),
                sources_is_obj_array=(
                    is_obj_array(sources) or isinstance(sources, (tuple, list))))

        rowindices, colindices, _ = index_set.linear_indices()
        return knl(queue, targets=targets, sources=sources,
            tgtindices=rowindices, srcindices=colindices, **kwargs)

# }}}


# {{{ P2P from CSR-like interaction list

class P2PFromCSR(P2PBase):
    default_name = "p2p_from_csr"

    def get_kernel(self):
        loopy_insns, result_names = self.get_loopy_insns_and_result_names()
        kernel_exprs = self.get_kernel_exprs(result_names)
        arguments = (
            self.get_default_src_tgt_arguments() +
            [
                lp.GlobalArg("box_target_starts",
                    None, shape=None),
                lp.GlobalArg("box_target_counts_nonchild",
                    None, shape=None),
                lp.GlobalArg("box_source_starts",
                    None, shape=None),
                lp.GlobalArg("box_source_counts_nonchild",
                    None, shape=None),
                lp.GlobalArg("source_box_starts",
                    None, shape=None),
                lp.GlobalArg("source_box_lists",
                    None, shape=None),
                lp.GlobalArg("strength", None,
                    shape="nstrengths, nsources", dim_tags="sep,C"),
                lp.GlobalArg("result", None,
                    shape="nkernels, ntargets", dim_tags="sep,C"),
                "..."
            ])

        loopy_knl = lp.make_kernel([
            "{[itgt_box]: 0 <= itgt_box < ntgt_boxes}",
            "{[isrc_box]: isrc_box_start <= isrc_box < isrc_box_end}",
            "{[itgt, isrc, idim]: \
                itgt_start <= itgt < itgt_end and \
                isrc_start <= isrc < isrc_end and \
                0 <= idim < dim}",
            ],
            self.get_kernel_scaling_assignments()
            + ["""
                for itgt_box
                <> tgt_ibox = target_boxes[itgt_box]
                <> itgt_start = box_target_starts[tgt_ibox]
                <> itgt_end = itgt_start + box_target_counts_nonchild[tgt_ibox]

                <> isrc_box_start = source_box_starts[itgt_box]
                <> isrc_box_end = source_box_starts[itgt_box+1]

                for isrc_box
                    <> src_ibox = source_box_lists[isrc_box]
                    <> isrc_start = box_source_starts[src_ibox]
                    <> isrc_end = isrc_start + box_source_counts_nonchild[src_ibox]

                    for itgt
                    for isrc
                        <> d[idim] = \
                            targets[idim, itgt] - sources[idim, isrc] {dup=idim}
            """] + ["""
                        <> is_self = (isrc == target_to_source[itgt])
                    """ if self.exclude_self else ""]
            + loopy_insns + kernel_exprs
            + ["    end"]
            + ["""
                    result[{i}, itgt] = result[{i}, itgt] + \
                        knl_{i}_scaling * simul_reduce(sum, isrc, pair_result_{i}) \
                        {{id_prefix=write_csr}}
                """.format(i=iknl)
                for iknl in range(len(self.kernels))]
            + ["""
                    end
                end
                end
            """],
            arguments,
            assumptions="ntgt_boxes>=1",
            name=self.name,
            silenced_warnings="write_race(write_csr*)",
            fixed_parameters=dict(
                dim=self.dim,
                nstrengths=self.strength_count,
                nkernels=len(self.kernels)),
            lang_version=MOST_RECENT_LANGUAGE_VERSION)

        loopy_knl = lp.add_dtypes(loopy_knl,
            dict(nsources=np.int32, ntargets=np.int32))

        loopy_knl = lp.tag_inames(loopy_knl, "idim*:unr")
        loopy_knl = lp.tag_array_axes(loopy_knl, "targets", "sep,C")
        loopy_knl = lp.tag_array_axes(loopy_knl, "sources", "sep,C")

        for knl in self.kernels:
            loopy_knl = knl.prepare_loopy_kernel(loopy_knl)

        return loopy_knl

    def get_optimized_kernel(self):
        # FIXME
        knl = self.get_kernel()

        import pyopencl as cl
        dev = self.context.devices[0]
        if dev.type & cl.device_type.CPU:
            knl = lp.split_iname(knl, "itgt_box", 4, outer_tag="g.0")
        else:
            knl = lp.split_iname(knl, "itgt_box", 4, outer_tag="g.0")

        return knl

    def __call__(self, queue, **kwargs):
        knl = self.get_cached_optimized_kernel()

        return knl(queue, **kwargs)

# }}}

# vim: foldmethod=marker
