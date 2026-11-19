"""Unit tests for board/activity_model.py (stdlib unittest).  Run from the repo root:  python3 -I -m unittest discover -s tests -p "test_*.py" -v"""
import copy, json, math, os, random, sys, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "board"))
import activity_model as am

FS = 49.87
def tvec(n=997): return [i / FS for i in range(n)]
def pulse(f, t, amp=1.0):                                    # pulse-like wave: fundamental + 2nd + 3rd harmonic
    return [amp * (math.sin(2 * math.pi * f * x) + 0.5 * math.sin(4 * math.pi * f * x + 0.4) + 0.25 * math.sin(6 * math.pi * f * x + 1.0)) for x in t]
A = am.DEFAULT_CONFIG["activity"]

class TestSpectrum(unittest.TestCase):
    def test_A1_finds_sinusoid(self):                         # T-A1
        t = tvec(); x = [math.sin(2 * math.pi * 0.9 * v) for v in t]
        fr = [0.5 + 0.05 * i for i in range(71)]; P = am.spectrum(x, FS, fr)
        self.assertAlmostEqual(fr[P.index(max(P))], 0.9, delta=0.05)

class TestRhythm(unittest.TestCase):
    def test_A2_pulse_only(self):                             # T-A2a
        t = tvec(); r = am.ppg_rhythm(pulse(1.4, t), FS, 83.0, 85.0, A)
        self.assertGreater(r["p_card"], 0.8); self.assertLess(r["mot_ratio"], 0.3)
    def test_A2_pulse_plus_stride(self):                      # T-A2b
        t = tvec(); x = [a + b for a, b in zip(pulse(1.4, t), [2.0 * math.sin(2 * math.pi * 0.85 * v) for v in t])]
        r = am.ppg_rhythm(x, FS, 83.0, 85.0, A)
        self.assertAlmostEqual(r["mot_f"], 0.85, delta=0.05); self.assertGreater(r["mot_ratio"], 1.5); self.assertGreaterEqual(r["mot_sharp"], A["min_sharp"])
    def test_stride_with_second_harmonic(self):
        t = tvec(); x = [a + 2 * math.sin(2 * math.pi * 0.85 * v) + 1.0 * math.sin(2 * math.pi * 1.7 * v + 0.3) for a, v in zip(pulse(1.1, t), t)]
        r = am.ppg_rhythm(x, FS, 65.0, 67.0, A); self.assertTrue(r["harm2"])
    def test_stride_inside_cardiac_band_is_ignored(self):     # HR 51 bpm: the pulse sits at 0.85 Hz, so a 0.85 Hz peak is the pulse
        t = tvec(); r = am.ppg_rhythm(pulse(0.85, t), FS, 50.0, 52.0, A)
        self.assertLess(r["mot_ratio"], 0.3)
    def test_ecg_rhythm_agrees(self):
        t = tvec(); x = [math.sin(2 * math.pi * 0.85 * v) + 0.05 * math.sin(2 * math.pi * 3.1 * v) for v in t]
        e = am.ecg_rhythm(x, FS, 83.0, 85.0, 0.85, A); self.assertTrue(e["agree"])
        self.assertFalse(am.ecg_rhythm(x, FS, 83.0, 85.0, 1.15, A)["agree"])
    def test_ecg_rhythm_noise_false_agreement_rate(self):
        # Measured on white noise: about 12% of windows reach sharpness >= 5 within +-0.1 Hz of a given cadence by chance.
        # It is only supporting evidence (weight 0.6) and is only evaluated when the PPG cadence is already strong.
        hits = 0
        for seed in range(100):
            rnd = random.Random(seed); x = [rnd.gauss(0, 1) for _ in range(997)]
            hits += am.ecg_rhythm(x, FS, 83.0, 85.0, 0.85, A)["agree"]
        self.assertLess(hits, 25)

class TestDecide(unittest.TestCase):
    """T-A3: truth table for the label rules."""
    walk_ev = {"cadence": 1.0, "purity": 0.3, "harm2": 1.0, "ecg_agree": 1.0, "persistence": 1.0, "hr_elev": 1.0}
    still_ev = {"cadence": -1.0, "purity": -1.0, "hr_elev": 0.0}
    def d(self, ev, raw, basis="ecg+ppg"): return am.decide(ev, raw, basis, A)
    def test_walking(self):
        self.assertEqual(self.d(self.walk_ev, {"p_card": 0.05, "hr_elev": 35})[0], "walking")
    def test_walking_needs_persistence(self):                 # F5
        ev = dict(self.walk_ev); ev["persistence"] = 0.0; lab, _, _, note = self.d(ev, {"p_card": 0.05, "hr_elev": 35})
        self.assertEqual((lab, note), ("uncertain", "waiting"))
    def test_walking_needs_positive_cadence(self):
        ev = dict(self.walk_ev); ev["cadence"] = 0.0; self.assertEqual(self.d(ev, {"p_card": 0.05, "hr_elev": 35})[0], "uncertain")
    def test_walking_needs_both_signals(self):
        for b in ("ecg_only", "ppg_only", "none"):
            lab, score, conf, _ = self.d(self.walk_ev, {"p_card": 0.05, "hr_elev": 35}, b)
            self.assertEqual(lab, "uncertain"); self.assertLessEqual(conf, 0.3); self.assertIsNone(score)
    def test_sitting(self):
        self.assertEqual(self.d(self.still_ev, {"p_card": 0.9, "hr_elev": 3})[0], "sitting")
    def test_sitting_hr_guard(self):                          # F4: clean pulse but HR 58 bpm above floor is not 'sitting'
        ev = dict(self.still_ev); ev["hr_elev"] = 1.0
        self.assertEqual(self.d(ev, {"p_card": 0.9, "hr_elev": 58})[0], "uncertain")
        self.assertEqual(self.d(self.still_ev, {"p_card": 0.9, "hr_elev": 15.0})[0], "uncertain")   # boundary: < 15 required
    def test_sitting_needs_purity(self):
        self.assertEqual(self.d(self.still_ev, {"p_card": 0.55, "hr_elev": 3})[0], "uncertain")
    def test_sitting_needs_negative_cadence(self):
        ev = dict(self.still_ev); ev["cadence"] = 0.0; ev["purity"] = -1.0; ev["hr_elev"] = -0.5
        self.assertEqual(self.d(ev, {"p_card": 0.9, "hr_elev": 3})[0], "uncertain")
    def test_low_confidence_is_uncertain(self):
        ev = {"cadence": -0.2, "purity": -0.2}; self.assertEqual(self.d(ev, {"p_card": 0.9, "hr_elev": 3})[0], "uncertain")
    def test_hr_alone_never_walks(self):                      # heart rate alone must not produce 'walking'
        self.assertEqual(self.d({"cadence": 0.0, "purity": 0.0, "hr_elev": 1.0}, {"p_card": 0.4, "hr_elev": 50})[0], "uncertain")
        self.assertEqual(self.d({"hr_elev": 1.0}, {"p_card": None, "hr_elev": 50})[0], "uncertain")

class TestConfig(unittest.TestCase):
    def test_A4_rejects_bad(self):                            # T-A4
        def bad(mut):
            c = copy.deepcopy(am.DEFAULT_CONFIG); mut(c)
            with self.assertRaises(ValueError): am.validate_config(c)
        bad(lambda c: c["region_model"]["features"].append("nonsense"))
        bad(lambda c: c["region_model"]["regions"][0].update(w=[1, 2]))
        bad(lambda c: c["region_model"].update(alpha_load=0))
        bad(lambda c: c["region_model"].update(horizon_windows=-1))
        bad(lambda c: c["activity"]["w"].update(bogus=1.0))
        bad(lambda c: c["strain"]["w"].update(bogus=1.0))
    def test_A4_partial_override_merges(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "c.json")
            with open(p, "w") as fh: json.dump({"region_model": {"gain": 2.5}, "activity": {"w": {"cadence": 2.0}}}, fh)
            c = am.load_config(p)
        self.assertEqual(c["region_model"]["gain"], 2.5); self.assertEqual(len(c["region_model"]["regions"]), 7)
        self.assertEqual(c["activity"]["w"]["cadence"], 2.0); self.assertEqual(c["activity"]["w"]["purity"], A["w"]["purity"])
    def test_shipped_config_is_default_plus_v2c_tuning(self):      # docs/TUNING_v2c.md: only these keys differ from the code defaults
        TUNED = {"activity": {"purity_low": 0.35, "purity_still": [0.35, 0.5], "w": {"purity": 3.0}, "walk_score": 0.8, "ratio_still": 0.6,
                              "ratio_walk": [0.7, 1.5], "min_sharp": 4.0, "cad_persist_hz": 0.25},
                 "strain": {"deadband_sd": 1.5, "w": {"perf": 0.15, "pi_trend": 0.1}}}
        shipped = am.load_config(os.path.join(ROOT, "board", "twin_config.json")); shipped.pop("tuned", None)
        expect = am.validate_config(am._merge(copy.deepcopy(am.DEFAULT_CONFIG), copy.deepcopy(TUNED)))
        self.assertEqual(shipped, expect)
    def test_region_names_and_order(self):
        self.assertEqual([r["name"] for r in am.DEFAULT_CONFIG["region_model"]["regions"]],
                         ["heel", "sole/arch", "forefoot", "dorsum/midfoot", "medial", "lateral", "ankle/rearfoot"])
        self.assertTrue(all("hypothesis" in r["why"] for r in am.DEFAULT_CONFIG["region_model"]["regions"]))

def rank(v): return sorted(range(len(v)), key=lambda i: -v[i])
class TestAreas(unittest.TestCase):
    def proj(self, **kw):
        ap = am.AreaProjector(am.load_config()); a = dict(walk=0.0, strain=0.3, rise=0.0, x_perf=0.0, x_pat=0.0, cap=1.5); a.update(kw)
        for _ in range(3): r, _t = ap.update(a["walk"], a["strain"], a["rise"], a["x_perf"], a["x_pat"], a["cap"])
        return r
    def test_A5_single_feature_changes_rank_order(self):      # T-A5: the ranking is not fixed by the biases alone
        base = rank(self.proj())
        for name, kw in (("walk", dict(walk=1.0)), ("perf_drop", dict(x_perf=1.5)), ("pat_shift", dict(x_pat=1.5)), ("strain_rise", dict(rise=2.0))):
            r = rank(self.proj(**kw)); moved = sum(1 for i in range(7) if base.index(i) != r.index(i))
            self.assertGreaterEqual(moved, 2, "%s moved only %d regions" % (name, moved))
        self.assertNotEqual(rank(self.proj(walk=1.0)), rank(self.proj(x_perf=1.5)))
    def test_projection_rises_with_horizon(self):
        c0 = am.load_config(); c1 = am.load_config(); c1["region_model"]["horizon_windows"] = 0
        a0, a1 = am.AreaProjector(c0), am.AreaProjector(c1)
        for _ in range(3): r0, _t = a0.update(1.0, 0.6, 0.5, 0, 0, 1.5); r1, _t = a1.update(1.0, 0.6, 0.5, 0, 0, 1.5)
        self.assertTrue(all(x >= y for x, y in zip(r0, r1))); self.assertTrue(any(x > y for x, y in zip(r0, r1)))

def base_ctx(**kw):
    c = dict(ecg_reliable=True, hr=72.0, hr_elev=0.0, rmssd=40.0, ihr_lo=70.0, ihr_hi=74.0, ppg_dc=1e6, contact=True, fb=FS, mot_sig=None,
             ecg_lf=None, ppg_usable=True, ppg_reason="ok", u=[0.2, 0.2, 0.0], t_mid=10.0, ln_pi=math.log(1.0))
    c.update(kw); return c

class TestStrain(unittest.TestCase):
    def strain(self, hist=(), walk=None, **kw):
        se = am.StrainEstimator(am.load_config()); return se.update(base_ctx(**kw), list(hist), walk)[0]
    def test_A7_baseline_is_low(self):                        # T-A7
        self.assertLess(self.strain(), 0.4)
        self.assertLess(self.strain(hr=75.0, hr_elev=3.0, u=[1.0, 1.0, 0.0], walk=0.15), 0.4)           # inside the 1-SD deadband
        hist = [(10.0 * i, 72.0 + (i % 2), math.log(1.0), 0.0) for i in range(5)]
        self.assertLess(self.strain(hist=hist, t_mid=50.0, walk=0.1), 0.4)
    def test_A7_monotonic(self):
        def mono(vals, f):
            out = [f(v) for v in vals]; self.assertEqual(out, sorted(out), out)
        mono([0, 5, 10, 20, 40, 80], lambda v: self.strain(hr=72.0 + v, hr_elev=float(v)))
        mono([0, 1, 1.5, 2, 3, 4], lambda v: self.strain(u=[v, 0.0, 0.0]))
        mono([0, 1, 1.5, 2, 3, 4], lambda v: self.strain(u=[0.0, v, 0.0]))
        mono([0, 0.2, 0.5, 1.0], lambda v: self.strain(walk=v))
        mono([0, 5, 10, 20, 40], lambda v: self.strain(hist=[(10.0 * i, 72.0 + v * i / 6.0, 0.0, 0.0) for i in range(5)], t_mid=50.0, hr=72.0 + v * 50 / 60.0))
    def test_deadband(self):
        se = am.StrainEstimator(am.load_config()); self.assertEqual(se.twin_inputs([0.9, 1.0, 0]), (0.0, 0.0)); self.assertEqual(se.twin_inputs([9.0, 9.0, 0]), (1.5, 1.5))
        self.assertEqual(se.twin_inputs(None), (None, None))
    def test_trend_needs_four_points(self):
        se = am.StrainEstimator(am.load_config())
        _, _, _, parts3, _ = se.update(base_ctx(t_mid=30.0), [(10.0 * i, 70.0 + 5 * i, 0.0, 0.0) for i in range(2)], None)
        _, _, _, parts4, _ = se.update(base_ctx(t_mid=30.0), [(10.0 * i, 70.0 + 5 * i, 0.0, 0.0) for i in range(3)], None)
        self.assertNotIn("hr_trend", parts3); self.assertIn("hr_trend", parts4)
    def test_no_ecg_gives_none(self):
        s, conf, why, parts, _ = am.StrainEstimator(am.load_config()).update(base_ctx(ecg_reliable=False, hr=None, hr_elev=None), [], None)
        self.assertIsNone(s); self.assertTrue(why.startswith("insufficient data"))
    def test_wording_limits(self):
        s, conf, why, parts, _ = am.StrainEstimator(am.load_config()).update(base_ctx(hr=120.0, hr_elev=48.0), [], 0.9)
        self.assertLessEqual(len(why), 160); self.assertNotIn("muscle", why.lower())

class TestAnalytics(unittest.TestCase):
    def ctxs(self, n):
        rnd = random.Random(5); out = []
        for i in range(n):
            t = tvec(); sig = [a + rnd.gauss(0, 0.05) for a in pulse(1.2, t)]
            out.append(base_ctx(hr=72.0 + i, hr_elev=None, ihr_lo=70.0 + i, ihr_hi=74.0 + i, mot_sig=sig, t_mid=10.0 * i + 10, u=[0.5 + 0.1 * i, 0.4, 0.0]))
        return out
    def test_A6_state_round_trip(self):                       # T-A6
        cfg = am.load_config(); a = am.Analytics(cfg); cs = self.ctxs(7)
        for c in cs[:6]: a.update({}, dict(c))
        st = json.loads(json.dumps(a.dump())); b = am.Analytics(cfg, st)
        self.assertEqual(json.loads(json.dumps(b.dump())), st)
        a.hist = []; a.act.prev_mot_f = None                  # history and previous cadence are deliberately not persisted
        wa, wb = a.update({}, dict(cs[6])), b.update({}, dict(cs[6]))
        self.assertEqual(wa, wb)
    def test_A6_old_state_loads(self):
        cfg = am.load_config(); a = am.Analytics(cfg, None); self.assertEqual(a.act.hr_ref, 72.0)
        w = a.update({}, base_ctx()); self.assertIn("act", w)
        self.assertEqual(am.Analytics(cfg, {"hr_ref": "garbage", "load": [1, 2]}).area.load, [None] * 7)   # wrong length -> reset
    def test_F1_hr_ref_behaviour(self):
        cfg = am.load_config(); est = am.ActivityEstimator(cfg); self.assertEqual(est.hr_ref, 72.0)
        est.update(base_ctx(hr=60.0)); self.assertAlmostEqual(est.hr_ref, 72.0 + 0.3 * (60 - 72), places=6)     # falls fast
        r0 = est.hr_ref; est.update(base_ctx(hr=100.0)); self.assertEqual(est.hr_ref, r0)                         # does not rise on non-sitting
    def test_F1_hr_ref_rises_slowly_on_clean_sitting(self):
        cfg = am.load_config(); est = am.ActivityEstimator(cfg); t = tvec(); sig = pulse(1.3, t)
        r = est.update(base_ctx(hr=78.0, ihr_lo=76.0, ihr_hi=80.0, mot_sig=sig)); self.assertEqual(r["act"], "sitting")
        self.assertAlmostEqual(est.hr_ref, 72.0 + 0.02 * 6.0, places=6)
    def test_poor_contact_and_missing_ecg(self):
        cfg = am.load_config(); est = am.ActivityEstimator(cfg)
        r = est.update(base_ctx(contact=False)); self.assertEqual((r["act"], r["act_basis"]), ("uncertain", "ecg_only")); self.assertIn("poor contact", r["act_reason"])
        r = est.update(base_ctx(ecg_reliable=False, hr=None)); self.assertEqual((r["act"], r["act_basis"], r["act_reason"]), ("uncertain", "ppg_only", "ECG beats unreliable"))
        r = est.update(base_ctx(ecg_reliable=False, hr=None, contact=False)); self.assertEqual((r["act_basis"], r["act_reason"]), ("none", "insufficient data"))
    def test_area_states(self):
        a = am.Analytics(am.load_config())
        w = a.update({}, base_ctx(ecg_reliable=False, hr=None, contact=False, ppg_usable=False, u=None))
        self.assertEqual((w["area_q"], w["area"], w["strain"]), ("none", None, None)); self.assertTrue(w["area_reason"].startswith("insufficient data"))
        self.assertEqual(a.area.load, [None] * 7)                                       # load is NOT updated without a reliable ECG
        w = a.update({}, base_ctx(contact=False, ppg_usable=False, u=None)); self.assertEqual(w["area_q"], "partial"); self.assertIn("poor contact", w["area_reason"])
        self.assertEqual(len(w["area"]), 7); self.assertEqual(w["area_h_s"], 120.0)
        w = a.update({}, base_ctx()); self.assertEqual(w["area_q"], "full")
    def test_v2c_no_stride_peak_does_not_crash(self):         # live bug: mot_f/mot_ratio None -> round(None) TypeError
        a = am.Analytics(am.load_config())
        orig = a.act.update
        def with_none(c):
            r = orig(c); r["diag"].update(mot_f=None, mot_ratio=None); return r   # what ppg_rhythm yields with no cardiac peak in the grid
        a.act.update = with_none
        w = a.update({}, base_ctx())
        self.assertIsNone(w["mot_f"]); self.assertIsNone(w["mot_ratio"]); self.assertIn("act", w)
    def test_reason_length_and_wording(self):
        a = am.Analytics(am.load_config()); w = a.update({}, self.ctxs(1)[0])
        self.assertLessEqual(len(w["act_reason"]), 160); self.assertLessEqual(len(w["strain_why"]), 160)
        self.assertNotIn("tension", (w["act_reason"] + w["strain_why"]).lower())

class TestRestingFloor(unittest.TestCase):
    def test_force_rest_hr_ignores_saved_floor(self):
        c = am.load_config(); c["activity"].update(resting_hr_prior_bpm=88.0, force_rest_hr=True)
        self.assertEqual(am.ActivityEstimator(c, {"hr_ref": 60.0}).hr_ref, 88.0)
        c2 = am.load_config(); self.assertEqual(am.ActivityEstimator(c2, {"hr_ref": 60.0}).hr_ref, 60.0)      # default: saved floor is used
    def test_higher_floor_lowers_strain(self):
        lo = am.Analytics(am.load_config()); c = am.load_config(); c["activity"].update(resting_hr_prior_bpm=90.0, force_rest_hr=True); hi = am.Analytics(c)
        a, b = lo.update({}, base_ctx(hr=92.0, hr_elev=None, ihr_lo=90.0, ihr_hi=94.0)), hi.update({}, base_ctx(hr=92.0, hr_elev=None, ihr_lo=90.0, ihr_hi=94.0))
        self.assertGreater(a["strain"], b["strain"]); self.assertAlmostEqual(b["hr_elev"], 2.0, places=3)
    def test_calibration_learns_median_then_runs(self):
        c = am.load_config(); c["activity"]["calibrate_rest_s"] = 60.0; an = am.Analytics(c); need = an.act.cal_need; self.assertEqual(need, 5)
        for i, hr in enumerate([90.0, 88.0, 92.0, 89.0, 91.0]):
            w = an.update({}, base_ctx(hr=hr, hr_elev=None, t_mid=10.0 * i))
            if i < need - 1: self.assertTrue(w["calibrating"]); self.assertIsNone(w["strain"]); self.assertEqual(w["area_q"], "none"); self.assertIn("resting heart rate", w["act_reason"])
        self.assertEqual(an.act.hr_ref, 90.0); self.assertFalse(an.act.calibrating)
        w = an.update({}, base_ctx(hr=90.0, hr_elev=None, t_mid=60.0)); self.assertIsNotNone(w["strain"]); self.assertNotIn("calibrating", w)
    def test_calibration_skips_unreliable_ecg(self):
        c = am.load_config(); c["activity"]["calibrate_rest_s"] = 30.0; an = am.Analytics(c)
        an.update({}, base_ctx(ecg_reliable=False, hr=None, hr_elev=None)); self.assertEqual(len(an.act.cal_hr), 0)
    def test_validation(self):
        c = am.load_config(); c["activity"]["resting_hr_prior_bpm"] = 10.0
        with self.assertRaises(ValueError): am.validate_config(c)

class TestBoardHelpers(unittest.TestCase):
    def test_qrs_blanked_lf_keeps_wander_removes_qrs(self):
        import cardiotwin_board as cb
        fa = 498.7; n = int(30 * fa); R = [int(0.8 * fa * k) + 100 for k in range(int(30 / 0.8) - 1)]
        x = [800.0 * math.sin(2 * math.pi * 0.85 * i / fa) for i in range(n)]
        for r in R:
            for d in range(-6, 7): x[r + d] += 40000.0 * math.exp(-(d / 3.0) ** 2)
        y = cb.qrs_blanked_lf(x, R, fa, 0, n); fs = fa / 10
        P = am.spectrum(y, fs, [0.55 + 0.05 * i for i in range(16)]); f = 0.55 + 0.05 * P.index(max(P))
        self.assertAlmostEqual(f, 0.85, delta=0.05); self.assertLess(max(abs(v) for v in y), 4000.0)   # QRS (4e4) is gone
        self.assertEqual(len(y), len(range(0, n, 10)))

if __name__ == "__main__": unittest.main()
