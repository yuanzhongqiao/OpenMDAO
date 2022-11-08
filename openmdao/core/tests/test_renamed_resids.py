import unittest

import numpy as np

import openmdao.api as om
from openmdao.utils.assert_utils import assert_near_equal, assert_check_partials


class MyCompApprox(om.ImplicitComponent):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.nonlinear_solver = om.NewtonSolver(solve_subsystems=False)
        self.linear_solver = om.DirectSolver()

    def setup(self):
        self.add_input('mm', np.ones(1))
        self.add_output('Re', np.ones((1, 1)))
        self.add_output('temp', np.ones((1, 1)))
        self.add_residual('res', shape=(2,))

    def setup_partials(self):
        self.declare_partials('res', ['*'], method='fd')

    def apply_nonlinear(self, inputs, outputs, residuals):
        mm = inputs['mm'][0]
        T = 389.97
        cf = 0.01
        temp = outputs['temp'][0][0]
        RE = 1.479301E9 * .0260239151 * (T / 1.8 + 110.4) / (T / 1.8) ** 2
        comb = 4.593153E-6 * 0.8 * (T + 198.72) / (RE * mm * T ** 1.5)
        temp_ratio = 1.0 + 0.035 * mm * mm + 0.45 * (temp / T - 1.0)
        CFL = cf / (1.0 + 3.59 * np.sqrt(cf) * temp_ratio)
        residuals['res'][0] = outputs['Re'] - RE * mm
        residuals['res'][1] = (1.0 / (1.0 +  comb * temp ** 3 / CFL) + temp) * 0.5 - temp


class MyCompAnalytic(MyCompApprox):
    def setup_partials(self):
        self.declare_partials('res', ['*'])

    def linearize(self, inputs, outputs, partials):
        mm = inputs['mm'][0]
        temp = outputs['temp'][0][0]
        T = 389.97
        cf = 0.01
        RE = 1.479301E9 * .0260239151 * ((T / 1.8) + 110.4) / (T / 1.8) ** 2
        comb = 4.593153E-6 * 0.8 * (T + 198.72) / (RE * mm * T ** 1.5)
        dcomb_dmm = -4.593153E-6 * 0.8 * (T + 198.72) / (RE * mm * mm * T ** 1.5)
        temp_ratio = 1.0 +  0.035 * mm * mm + 0.45 * temp / T - 1.0
        CFL = cf / (1.0 + 3.59 * np.sqrt(cf) * temp_ratio)
        dCFL_dwtr = - cf * 3.59 * np.sqrt(cf) / (1.0 + 3.59 * np.sqrt(cf) * temp_ratio) ** 2
        den = 1.0 + comb * temp ** 3 / CFL
        dreswt_dcomb = -0.5 * temp ** 3 / (CFL * den ** 2)
        dreswt_dCFL = 0.5 * comb * temp ** 3 / (CFL * den) ** 2
        dreswt_dwt = -0.5 - 1.5 * comb * temp ** 2 / (CFL * den ** 2)

        partials['res', 'mm'] = np.array([[-RE],
                                          [dreswt_dcomb * dcomb_dmm +  dreswt_dCFL * dCFL_dwtr * 0.07 * mm]])
        partials['res', 'temp'] = np.array([[0.],
                                            [dreswt_dCFL * dCFL_dwtr * 0.45 / T + dreswt_dwt]])
        partials['res', 'Re'] = np.array([[1.],[0.]])


class MyCompApprox2(MyCompApprox):
    def setup(self):
        self.add_input('mm', np.ones(1))
        self.add_output('Re', np.ones((1, 1)))
        self.add_output('temp', np.ones((1, 1)))
        self.add_residual('res1', shape=(1,))
        self.add_residual('res2', shape=(1,))

    def setup_partials(self):
        self.declare_partials('res1', ['Re', 'mm'], method='fd')
        self.declare_partials('res2', ['temp', 'mm'], method='fd')

    def apply_nonlinear(self, inputs, outputs, residuals):
        mm = inputs['mm'][0]
        T = 389.97
        cf = 0.01
        temp = outputs['temp'][0][0]
        RE = 1.479301E9 * .0260239151 * (T / 1.8 + 110.4) / (T / 1.8) ** 2
        comb = 4.593153E-6 * 0.8 * (T + 198.72) / (RE * mm * T ** 1.5)
        temp_ratio = 1.0 + 0.035 * mm * mm + 0.45 * (temp / T - 1.0)
        CFL = cf / (1.0 + 3.59 * np.sqrt(cf) * temp_ratio)
        residuals['res1'] = outputs['Re'] - RE * mm
        residuals['res2'] = (1.0 / (1.0 +  comb * temp ** 3 / CFL) + temp) * 0.5 - temp


class MyCompBad1(MyCompApprox):
    def setup(self):
        self.add_input('mm', np.ones(1))
        self.add_output('Re', np.ones((1, 1)))
        self.add_output('temp', np.ones((1, 1)))
        self.add_residual('res1', shape=(1,))
        self.add_residual('res2', shape=(2,))

    def setup_partials(self):
        self.declare_partials('res1', ['Re', 'mm'], method='fd')
        self.declare_partials('res2', ['temp', 'mm'], method='fd')

    def apply_nonlinear(self, inputs, outputs, residuals):
        pass


class MyCompBad2(MyCompApprox):
    def setup(self):
        self.add_input('mm', np.ones(1))
        self.add_output('Re', np.ones((1, 1)))
        self.add_output('temp', np.ones((1, 1)))
        self.add_residual('res1', shape=(1,), ref=np.ones((1,2)))
        self.add_residual('res2', shape=(1,))

    def setup_partials(self):
        self.declare_partials('res1', ['Re', 'mm'], method='fd')
        self.declare_partials('res2', ['temp', 'mm'], method='fd')

    def apply_nonlinear(self, inputs, outputs, residuals):
        pass


class MyCompBad3(MyCompApprox):
    def setup(self):
        self.add_input('mm', np.ones(1))
        self.add_output('Re', np.ones((1, 1)))
        self.add_output('temp', np.ones((1, 1)))
        self.add_residual('res1', shape=(1,), units="foobar/baz")
        self.add_residual('res2', shape=(1,))

    def setup_partials(self):
        self.declare_partials('res1', ['Re', 'mm'], method='fd')
        self.declare_partials('res2', ['temp', 'mm'], method='fd')

    def apply_nonlinear(self, inputs, outputs, residuals):
        pass


class MyCompAnalytic2(MyCompApprox2):
    def setup_partials(self):
        self.declare_partials('res1', ['Re', 'mm'])
        self.declare_partials('res2', ['temp', 'mm'])

    def linearize(self, inputs, outputs, partials):
        mm = inputs['mm'][0]
        temp = outputs['temp'][0][0]
        T = 389.97
        cf = 0.01
        RE = 1.479301E9 * .0260239151 * ((T / 1.8) + 110.4) / (T / 1.8) ** 2
        comb = 4.593153E-6 * 0.8 * (T + 198.72) / (RE * mm * T ** 1.5)
        dcomb_dmm = -4.593153E-6 * 0.8 * (T + 198.72) / (RE * mm * mm * T ** 1.5)
        temp_ratio = 1.0 +  0.035 * mm * mm + 0.45 * temp / T - 1.0
        CFL = cf / (1.0 + 3.59 * np.sqrt(cf) * temp_ratio)
        dCFL_dwtr = - cf * 3.59 * np.sqrt(cf) / (1.0 + 3.59 * np.sqrt(cf) * temp_ratio) ** 2
        den = 1.0 + comb * temp ** 3 / CFL
        dreswt_dcomb = -0.5 * temp ** 3 / (CFL * den ** 2)
        dreswt_dCFL = 0.5 * comb * temp ** 3 / (CFL * den) ** 2
        dreswt_dwt = -0.5 - 1.5 * comb * temp ** 2 / (CFL * den ** 2)
        partials['res1', 'Re'] = 1.0
        partials['res1', 'mm'] = -RE
        partials['res2', 'mm'] = dreswt_dcomb * dcomb_dmm +  dreswt_dCFL * dCFL_dwtr * 0.07 * mm
        partials['res2', 'temp'] = (dreswt_dCFL * dCFL_dwtr * 0.45 / T + dreswt_dwt)


class ResidNamingTestCase(unittest.TestCase):
    def _build_model(self, comp_class):
        prob = om.Problem()
        model = prob.model
        model.add_subsystem('MyComp', comp_class(), promotes=['*'])

        model.add_objective('Re')
        model.add_design_var('mm')

        prob.setup(force_alloc_complex=True)
        prob.set_solver_print(level=0)

        prob.set_val("mm", val=0.2)

        prob.run_model()

        return prob

    def test_approx(self):
        prob = self._build_model(MyCompApprox)
        assert_check_partials(prob.check_partials(method='cs', out_stream=None), atol=1e-5)

        totals = prob.check_totals(method='cs', out_stream=None)
        for val in totals.values():
            assert_near_equal(val['rel error'][0], 0.0, 1e-10)

    def test_approx2(self):
        prob = self._build_model(MyCompApprox2)
        assert_check_partials(prob.check_partials(method='cs', out_stream=None), atol=1e-5)

        totals = prob.check_totals(method='cs', out_stream=None)
        for val in totals.values():
            assert_near_equal(val['rel error'][0], 0.0, 1e-10)

    def test_size_mismatch(self):
        with self.assertRaises(Exception) as cm:
            prob = self._build_model(MyCompBad1)

        self.assertEqual(cm.exception.args[0], "'MyComp' <class MyCompBad1>: The number of residuals (3) doesn't match number of outputs (2).  If any residuals are added using 'add_residuals', their total size must match the total size of the outputs.")

    def test_ref_shape_mismatch(self):
        with self.assertRaises(Exception) as cm:
            prob = self._build_model(MyCompBad2)

        self.assertEqual(cm.exception.args[0], "'MyComp' <class MyCompBad2>: When adding residual 'res1', expected shape (1,) but got shape (1, 2) for argument 'ref'.")

    def test_bad_unit(self):
        with self.assertRaises(Exception) as cm:
            prob = self._build_model(MyCompBad3)

        self.assertEqual(cm.exception.args[0], "'MyComp' <class MyCompBad3>: The units 'foobar/baz' are invalid.")

    def test_analytic(self):
        prob = self._build_model(MyCompAnalytic)
        assert_check_partials(prob.check_partials(method='cs', out_stream=None))

        totals = prob.check_totals(method='cs', out_stream=None)
        for val in totals.values():
            assert_near_equal(val['rel error'][0], 0.0, 1e-12)

    def test_analytic2(self):
        prob = self._build_model(MyCompAnalytic2)
        assert_check_partials(prob.check_partials(method='cs', out_stream=None))

        totals = prob.check_totals(method='cs', out_stream=None)
        for val in totals.values():
            assert_near_equal(val['rel error'][0], 0.0, 1e-12)


if __name__ == '__main__':
    unittest.main()
