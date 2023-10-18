"""Define the PETSc Transfer class."""
import numpy as np
import networkx as nx
from openmdao.utils.mpi import check_mpi_env
from openmdao.utils.general_utils import common_subpath
from openmdao.core.constants import INT_DTYPE

use_mpi = check_mpi_env()
_empty_idx_array = np.array([], dtype=INT_DTYPE)


if use_mpi is False:
    PETScTransfer = None
else:
    try:
        import petsc4py
        from petsc4py import PETSc
    except ImportError:
        PETSc = None
        if use_mpi is True:
            raise ImportError("Importing petsc4py failed and OPENMDAO_USE_MPI is true.")

    from petsc4py import PETSc
    from collections import defaultdict

    from openmdao.vectors.default_transfer import DefaultTransfer, _fill, _setup_index_views
    from openmdao.utils.array_utils import shape_to_len

    class PETScTransfer(DefaultTransfer):
        """
        PETSc Transfer implementation for running in parallel.

        Parameters
        ----------
        in_vec : <Vector>
            Pointer to the input vector.
        out_vec : <Vector>
            Pointer to the output vector.
        in_inds : int ndarray
            Input indices for the transfer.
        out_inds : int ndarray
            Output indices for the transfer.
        comm : MPI.Comm or <FakeComm>
            Communicator of the system that owns this transfer.

        Attributes
        ----------
        _scatter : method
            Method that performs a PETSc scatter.
        """

        def __init__(self, in_vec, out_vec, in_inds, out_inds, comm):
            """
            Initialize all attributes.
            """
            super().__init__(in_vec, out_vec, in_inds, out_inds)
            in_indexset = PETSc.IS().createGeneral(self._in_inds, comm=comm)
            out_indexset = PETSc.IS().createGeneral(self._out_inds, comm=comm)

            self._scatter = PETSc.Scatter().create(out_vec._petsc, out_indexset, in_vec._petsc,
                                                   in_indexset).scatter

        @staticmethod
        def _setup_transfers(group, desvars, responses):
            """
            Compute all transfers that are owned by our parent group.

            Parameters
            ----------
            group : <Group>
                Parent group.
            desvars : dict
                Dictionary of all design variable metadata. Keyed by absolute source name or alias.
            responses : dict
                Dictionary of all response variable metadata. Keyed by absolute source name or
                alias.
            """
            rev = group._mode != 'fwd'

            for subsys in group._subgroups_myproc:
                subsys._setup_transfers(desvars, responses)

            abs2meta_in = group._var_abs2meta['input']
            abs2meta_out = group._var_abs2meta['output']
            allprocs_abs2meta_out = group._var_allprocs_abs2meta['output']
            myrank = group.comm.rank

            transfers = group._transfers = {}
            vectors = group._vectors
            offsets = group._get_var_offsets()
            mypathlen = len(group.pathname) + 1 if group.pathname else 0

            # Initialize empty lists for the transfer indices
            xfer_in = []
            xfer_out = []
            fwd_xfer_in = defaultdict(list)
            fwd_xfer_out = defaultdict(list)
            if rev:
                has_rev_par_coloring = any([m['parallel_deriv_color'] is not None
                                            for m in responses.values()])
                rev_xfer_in = defaultdict(list)
                rev_xfer_out = defaultdict(list)

                # xfers that are only active when parallel coloring is not
                rev_xfer_in_nocolor = defaultdict(list)
                rev_xfer_out_nocolor = defaultdict(list)

            allprocs_abs2idx = group._var_allprocs_abs2idx
            sizes_in = group._var_sizes['input']
            sizes_out = group._var_sizes['output']
            offsets_in = offsets['input']
            offsets_out = offsets['output']

            def is_dup(name, io):
                # return if given var is duplicated and number of procs where var doesn't exist
                if group._var_allprocs_abs2meta[io][name]['distributed']:
                    return False, 0, True  # distributed vars are never dups
                nz = np.count_nonzero(group._var_sizes[io][:, allprocs_abs2idx[name]])
                return nz > 1, group._var_sizes[io].shape[0] - nz, False

            def get_nonzero_ranks(name, io):
                return np.nonzero(group._var_sizes[io][:, allprocs_abs2idx[name]])[0]

            # for an FD group, we use the relevance graph to determine which inputs on the
            # boundary of the group are upstream of distributed variables within the group so
            # that we can perform any necessary allreduce operations on the outputs that
            # are connected to those inputs.
            if rev and group._owns_approx_jac and group._has_distrib_vars and group.pathname != '':
                model = group._problem_meta['model_ref']()
                relgraph = model._relevance_graph
                group_path = group.pathname + '.'

                inner_resps = []
                outer_dvs = []
                inner_dists = set()
                for n, data in relgraph.nodes(data=True):
                    inside = n.startswith(group_path)
                    if inside:
                        if 'isresponse' in data and data['isresponse']:
                            inner_resps.append(n)
                        if 'dist' in data and data['dist']:
                            inner_dists.add(n)
                    else:  # not inside
                        if 'isdv' in data and data['isdv']:
                            outer_dvs.append(n)

                if inner_resps and outer_dvs and inner_dists:
                    # inp_boundary_set is the set of input variables that are connected to sources
                    # outside of this group. (all group inputs minus group inputs that are connected
                    # internal to the group)
                    inp_boundary_set = set(group._var_allprocs_abs2meta['input'])
                    inp_boundary_set = inp_boundary_set.difference(group._conn_global_abs_in2out)

                    # inp_dep_dist is the set of group boundary inputs that are upstream of
                    # distributed variables and between an external design var and an internal
                    # response.
                    inp_dep_dist = set()

                    relevant = group._relevant
                    for resp in inner_resps:
                        for dv in outer_dvs:
                            rel = relevant[resp][dv][0]
                            if inner_dists.intersection(rel['input']) or \
                               inner_dists.intersection(rel['output']):
                                # dist var is in between the dv and response
                                if resp in inner_dists:
                                    inp_dep_dist.update(inp_boundary_set.intersection(rel['input']))

                    # look in model for the connections to the group boundary inputs from outside
                    for inp in inp_dep_dist:
                        src = model._conn_global_abs_in2out[inp]
                        gname = common_subpath((src, inp))
                        owning_group = model._get_subsystem(gname)
                        owning_group._fd_subgroup_inputs.add(inp)

            total_fwd = total_rev = total_rev_nocolor = 0

            # Loop through all connections owned by this system
            for abs_in, abs_out in group._conn_abs_in2out.items():
                # Only continue if the input exists on this processor
                if abs_in in abs2meta_in:
                    # Get meta
                    meta_in = abs2meta_in[abs_in]
                    meta_out = allprocs_abs2meta_out[abs_out]

                    idx_in = allprocs_abs2idx[abs_in]
                    idx_out = allprocs_abs2idx[abs_out]

                    local_out = abs_out in abs2meta_out

                    # Read in and process src_indices
                    src_indices = meta_in['src_indices']
                    if src_indices is None:
                        owner = group._owning_rank[abs_out]
                        # if the input is larger than the output on a single proc, we have
                        # to just loop over the procs in the same way we do when src_indices
                        # is defined.
                        if meta_in['size'] > sizes_out[owner, idx_out]:
                            src_indices = np.arange(meta_in['size'], dtype=INT_DTYPE)
                    else:
                        src_indices = src_indices.shaped_array()

                    on_iprocs = []

                    # 1. Compute the output indices
                    # NOTE: src_indices are relative to a single, possibly distributed variable,
                    # while the output_inds that we compute are relative to the full distributed
                    # array that contains all local variables from each rank stacked in rank order.
                    if src_indices is None:
                        if meta_out['distributed']:
                            # input in this case is non-distributed (else src_indices would be
                            # defined by now).  dist output to non-distributed input conns w/o
                            # src_indices are not allowed.
                            raise RuntimeError(f"{group.msginfo}: Can't connect distributed output "
                                               f"'{abs_out}' to non-distributed input '{abs_in}' "
                                               "without declaring src_indices.",
                                               ident=(abs_out, abs_in))
                        else:
                            rank = myrank if local_out else owner
                            offset = offsets_out[rank, idx_out]
                            output_inds = range(offset, offset + meta_in['size'])
                    else:
                        output_inds = np.empty(src_indices.size, INT_DTYPE)
                        start = end = 0
                        for iproc in range(group.comm.size):
                            end += sizes_out[iproc, idx_out]
                            if start == end:
                                continue

                            # The part of src on iproc
                            on_iproc = np.logical_and(start <= src_indices, src_indices < end)

                            if np.any(on_iproc):
                                # This converts from iproc-then-ivar to ivar-then-iproc ordering
                                # Subtract off part of this variable from previous procs
                                # Then add all variables on previous procs
                                # Then all previous variables on this proc
                                # - np.sum(out_sizes[:iproc, idx_out])
                                # + np.sum(out_sizes[:iproc, :])
                                # + np.sum(out_sizes[iproc, :idx_out])
                                # + inds
                                offset = offsets_out[iproc, idx_out] - start
                                output_inds[on_iproc] = src_indices[on_iproc] + offset
                                on_iprocs.append(iproc)

                            start = end

                    # 2. Compute the input indices
                    input_inds = range(offsets_in[myrank, idx_in],
                                       offsets_in[myrank, idx_in] + sizes_in[myrank, idx_in])

                    total_fwd += len(input_inds)

                    # Now the indices are ready - input_inds, output_inds
                    sub_in = abs_in[mypathlen:].partition('.')[0]
                    fwd_xfer_in[sub_in].append(input_inds)
                    fwd_xfer_out[sub_in].append(output_inds)
                    if rev and group._owns_approx_jac:
                        pass  # no rev transfers needed for FD group
                    elif rev:
                        inp_is_dup, inp_missing, distrib_in = is_dup(abs_in, 'input')
                        out_is_dup, _, distrib_out = is_dup(abs_out, 'output')

                        iowninput = myrank == group._owning_rank[abs_in]
                        sub_out = abs_out[mypathlen:].partition('.')[0]

                        if inp_is_dup and (abs_out not in abs2meta_out or
                                           (distrib_out and not iowninput)):
                            rev_xfer_in[sub_out]
                            rev_xfer_out[sub_out]
                        elif out_is_dup and inp_is_dup and inp_missing > 0 and iowninput:
                            # if this proc owns the input and both the output and input have
                            # duplicates, then we send the owning input to each duplicated output
                            # that doesn't have a corresponding connected input on the same proc.
                            oidxlist = []
                            iidxlist = []
                            oidxlist_nc = []
                            iidxlist_nc = []
                            size = size_nc = 0
                            for rnk, osize, isize in zip(range(group.comm.size),
                                                         sizes_out[:, idx_out],
                                                         sizes_in[:, idx_in]):
                                if rnk == myrank:
                                    oidxlist.append(output_inds)
                                    iidxlist.append(input_inds)
                                elif osize > 0 and isize == 0:
                                    offset = offsets_out[rnk, idx_out]
                                    if src_indices is None:
                                        oarr = range(offset, offset + meta_in['size'])
                                    elif src_indices.size > 0:
                                        oarr = np.asarray(src_indices + offset, dtype=INT_DTYPE)
                                    else:
                                        continue

                                    if has_rev_par_coloring:
                                        oidxlist_nc.append(oarr)
                                        iidxlist_nc.append(input_inds)
                                        size_nc += len(input_inds)
                                    else:
                                        oidxlist.append(oarr)
                                        iidxlist.append(input_inds)
                                        size += len(input_inds)

                            if len(iidxlist) > 1:
                                input_inds = _merge(iidxlist, size)
                                output_inds = _merge(oidxlist, size)
                            else:
                                input_inds = iidxlist[0]
                                output_inds = oidxlist[0]

                            total_rev += len(input_inds)

                            rev_xfer_in[sub_out].append(input_inds)
                            rev_xfer_out[sub_out].append(output_inds)

                            if has_rev_par_coloring and iidxlist_nc:
                                # keep transfers separate that shouldn't happen when partial
                                # coloring is active
                                if len(iidxlist_nc) > 1:
                                    input_inds = _merge(iidxlist_nc, size_nc)
                                    output_inds = _merge(oidxlist_nc, size_nc)
                                else:
                                    input_inds = iidxlist_nc[0]
                                    output_inds = oidxlist_nc[0]

                                total_rev_nocolor += len(input_inds)

                                rev_xfer_in_nocolor[sub_out].append(input_inds)
                                rev_xfer_out_nocolor[sub_out].append(output_inds)

                        elif out_is_dup and (not inp_is_dup or inp_missing > 0) and (iowninput or
                                                                                     distrib_in):
                            oidxlist = []
                            iidxlist = []
                            oidxlist_nc = []
                            iidxlist_nc = []
                            size = size_nc = 0
                            for rnk in get_nonzero_ranks(abs_out, 'output'):
                                offset = offsets_out[rnk, idx_out]
                                if src_indices is None:
                                    oarr = range(offset, offset + meta_in['size'])
                                elif src_indices.size > 0:
                                    if (distrib_in and not distrib_out and len(on_iprocs) == 1 and
                                            on_iprocs[0] == rnk):
                                        offset -= np.sum(sizes_out[:rnk, idx_out])
                                    oarr = np.asarray(src_indices + offset, dtype=INT_DTYPE)
                                else:
                                    continue
                                if rnk == myrank or not has_rev_par_coloring:
                                    oidxlist.append(oarr)
                                    iidxlist.append(input_inds)
                                    size += len(input_inds)
                                else:
                                    oidxlist_nc.append(oarr)
                                    iidxlist_nc.append(input_inds)
                                    size_nc += len(input_inds)

                            if len(iidxlist) > 1:
                                input_inds = _merge(iidxlist, size)
                                output_inds = _merge(oidxlist, size)
                            elif len(iidxlist) == 1:
                                input_inds = iidxlist[0]
                                output_inds = oidxlist[0]
                            else:
                                input_inds = output_inds = _empty_idx_array

                            total_rev += len(input_inds)

                            rev_xfer_in[sub_out].append(input_inds)
                            rev_xfer_out[sub_out].append(output_inds)

                            if has_rev_par_coloring and iidxlist_nc:
                                if len(iidxlist_nc) > 1:
                                    input_inds = _merge(iidxlist_nc, size_nc)
                                    output_inds = _merge(oidxlist_nc, size_nc)
                                else:
                                    input_inds = iidxlist_nc[0]
                                    output_inds = oidxlist_nc[0]

                                total_rev_nocolor += len(input_inds)

                                rev_xfer_in_nocolor[sub_out].append(input_inds)
                                rev_xfer_out_nocolor[sub_out].append(output_inds)
                        else:
                            if (inp_is_dup and out_is_dup and src_indices is not None and
                                    src_indices.size > 0):
                                offset = offsets_out[myrank, idx_out]
                                output_inds = np.asarray(src_indices + offset, dtype=INT_DTYPE)

                            total_rev += len(input_inds)

                            rev_xfer_in[sub_out].append(input_inds)
                            rev_xfer_out[sub_out].append(output_inds)
                else:
                    # not a local input but still need entries in the transfer dicts to
                    # avoid hangs
                    sub_in = abs_in[mypathlen:].partition('.')[0]
                    fwd_xfer_in[sub_in]  # defaultdict will create an empty list there
                    fwd_xfer_out[sub_in]
                    if rev and not group._owns_approx_jac:
                        sub_out = abs_out[mypathlen:].partition('.')[0]
                        rev_xfer_in[sub_out]
                        rev_xfer_out[sub_out]
                        if has_rev_par_coloring:
                            rev_xfer_in_nocolor[sub_out]
                            rev_xfer_out_nocolor[sub_out]

            if fwd_xfer_in:
                xfer_in, xfer_out = _setup_index_views(total_fwd, fwd_xfer_in, fwd_xfer_out)

            out_vec = vectors['output']['nonlinear']

            xfer_all = PETScTransfer(vectors['input']['nonlinear'], out_vec,
                                     xfer_in, xfer_out, group.comm)

            transfers['fwd'] = xfwd = {}
            xfwd[None] = xfer_all

            for sname, inds in fwd_xfer_in.items():
                transfers['fwd'][sname] = PETScTransfer(
                    vectors['input']['nonlinear'], vectors['output']['nonlinear'],
                    inds, fwd_xfer_out[sname], group.comm)

            if rev and not group._owns_approx_jac:
                xfer_in, xfer_out = _setup_index_views(total_rev, rev_xfer_in, rev_xfer_out)

                xfer_all = PETScTransfer(vectors['input']['nonlinear'], out_vec,
                                         xfer_in, xfer_out, group.comm)

                transfers['rev'] = xrev = {}
                xrev[None] = xfer_all

                for sname, inds in rev_xfer_out.items():
                    transfers['rev'][sname] = PETScTransfer(
                        vectors['input']['nonlinear'], vectors['output']['nonlinear'],
                        rev_xfer_in[sname], inds, group.comm)

                if rev_xfer_in_nocolor:
                    xfer_in, xfer_out = _setup_index_views(total_rev_nocolor, rev_xfer_in_nocolor,
                                                           rev_xfer_out_nocolor)

                    xrev[(None, 'nocolor')] = PETScTransfer(vectors['input']['nonlinear'], out_vec,
                                                            xfer_in, xfer_out, group.comm)

                    for sname, inds in rev_xfer_out_nocolor.items():
                        transfers['rev'][(sname, 'nocolor')] = PETScTransfer(
                            vectors['input']['nonlinear'], vectors['output']['nonlinear'],
                            rev_xfer_in_nocolor[sname], inds, group.comm)

        def _transfer(self, in_vec, out_vec, mode='fwd'):
            """
            Perform transfer.

            Parameters
            ----------
            in_vec : <Vector>
                pointer to the input vector.
            out_vec : <Vector>
                pointer to the output vector.
            mode : str
                'fwd' or 'rev'.
            """
            flag = False
            if mode == 'rev':
                flag = True
                in_vec, out_vec = out_vec, in_vec

            in_petsc = in_vec._petsc
            out_petsc = out_vec._petsc

            # For Complex Step, need to disassemble real and imag parts, transfer them separately,
            # then reassemble them.
            if in_vec._under_complex_step and out_vec._alloc_complex:

                # Real
                in_petsc.array = in_vec._data.real
                out_petsc.array = out_vec._data.real
                self._scatter(out_petsc, in_petsc, addv=flag, mode=flag)

                # Imaginary
                in_petsc_imag = in_vec._imag_petsc
                out_petsc_imag = out_vec._imag_petsc
                in_petsc_imag.array = in_vec._data.imag
                out_petsc_imag.array = out_vec._data.imag
                self._scatter(out_petsc_imag, in_petsc_imag, addv=flag, mode=flag)

                in_vec._data[:] = in_petsc.array + in_petsc_imag.array * 1j

            else:

                # Anything that has been allocated complex requires an additional step because
                # the petsc vector does not directly reference the _data.

                if in_vec._alloc_complex:
                    in_petsc.array = in_vec._get_data()

                if out_vec._alloc_complex:
                    out_petsc.array = out_vec._get_data()

                self._scatter(out_petsc, in_petsc, addv=flag, mode=flag)

                if in_vec._alloc_complex:
                    data = in_vec._get_data()
                    data[:] = in_petsc.array


def _merge(inds_list, tot_size):
    """
    Convert a list of indices and/or ranges into an array.

    Parameters
    ----------
    inds_list : list of ranges or ndarrays
        List of indices.
    tot_size : int
        Total size of the indices in the list.

    Returns
    -------
    ndarray
        Array of indices.
    """
    if inds_list:
        arr = np.empty(tot_size, dtype=INT_DTYPE)
        start = end = 0
        for inds in inds_list:
            end += len(inds)
            arr[start:end] = inds
            start = end

        return arr

    return _empty_idx_array
