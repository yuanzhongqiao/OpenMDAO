"""Define the ExplicitComponent class."""

import numpy as np

from openmdao.jacobians.dictionary_jacobian import DictionaryJacobian
from openmdao.core.component import Component
from openmdao.vectors.vector import _full_slice, _CompMatVecWrapper
from openmdao.utils.class_util import overrides_method
from openmdao.recorders.recording_iteration_stack import Recording
from openmdao.core.constants import INT_DTYPE

_inst_functs = ['compute_jacvec_product']


class ExplicitComponent(Component):
    """
    Class to inherit from when all output variables are explicit.

    Parameters
    ----------
    **kwargs : dict of keyword arguments
        Keyword arguments that will be mapped into the Component options.

    Attributes
    ----------
    _inst_functs : dict
        Dictionary of names mapped to bound methods.
    _has_compute_partials : bool
        If True, the instance overrides compute_partials.
    _last_input_hash : str
        Keeps track of changes to input vector. Used if matrix_free_caching is True.
    _last_dinput_hash : str
        Keeps track of changes to dinput vector. Used if matrix_free_caching is True.
    _last_doutput_hash : str
        Keeps track of changes to doutput vector. Used if matrix_free_caching is True.
    _last_mode : str
        Keeps track of changes to derivative direction. Used if matrix_free_caching is True.
    _linop_cache : ndarray or None
        Dict wrapper for the last computed full JVP or VJP. Used if matrix_free_caching is True.
    """

    def __init__(self, **kwargs):
        """
        Store some bound methods so we can detect runtime overrides.
        """
        super().__init__(**kwargs)

        self._inst_functs = {name: getattr(self, name, None) for name in _inst_functs}
        self._has_compute_partials = overrides_method('compute_partials', self, ExplicitComponent)
        self.options.undeclare('assembled_jac_type')
        self._last_dinput_hash = ''
        self._last_input_hash = ''
        self._last_doutput_hash = ''
        self._last_mode = ''
        self._linop_cache = None

    def _configure(self):
        """
        Configure this system to assign children settings and detect if matrix_free.
        """
        new_jacvec_prod = getattr(self, 'compute_jacvec_product', None)

        self.matrix_free = (
            overrides_method('compute_jacvec_product', self, ExplicitComponent) or
            (new_jacvec_prod is not None and
             new_jacvec_prod != self._inst_functs['compute_jacvec_product']))

    def _get_partials_varlists(self):
        """
        Get lists of 'of' and 'wrt' variables that form the partial jacobian.

        Returns
        -------
        tuple(list, list)
            'of' and 'wrt' variable lists.
        """
        of = list(self._var_rel_names['output'])
        wrt = list(self._var_rel_names['input'])

        # filter out any discrete inputs or outputs
        if self._discrete_outputs:
            of = [n for n in of if n not in self._discrete_outputs]
        if self._discrete_inputs:
            wrt = [n for n in wrt if n not in self._discrete_inputs]

        return of, wrt

    def _jac_wrt_iter(self, wrt_matches=None):
        """
        Iterate over (name, start, end, vec, slice, dist_sizes) for each column var in the jacobian.

        Parameters
        ----------
        wrt_matches : set or None
            Only include row vars that are contained in this set.  This will determine what
            the actual offsets are, i.e. the offsets will be into a reduced jacobian
            containing only the matching columns.

        Yields
        ------
        str
            Name of 'wrt' variable.
        int
            Starting index.
        int
            Ending index.
        Vector
            The _inputs vector.
        slice
            A full slice.
        ndarray or None
            Distributed sizes if var is distributed else None
        """
        start = end = 0
        local_ins = self._var_abs2meta['input']
        toidx = self._var_allprocs_abs2idx
        sizes = self._var_sizes['input']
        total = self.pathname == ''
        szname = 'global_size' if total else 'size'
        for wrt, meta in self._var_abs2meta['input'].items():
            if wrt_matches is None or wrt in wrt_matches:
                end += meta[szname]
                vec = self._inputs if wrt in local_ins else None
                dist_sizes = sizes[:, toidx[wrt]] if meta['distributed'] else None
                yield wrt, start, end, vec, _full_slice, dist_sizes
                start = end

    def _setup_partials(self):
        """
        Call setup_partials in components.
        """
        super()._setup_partials()

        abs2prom_out = self._var_abs2prom['output']

        # Note: These declare calls are outside of setup_partials so that users do not have to
        # call the super version of setup_partials. This is still in the final setup.
        for out_abs, meta in self._var_abs2meta['output'].items():

            # No need to FD outputs wrt other outputs
            abs_key = (out_abs, out_abs)
            if abs_key in self._subjacs_info:
                if 'method' in self._subjacs_info[abs_key]:
                    del self._subjacs_info[abs_key]['method']

            size = meta['size']

            # ExplicitComponent jacobians have -1 on the diagonal.
            if size > 0 and not self.matrix_free:
                arange = np.arange(size, dtype=INT_DTYPE)

                self._subjacs_info[abs_key] = {
                    'rows': arange,
                    'cols': arange,
                    'shape': (size, size),
                    'val': np.full(size, -1.),
                    'dependent': True,
                }

    def _setup_jacobians(self, recurse=True):
        """
        Set and populate jacobian.

        Parameters
        ----------
        recurse : bool
            If True, setup jacobians in all descendants. (ignored)
        """
        if self._has_approx and self._use_derivatives:
            self._set_approx_partials_meta()

    def add_output(self, name, val=1.0, shape=None, units=None, res_units=None, desc='',
                   lower=None, upper=None, ref=1.0, ref0=0.0, res_ref=None, tags=None,
                   shape_by_conn=False, copy_shape=None, distributed=None):
        """
        Add an output variable to the component.

        For ExplicitComponent, res_ref defaults to the value in res unless otherwise specified.

        Parameters
        ----------
        name : str
            Name of the variable in this component's namespace.
        val : float or list or tuple or ndarray
            The initial value of the variable being added in user-defined units. Default is 1.0.
        shape : int or tuple or list or None
            Shape of this variable, only required if val is not an array.
            Default is None.
        units : str or None
            Units in which the output variables will be provided to the component during execution.
            Default is None, which means it has no units.
        res_units : str or None
            Units in which the residuals of this output will be given to the user when requested.
            Default is None, which means it has no units.
        desc : str
            Description of the variable.
        lower : float or list or tuple or ndarray or None
            Lower bound(s) in user-defined units. It can be (1) a float, (2) an array_like
            consistent with the shape arg (if given), or (3) an array_like matching the shape of
            val, if val is array_like. A value of None means this output has no lower bound.
            Default is None.
        upper : float or list or tuple or ndarray or None
            Upper bound(s) in user-defined units. It can be (1) a float, (2) an array_like
            consistent with the shape arg (if given), or (3) an array_like matching the shape of
            val, if val is array_like. A value of None means this output has no upper bound.
            Default is None.
        ref : float
            Scaling parameter. The value in the user-defined units of this output variable when
            the scaled value is 1. Default is 1.
        ref0 : float
            Scaling parameter. The value in the user-defined units of this output variable when
            the scaled value is 0. Default is 0.
        res_ref : float
            Scaling parameter. The value in the user-defined res_units of this output's residual
            when the scaled value is 1. Default is None, which means residual scaling matches
            output scaling.
        tags : str or list of strs
            User defined tags that can be used to filter what gets listed when calling
            list_inputs and list_outputs and also when listing results from case recorders.
        shape_by_conn : bool
            If True, shape this output to match its connected input(s).
        copy_shape : str or None
            If a str, that str is the name of a variable. Shape this output to match that of
            the named variable.
        distributed : bool
            If True, this variable is a distributed variable, so it can have different sizes/values
            across MPI processes.

        Returns
        -------
        dict
            Metadata for added variable.
        """
        if res_ref is None:
            res_ref = ref

        return super().add_output(name, val=val, shape=shape, units=units,
                                  res_units=res_units, desc=desc,
                                  lower=lower, upper=upper,
                                  ref=ref, ref0=ref0, res_ref=res_ref,
                                  tags=tags, shape_by_conn=shape_by_conn,
                                  copy_shape=copy_shape, distributed=distributed)

    def _approx_subjac_keys_iter(self):
        is_output = self._outputs._contains_abs
        for abs_key, meta in self._subjacs_info.items():
            if 'method' in meta and not is_output(abs_key[1]):
                method = meta['method']
                if (method is not None and method in self._approx_schemes):
                    yield abs_key

    def _compute_wrapper(self):
        """
        Call compute based on the value of the "run_root_only" option.
        """
        with self._call_user_function('compute'):
            args = [self._inputs, self._outputs]
            if self._discrete_inputs or self._discrete_outputs:
                args += [self._discrete_inputs, self._discrete_outputs]

            if self._run_root_only():
                if self.comm.rank == 0:
                    self.compute(*args)
                    self.comm.bcast([self._outputs.asarray(), self._discrete_outputs], root=0)
                else:
                    new_outs, new_disc_outs = self.comm.bcast(None, root=0)
                    self._outputs.set_val(new_outs)
                    if new_disc_outs:
                        for name, val in new_disc_outs.items():
                            self._discrete_outputs[name] = val
            else:
                self.compute(*args)

    def _apply_nonlinear(self):
        """
        Compute residuals. The model is assumed to be in a scaled state.
        """
        outputs = self._outputs
        residuals = self._residuals
        with self._unscaled_context(outputs=[outputs], residuals=[residuals]):
            residuals.set_vec(outputs)

            # Sign of the residual is minus the sign of the output vector.
            residuals *= -1.0
            self._compute_wrapper()
            residuals += outputs
            outputs -= residuals

        self.iter_count_apply += 1

    def _solve_nonlinear(self):
        """
        Compute outputs. The model is assumed to be in a scaled state.
        """
        with Recording(self.pathname + '._solve_nonlinear', self.iter_count, self):
            with self._unscaled_context(outputs=[self._outputs], residuals=[self._residuals]):
                self._residuals.set_val(0.0)
                self._compute_wrapper()

            # Iteration counter is incremented in the Recording context manager at exit.

    def _compute_jacvec_product_wrapper(self, inputs, d_inputs, d_resids, mode,
                                        discrete_inputs=None):
        """
        Call compute_jacvec_product based on the value of the "run_root_only" option.

        Parameters
        ----------
        inputs : Vector
            Nonlinear input vector.
        d_inputs : Vector
            Linear input vector.
        d_resids : Vector
            Linear residual vector.
        mode : str
            Indicates direction of derivative computation, either 'fwd' or 'rev'.
        discrete_inputs : dict or None
            Mapping of variable name to discrete value.
        """
        if self._run_root_only():
            if self.comm.rank == 0:
                if discrete_inputs:
                    self.compute_jacvec_product(inputs, d_inputs, d_resids, mode, discrete_inputs)
                else:
                    self.compute_jacvec_product(inputs, d_inputs, d_resids, mode)
                if mode == 'fwd':
                    self.comm.bcast(d_resids.asarray(), root=0)
                else:  # rev
                    self.comm.bcast(d_inputs.asarray(), root=0)
            else:
                new_vals = self.comm.bcast(None, root=0)
                if mode == 'fwd':
                    d_resids.set_val(new_vals)
                else:  # rev
                    d_inputs.set_val(new_vals)
        else:
            if discrete_inputs:
                self.compute_jacvec_product(inputs, d_inputs, d_resids, mode, discrete_inputs)
            else:
                self.compute_jacvec_product(inputs, d_inputs, d_resids, mode)

    def _cache_jvp(self, mode):
        if mode == 'rev':
            arr = self._vectors['input']['linear'].asarray()
            if self._linop_cache is None:
                self._linop_cache = arr.copy()
            else:
                self._linop_cache[:] = arr

            # print(self.pathname, '**************** saved linop cache', self._linop_cache)

    def _use_cached_jvp(self, mode):
        if mode == 'rev':
            self._vectors['input']['linear'].iadd(self._linop_cache)

            # print(self.pathname, "****************** after restoring cache, vec=", vec.asarray())

    def _apply_linear(self, jac, rel_systems, mode, scope_out=None, scope_in=None):
        """
        Compute jac-vec product. The model is assumed to be in a scaled state.

        Parameters
        ----------
        jac : Jacobian or None
            If None, use local jacobian, else use jac.
        rel_systems : set of str
            Set of names of relevant systems based on the current linear solve.
        mode : str
            'fwd' or 'rev'.
        scope_out : set or None
            Set of absolute output names in the scope of this mat-vec product.
            If None, all are in the scope.
        scope_in : set or None
            Set of absolute input names in the scope of this mat-vec product.
            If None, all are in the scope.
        """
        # print("Component", self.pathname, "_apply_linear")
        J = self._jacobian if jac is None else jac

        matfreecache = self.options['matrix_free_caching']

        d_inputs = self._vectors['input']['linear']
        d_resids = self._vectors['residual']['linear']
        changed = not matfreecache or self.seed_changed(self._inputs, d_inputs, d_resids, mode)
        # if changed:
        #     pass
        # else:
        #     print("USE CACHE")

        with self._matvec_context(scope_out, scope_in, mode) as vecs:
            d_inputs, d_outputs, d_residuals = vecs

            # Jacobian and vectors are all scaled, unitless
            J._apply(self, d_inputs, d_outputs, d_residuals, mode)

            if not self.matrix_free:
                # if we're not matrix free, we can skip the rest because
                # compute_jacvec_product does nothing.
                return

            # Jacobian and vectors are all unscaled, dimensional
            with self._unscaled_context(outputs=[self._outputs], residuals=[d_residuals]):

                # set appropriate vectors to read_only to help prevent user error
                if mode == 'fwd':
                    d_inputs.read_only = True
                    if matfreecache:
                        ins = _CompMatVecWrapper(self._inputs)
                        dins = _CompMatVecWrapper(d_inputs)
                    else:
                        ins = self._inputs
                        dins = d_inputs
                    dres = d_residuals
                else:  # rev
                    d_residuals.read_only = True
                    if matfreecache:
                        dres = _CompMatVecWrapper(d_residuals)
                    else:
                        dres = d_residuals
                    ins = self._inputs
                    dins = d_inputs

                try:
                    # handle identity subjacs (output_or_resid wrt itself)
                    if isinstance(J, DictionaryJacobian):
                        d_out_names = d_outputs._names

                        if d_out_names:
                            rflat = d_residuals._abs_get_val
                            oflat = d_outputs._abs_get_val

                            # 'val' in the code below is a reference to the part of the
                            # output or residual array corresponding to the variable 'v'
                            if mode == 'fwd':
                                for v in self._var_abs2meta['output']:
                                    if v in d_out_names and (v, v) not in self._subjacs_info:
                                        val = rflat(v)
                                        val -= oflat(v)
                            else:  # rev
                                for v in self._var_abs2meta['output']:
                                    if v in d_out_names and (v, v) not in self._subjacs_info:
                                        val = oflat(v)
                                        val -= rflat(v)

                    # print('dins:', dins._data.real, 'dres:', dres._data.real, 'douts:',
                    # self._vectors['output']['linear']._data.real)
                    if changed:
                        # We used to negate the residual here, and then re-negate after the hook
                        with self._call_user_function('compute_jacvec_product'):
                            self._compute_jacvec_product_wrapper(ins, dins, dres, mode,
                                                                 self._discrete_inputs)
                            if matfreecache:
                                self._cache_jvp(mode)
                    else:
                        # print("SKIPPING _compute_jacvec_product", self.pathname)
                        self._use_cached_jvp(mode)
                    # print('dins:', dins._data.real, 'dres:', dres._data.real, 'douts:',
                    # self._vectors['output']['linear']._data.real)
                finally:
                    d_inputs.read_only = d_residuals.read_only = False

    def _solve_linear(self, mode, rel_systems):
        """
        Apply inverse jac product. The model is assumed to be in a scaled state.

        Parameters
        ----------
        mode : str
            'fwd' or 'rev'.
        rel_systems : set of str
            Set of names of relevant systems based on the current linear solve.

        """
        # print("Component", self.pathname, "_solve_linear")
        d_outputs = self._vectors['output']['linear']
        d_residuals = self._vectors['residual']['linear']

        if mode == 'fwd':
            if self._has_resid_scaling:
                with self._unscaled_context(outputs=[d_outputs], residuals=[d_residuals]):
                    d_outputs.set_vec(d_residuals)
            else:
                d_outputs.set_vec(d_residuals)

            # ExplicitComponent jacobian defined with -1 on diagonal.
            d_outputs *= -1.0

        else:  # rev
            if self._has_resid_scaling:
                with self._unscaled_context(outputs=[d_outputs], residuals=[d_residuals]):
                    d_residuals.set_vec(d_outputs)
            else:
                d_residuals.set_vec(d_outputs)

            # ExplicitComponent jacobian defined with -1 on diagonal.
            d_residuals *= -1.0

    def _compute_partials_wrapper(self):
        """
        Call compute_partials based on the value of the "run_root_only" option.
        """
        with self._call_user_function('compute_partials'):
            args = [self._inputs, self._jacobian]
            if self._discrete_inputs:
                args += [self._discrete_inputs]

            if self._run_root_only():
                if self.comm.rank == 0:
                    self.compute_partials(*args)
                    self.comm.bcast(list(self._jacobian.items()), root=0)
                else:
                    for key, val in self.comm.bcast(None, root=0):
                        self._jacobian[key] = val
            else:
                self.compute_partials(*args)

    def _linearize(self, jac=None, sub_do_ln=False):
        """
        Compute jacobian / factorization. The model is assumed to be in a scaled state.

        Parameters
        ----------
        jac : Jacobian or None
            Ignored.
        sub_do_ln : bool
            Flag indicating if the children should call linearize on their linear solvers.
        """
        if not (self._has_compute_partials or self._approx_schemes):
            return

        self._check_first_linearize()

        with self._unscaled_context(outputs=[self._outputs], residuals=[self._residuals]):
            # Computing the approximation before the call to compute_partials allows users to
            # override FD'd values.
            for approximation in self._approx_schemes.values():
                approximation.compute_approximations(self, jac=self._jacobian)

            if self._has_compute_partials:
                # We used to negate the jacobian here, and then re-negate after the hook.
                self._compute_partials_wrapper()

    def compute(self, inputs, outputs, discrete_inputs=None, discrete_outputs=None):
        """
        Compute outputs given inputs. The model is assumed to be in an unscaled state.

        Parameters
        ----------
        inputs : Vector
            Unscaled, dimensional input variables read via inputs[key].
        outputs : Vector
            Unscaled, dimensional output variables read via outputs[key].
        discrete_inputs : dict or None
            If not None, dict containing discrete input values.
        discrete_outputs : dict or None
            If not None, dict containing discrete output values.
        """
        pass

    def compute_partials(self, inputs, partials, discrete_inputs=None):
        """
        Compute sub-jacobian parts. The model is assumed to be in an unscaled state.

        Parameters
        ----------
        inputs : Vector
            Unscaled, dimensional input variables read via inputs[key].
        partials : Jacobian
            Sub-jac components written to partials[output_name, input_name]..
        discrete_inputs : dict or None
            If not None, dict containing discrete input values.
        """
        pass

    def compute_jacvec_product(self, inputs, d_inputs, d_outputs, mode, discrete_inputs=None):
        r"""
        Compute jac-vector product. The model is assumed to be in an unscaled state.

        If mode is:
            'fwd': d_inputs \|-> d_outputs

            'rev': d_outputs \|-> d_inputs

        Parameters
        ----------
        inputs : Vector
            Unscaled, dimensional input variables read via inputs[key].
        d_inputs : Vector
            See inputs; product must be computed only if var_name in d_inputs.
        d_outputs : Vector
            See outputs; product must be computed only if var_name in d_outputs.
        mode : str
            Either 'fwd' or 'rev'.
        discrete_inputs : dict or None
            If not None, dict containing discrete input values.
        """
        pass

    def is_implicit(self, simple=False):
        """
        Return False, meaning this system is not implicit.

        Parameters
        ----------
        simple : bool
            Ignored by Components.

        Returns
        -------
        bool
            False.
        """
        return False

    def seed_changed(self, inputs, dinputs, doutputs, mode):
        """
        Return True if inputs/dinputs (fwd) or doutputs (rev) have changed since last JVP call.

        Parameters
        ----------
        inputs : Vector
            Nonlinear input vector.
        dinputs : Vector
            Linear input vector.
        doutputs : Vector
            Linear residuals vector.
        mode : str
            Direction of derivative computation ('fwd' or 'rev').

        Returns
        -------
        bool
            True if inputs/dinputs (fwd) or doutputs (rev) have changed since last call to
            compute_jacvec_product.
        """
        # return True

        inhash = inputs.get_hash()
        changed = inhash != self._last_input_hash

        if mode == 'fwd':
            dinhash = dinputs.get_hash()
            changed |= dinhash != self._last_dinput_hash
            self._last_dinput_hash = dinhash
        else:  # rev
            douthash = doutputs.get_hash()
            changed |= douthash != self._last_doutput_hash
            self._last_doutput_hash = douthash

        changed |= mode != self._last_mode

        self._last_input_hash = inhash
        self._last_mode = mode

        # if changed:
        #     print("SEED CHANGE")

        return changed
