#!/usr/bin/env python3
"""Activity estimate, inferred strain index and per-area projected risk (Python standard library ONLY).

Runs on the AM62B inside cardiotwin_board.py, once per 20 s window (hop 10 s).  RESEARCH PROTOTYPE, NOT DIAGNOSTIC.

What this module is, honestly:
  * Activity (sitting / walking / uncertain) is a rule-based estimate from signal fluctuations: a steady non-cardiac
    rhythm in the PPG (candidate stride cadence, searched at 0.55-1.30 Hz outside the ECG-derived cardiac bands), whether
    the ECG shows the same rhythm, how much of the PPG power is the pulse itself, and how far heart rate is above the
    person's own resting floor.  "sitting" really means "sitting / still": standing still cannot be told apart.
    Thresholds are hand-set from physical reasoning.  NO walking recording exists, so accuracy is unmeasured.
    Heart rate alone never produces "walking".
  * Inferred strain (0..1) is "inferred strain, a model index of physiological load.  ECG/PPG do not measure muscle
    tension."  It combines HR above the resting floor, HRV drop, perfusion / pulse-timing shifts, trends and the walking
    estimate.
  * Area risk (7 foot regions) = sigmoid(bias_r + gain * projected_load_r).  The weights in twin_config.json are
    HAND-SET HYPOTHESES.  Nothing in the data supports differences between regions.
All numbers are unvalidated model projections, not measured pain locations and not clinical predictions.

Layout: pure functions at module level (spectrum, ppg_rhythm, ecg_rhythm, slope_per_min, decide) so they can be unit
tested; three small stateful classes (ActivityEstimator, StrainEstimator, AreaProjector); Analytics is the facade.

Tuning log (config only; every change after the first draft is recorded here):
  v2  F1-F8 from ARCHITECTURE_activity_strain_areas.md applied; stride band 0.55-1.30 Hz; hr scale 25 bpm.
  v2b resting-floor options: --rest-hr BPM (set the floor) and --calibrate-rest SEC (learn it from the first SEC seconds of reliable HR).
      Reason: on the first live run (sitting, HR 86-102 bpm) the 72 bpm default made strain 0.46-0.78 and blocked "sitting" (needs < 15 bpm above floor).
  v2a strain.hr_trend_scale 10 -> 20 bpm/min and strain.pi_trend_scale 0.15 -> 0.30 /min.  Reason: the first allowed trend (4 windows,
      30 s span) extrapolated to a per-minute slope saturated the trend terms on the three resting wrist recordings
      (ecg_ppg2 k3 strain 0.83, 0.8 of the load from hr_trend alone).  A walking-onset ramp (75->105 bpm in 60 s = 30 bpm/min)
      still gives x = 1.5.  Floating-point slack (1e-9) added to the 0.15 Hz persistence tolerance (0.80 vs 0.65 failed on 0.15000000000000002).
"""
import copy, json, math

FEATURES = ["walk", "walk_exposure", "strain", "strain_rise", "perf_drop", "pat_shift"]
ACT_TERMS = ["cadence", "purity", "harm2", "ecg_agree", "persistence", "hr_elev"]
STRAIN_TERMS = ["hr", "hrv", "perf", "pat", "hr_trend", "pi_trend", "walk"]

DEFAULT_CONFIG = {
    "version": 2,
    "activity": {
        "spec_hz": [0.5, 4.0], "freq_step_hz": 0.05,      # PPG spectrum grid
        "stride_band_hz": [0.55, 1.30],                   # search band for the stride rhythm (F3)
        "noise_band_hz": [0.55, 2.6],                     # bins used for the "sharpness" median
        "cardiac_tol_hz": 0.10, "cardiac_harmonics": 3,
        "min_ppg_dc": 3e5,                                # same contact gate as the board's quality gate
        "min_sharp": 5.0,                                 # peak / median of the other bins: a narrow (periodic) peak
        "ratio_walk": [0.5, 1.5],                         # non-cardiac / cardiac amplitude: evidence 0 at .5, 1 at 1.5
        "ratio_still": 0.3,                               # below this the pulse is clean (still-evidence)
        "purity_still": [0.5, 0.8], "purity_low": 0.3,    # fraction of PPG power at cardiac harmonics
        "hr_elev_bpm": [10.0, 30.0],                      # HR above personal floor: 0 evidence at 10, 1 at 30
        "resting_hr_prior_bpm": 72.0,                     # population default until the person's own floor is learned (F1)
        "force_rest_hr": False,                           # True: use resting_hr_prior_bpm even if a learned floor was saved (set by --rest-hr)
        "calibrate_rest_s": 0.0,                          # >0: learn the floor as the median HR of the first reliable windows spanning this many seconds (subject sits still; --calibrate-rest)
        "hr_ref_down": 0.3, "hr_ref_up": 0.02,            # resting-floor tracker rates (fast down, slow up)
        "hr_ref_min": 40.0, "hr_ref_up_purity": 0.8,      # floor never below 40; rises only on clean sitting windows
        "sit_max_hr_elev_bpm": 15.0,                      # F4: 'sitting' needs HR within 15 bpm of the floor
        "sit_min_purity": 0.6,
        "cad_persist_hz": 0.15,                           # F5: same cadence in two consecutive windows
        "ecg_agree_hz": 0.10, "ecg_min_sharp": 5.0,
        "w": {"cadence": 1.6, "purity": 0.8, "harm2": 0.3, "ecg_agree": 0.6, "persistence": 0.5, "hr_elev": 0.6},
        "walk_score": 0.70, "still_score": 0.30, "min_conf": 0.40,
    },
    "strain": {
        # load = sum(w_k * x_k), x_k >= 0 (only excursions in the straining direction); strain = 1 - exp(-load)
        "w": {"hr": 0.6, "hrv": 0.25, "perf": 0.3, "pat": 0.2, "hr_trend": 0.4, "pi_trend": 0.2, "walk": 0.5},
        "hr_scale_bpm": 25.0, "hr_trend_scale": 20.0, "pi_trend_scale": 0.30,   # per minute (see tuning log)
        "deadband_sd": 1.0, "twin_cap": 1.5,              # F2: 1-SD deadband on twin inputs, capped
        "trend_windows": 6, "trend_min_points": 4,
        "hrv_sd_floor": 0.25, "ema_alpha": 0.1,
    },
    "region_model": {
        "features": FEATURES,
        "hop_s": 10.0,                  # seconds per window hop (for the horizon text)
        "horizon_windows": 12,          # project this many windows ahead assuming the current drive persists (2 min)
        "alpha_load": 0.2,              # recent-exposure averaging rate per window
        "exposure_alpha": 0.1,          # walk_exposure EMA rate
        "rise_scale_per_min": 0.3,
        "gain": 1.6, "elevated_threshold": 0.5,
        "regions": [
            # bias is the resting logit; w = weights on FEATURES in order.  HAND-SET HYPOTHESES (see why).
            {"name": "heel", "bias": -2.4, "w": [1.2, 0.8, 0.5, 0.3, 0.3, 0.0],
             "why": "hypothesis: impact loading in gait; PPG site is adjacent (perfusion shift)"},
            {"name": "sole/arch", "bias": -2.6, "w": [0.6, 1.4, 0.6, 0.3, 0.0, 0.0],
             "why": "hypothesis: sustained loading, so it weights accumulated walking exposure"},
            {"name": "forefoot", "bias": -2.6, "w": [1.3, 0.4, 0.8, 0.4, 0.0, 0.0],
             "why": "hypothesis: push-off loading, responds to walking and overall strain"},
            {"name": "dorsum/midfoot", "bias": -2.8, "w": [0.3, 0.2, 0.5, 0.6, 0.6, 0.6],
             "why": "hypothesis: tendon/vascular, so rising strain, perfusion and timing shifts"},
            {"name": "medial", "bias": -2.7, "w": [0.7, 0.9, 0.4, 0.2, 0.0, 0.2],
             "why": "hypothesis: stability loading; not distinguishable from lateral by this sensor"},
            {"name": "lateral", "bias": -2.7, "w": [0.9, 0.6, 0.4, 0.4, 0.2, 0.2],
             "why": "hypothesis: stability loading; not distinguishable from medial by this sensor"},
            {"name": "ankle/rearfoot", "bias": -2.4, "w": [0.5, 0.5, 0.6, 0.5, 1.2, 0.8],
             "why": "hypothesis: nearest the Achilles PPG site, so perfusion and pulse-timing shifts weigh most"},
        ],
    },
}

# ----------------------------------------------------------------------------- config
def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict): _merge(base[k], v)
        else: base[k] = v
    return base

def validate_config(c):
    """Raise ValueError with a readable message for anything the model cannot run with."""
    rm = c["region_model"]; feats = rm["features"]
    for f in feats:
        if f not in FEATURES: raise ValueError("region_model.features: unknown feature %r (allowed: %s)" % (f, FEATURES))
    if len(set(feats)) != len(feats): raise ValueError("region_model.features has duplicates")
    if not rm["regions"]: raise ValueError("region_model.regions is empty")
    for r in rm["regions"]:
        if "name" not in r or "bias" not in r or "w" not in r: raise ValueError("each region needs name, bias and w: %r" % (r,))
        if len(r["w"]) != len(feats): raise ValueError("region %r has %d weights, expected %d" % (r.get("name"), len(r["w"]), len(feats)))
    if not (0 < rm["alpha_load"] <= 1): raise ValueError("region_model.alpha_load must be in (0, 1]")
    if not (0 < rm["exposure_alpha"] <= 1): raise ValueError("region_model.exposure_alpha must be in (0, 1]")
    if int(rm["horizon_windows"]) < 0: raise ValueError("region_model.horizon_windows must be >= 0")
    a = c["activity"]
    for k in a["w"]:
        if k not in ACT_TERMS: raise ValueError("activity.w: unknown term %r (allowed: %s)" % (k, ACT_TERMS))
    for k in c["strain"]["w"]:
        if k not in STRAIN_TERMS: raise ValueError("strain.w: unknown input %r (allowed: %s)" % (k, STRAIN_TERMS))
    if not (a["ratio_walk"][0] < a["ratio_walk"][1]): raise ValueError("activity.ratio_walk must be increasing")
    if not (a["stride_band_hz"][0] < a["stride_band_hz"][1]): raise ValueError("activity.stride_band_hz must be increasing")
    if not (0 < a["hr_ref_down"] <= 1 and 0 < a["hr_ref_up"] <= 1): raise ValueError("activity.hr_ref_down/up must be in (0, 1]")
    if not (30 <= a["resting_hr_prior_bpm"] <= 140): raise ValueError("activity.resting_hr_prior_bpm must be between 30 and 140 bpm")
    if a["calibrate_rest_s"] < 0: raise ValueError("activity.calibrate_rest_s must be >= 0")
    return c

def load_config(path=None):
    c = copy.deepcopy(DEFAULT_CONFIG)
    if path:
        with open(path, encoding="utf-8") as f: _merge(c, json.load(f))
    return validate_config(c)

def config_summary(c):
    """What the dashboard shows as the active mapping (sent once in the hello message)."""
    rm = c["region_model"]
    return {"features": rm["features"], "horizon_windows": rm["horizon_windows"], "hop_s": rm["hop_s"], "alpha_load": rm["alpha_load"],
            "gain": rm["gain"], "elevated_threshold": rm["elevated_threshold"],
            "regions": [{"name": r["name"], "bias": r["bias"], "w": r["w"], "why": r.get("why", "")} for r in rm["regions"]]}

# ----------------------------------------------------------------------------- pure helpers
def clamp(x, lo, hi): return lo if x < lo else hi if x > hi else x
def sigmoid(x): return 1 / (1 + math.exp(-x)) if x > -60 else 0.0
def _mean(a): return sum(a) / len(a)

def slope_per_min(ts, ys, min_points=3):
    """Least-squares slope of ys vs ts (seconds), returned per minute; None if fewer than min_points points."""
    n = len(ts)
    if n < max(3, min_points): return None
    tm, ym = _mean(ts), _mean(ys); sxx = sum((t - tm) ** 2 for t in ts)
    return None if sxx <= 0 else 60.0 * sum((t - tm) * (y - ym) for t, y in zip(ts, ys)) / sxx

def spectrum(x, fs, freqs):
    """Hann-windowed Goertzel power at each frequency (pure Python)."""
    n = len(x)
    if n < 8: return [0.0] * len(freqs)
    m = _mean(x); xw = [(v - m) * (0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1))) for i, v in enumerate(x)]
    out = []
    for f in freqs:
        c = 2 * math.cos(2 * math.pi * f / fs); s1 = s2 = 0.0
        for v in xw: s1, s2 = v + c * s1 - s2, s1
        out.append(max(0.0, s1 * s1 + s2 * s2 - c * s1 * s2))
    return out

def cardiac_bands(ihr_lo, ihr_hi, cfg):
    """Frequency bands (Hz) that the pulse and its harmonics can occupy, from the ECG heart-rate range (bpm)."""
    tol = cfg["cardiac_tol_hz"]
    return [(h * ihr_lo / 60.0 - tol, h * ihr_hi / 60.0 + tol) for h in range(1, cfg["cardiac_harmonics"] + 1)]

def _in_bands(f, bands): return any(a <= f <= b for a, b in bands)

def _grid(lo, hi, df): return [lo + i * df for i in range(int(round((hi - lo) / df)) + 1)]

def _median(a): s = sorted(a); return s[len(s) // 2] if s else 0.0

def ppg_rhythm(sig, fs, ihr_lo, ihr_hi, cfg):
    """Cardiac purity and the strongest non-cardiac rhythm of a high-passed PPG window.  `cfg` is the `activity` section.
    Returns dict(p_card, mot_f, mot_ratio, mot_sharp, harm2) or None when there is no signal power."""
    df = cfg["freq_step_hz"]; fr = _grid(cfg["spec_hz"][0], cfg["spec_hz"][1], df); nb = len(fr)
    P = spectrum(sig, fs, fr); tot = sum(P)
    if tot <= 0: return None
    bands = cardiac_bands(ihr_lo, ihr_hi, cfg); card = [_in_bands(f, bands) for f in fr]
    fund = [p for f, p in zip(fr, P) if bands[0][0] <= f <= bands[0][1]]
    cpk = max(fund) if fund else 0.0
    sb = cfg["stride_band_hz"]; nb_lo, nb_hi = cfg["noise_band_hz"]
    stride = [i for i, f in enumerate(fr) if sb[0] - 1e-9 <= f <= sb[1] + 1e-9 and not card[i]]
    out = {"p_card": sum(p for p, c in zip(P, card) if c) / tot, "mot_f": None, "mot_ratio": None, "mot_sharp": None, "harm2": False}
    cand = [i for i in stride if 0 < i < nb - 1 and P[i] >= P[i - 1] and P[i] >= P[i + 1]]   # local maxima: not band-edge leakage
    if cand and cpk > 0:
        i = max(cand, key=lambda k: P[k])
        med = _median([P[k] for k, f in enumerate(fr) if nb_lo - 1e-9 <= f <= nb_hi + 1e-9 and not card[k]])
        out.update(mot_f=fr[i], mot_ratio=math.sqrt(P[i] / cpk), mot_sharp=(P[i] / med) if med > 0 else 1e9)
        h = [k for k in range(1, nb - 1) if abs(fr[k] - 2 * fr[i]) <= 0.1 + 1e-9 and not card[k] and P[k] >= P[k - 1] and P[k] >= P[k + 1]]
        out["harm2"] = any(P[k] >= 0.2 * P[i] for k in h)        # F3: second harmonic of the stride, outside the cardiac bands
    elif cpk > 0: out.update(mot_ratio=0.0, mot_sharp=0.0)
    return out

def ecg_rhythm(sig, fs, ihr_lo, ihr_hi, mot_f, cfg):
    """Independent support: does the QRS-blanked, low-passed ECG show a steady rhythm at the PPG cadence mot_f?
    Returns dict(f, sharp, agree) or None."""
    df = cfg["freq_step_hz"]; sb = cfg["stride_band_hz"]; fr = _grid(sb[0], sb[1], df)
    P = spectrum(sig, fs, fr)
    if sum(P) <= 0: return None
    bands = cardiac_bands(ihr_lo, ihr_hi, cfg); ok = [i for i, f in enumerate(fr) if not _in_bands(f, bands)]
    cand = [i for i in ok if 0 < i < len(fr) - 1 and P[i] >= P[i - 1] and P[i] >= P[i + 1]]
    if not cand: return {"f": None, "sharp": 0.0, "agree": False}
    i = max(cand, key=lambda k: P[k]); med = _median([P[k] for k in ok])
    sharp = P[i] / med if med > 0 else 1e9
    return {"f": fr[i], "sharp": sharp, "agree": bool(mot_f is not None and abs(fr[i] - mot_f) <= cfg["ecg_agree_hz"] + 1e-9 and sharp >= cfg["ecg_min_sharp"])}

def decide(ev, raw, basis, a):
    """Deterministic label rules (section 5.1.5).  ev: evidence term -> value (missing = 0); raw: {'p_card', 'hr_elev'} (bpm).
    Returns (label, score, conf, note); note is 'waiting' (cadence seen but not yet confirmed), 'mixed' or None."""
    if basis != "ecg+ppg": return "uncertain", None, 0.0, None            # never a label without both signals
    tot = sum(a["w"][k] * v for k, v in ev.items() if k in a["w"])
    score = sigmoid(tot); conf = clamp(abs(tot) / 2.0, 0, 1)
    cad = ev.get("cadence", 0.0); pers = ev.get("persistence", 0.0)
    pc = raw.get("p_card"); he = raw.get("hr_elev")
    if cad > 0 and pers >= 1 and score >= a["walk_score"] and conf >= a["min_conf"]: return "walking", score, conf, None
    if (cad < 0 and pc is not None and pc >= a["sit_min_purity"] and he is not None and he < a["sit_max_hr_elev_bpm"]
            and score <= a["still_score"] and conf >= a["min_conf"]): return "sitting", score, conf, None
    if cad > 0 and pers < 1 and score >= a["walk_score"] and conf >= a["min_conf"]: return "uncertain", score, conf, "waiting"   # F5
    return "uncertain", score, conf, "mixed"

def _cut(s, n=160): return s if len(s) <= n else s[:n - 1].rstrip(" ;,") + "…"

# ----------------------------------------------------------------------------- stateful parts
class _Ema:
    def __init__(s, alpha, k_warm=3): s.a, s.kw, s.k, s.m, s.v, s.buf = alpha, k_warm, 0, 0.0, 0.0, []
    def z(s, x, floor):
        return None if s.k < s.kw else clamp((x - s.m) / max(math.sqrt(s.v), floor), -4.0, 4.0)
    def update(s, x):
        if s.k < s.kw:
            s.buf.append(x); s.k += 1
            if s.k == s.kw: s.m = _mean(s.buf); s.v = _mean([(b - s.m) ** 2 for b in s.buf])
            return
        d = x - s.m; s.m += s.a * d; s.v = (1 - s.a) * (s.v + s.a * d * d); s.k += 1
    def dump(s): return dict(k=s.k, m=s.m, v=s.v, buf=s.buf)
    def load(s, d): s.k, s.m, s.v, s.buf = d["k"], d["m"], d["v"], d["buf"]

class ActivityEstimator:
    """sitting / walking / uncertain from ECG + PPG fluctuations.  State: hr_ref (resting-HR floor) and the previous cadence."""
    def __init__(s, cfg, ext=None):
        s.a = cfg["activity"]; s.hr_ref = s.a["resting_hr_prior_bpm"]; s.prev_mot_f = None     # prev cadence is not persisted
        if ext and isinstance(ext.get("hr_ref"), (int, float)) and not s.a["force_rest_hr"]: s.hr_ref = float(ext["hr_ref"])
        # optional start-up calibration: median HR of the first reliable windows (the subject must sit still); not persisted
        s.cal_need = max(2, int(round(s.a["calibrate_rest_s"] / 10.0)) - 1) if s.a["calibrate_rest_s"] > 0 else 0
        s.cal_hr = []
    calibrating = property(lambda s: s.cal_need > 0 and len(s.cal_hr) < s.cal_need)
    def dump(s): return {"hr_ref": s.hr_ref}

    def update(s, c):
        a = s.a; hr = c.get("hr"); ecg_ok = bool(c.get("ecg_reliable")) and hr is not None
        contact = bool(c.get("contact"))
        res = {"act": "uncertain", "act_conf": 0.0, "act_score": None, "act_basis": "none", "act_reason": "", "diag": {}}
        if ecg_ok and s.calibrating:                                            # learn the personal resting floor first
            s.cal_hr.append(hr)
            if not s.calibrating: s.hr_ref = max(a["hr_ref_min"], _median(s.cal_hr))
            res.update(act_basis="ecg_only", act_reason="learning your resting heart rate: sit still (%d of %d windows)" % (min(len(s.cal_hr), s.cal_need), s.cal_need))
            res["diag"]["hr_ref"] = s.hr_ref; res["diag"]["calibrating"] = s.calibrating
            return res
        if hr is not None and ecg_ok and hr < s.hr_ref:                        # F1: the floor falls fast when HR is below it
            s.hr_ref = max(a["hr_ref_min"], s.hr_ref + a["hr_ref_down"] * (hr - s.hr_ref))
        he = (hr - s.hr_ref) if ecg_ok else None
        res["diag"]["hr_ref"] = s.hr_ref
        if he is not None: res["diag"]["hr_elev"] = he
        ev, parts, mot = {}, [], None
        if ecg_ok and contact and c.get("mot_sig") is not None:
            mot = ppg_rhythm(c["mot_sig"], c["fb"], c["ihr_lo"], c["ihr_hi"], a)
        if mot is None:
            s.prev_mot_f = None
            if ecg_ok and not contact: res.update(act_basis="ecg_only", act_reason="PPG poor contact: cannot separate sitting from walking")
            elif not ecg_ok and contact: res.update(act_basis="ppg_only", act_reason="ECG beats unreliable")
            elif ecg_ok: res.update(act_basis="ecg_only", act_reason="PPG has no usable signal: cannot separate sitting from walking")
            else: res["act_reason"] = "insufficient data"
            return res
        r, sharp, f, p = mot["mot_ratio"], mot["mot_sharp"], mot["mot_f"], mot["p_card"]
        strong = r is not None and sharp is not None and r > a["ratio_walk"][0] and sharp >= a["min_sharp"]
        if strong: ev["cadence"] = clamp((r - a["ratio_walk"][0]) / (a["ratio_walk"][1] - a["ratio_walk"][0]), 0, 1)
        elif r is not None and r < a["ratio_still"]: ev["cadence"] = -clamp((a["ratio_still"] - r) / a["ratio_still"], 0, 1)
        else: ev["cadence"] = 0.0
        lo, hi = a["purity_still"]
        if p >= lo: ev["purity"] = -clamp((p - lo) / (hi - lo), 0, 1)
        elif p < a["purity_low"]: ev["purity"] = 0.5 * clamp((a["purity_low"] - p) / a["purity_low"], 0, 1)
        else: ev["purity"] = 0.0
        agree = None
        if strong:
            if mot["harm2"]: ev["harm2"] = 1.0
            if c.get("ecg_lf") is not None:
                er = ecg_rhythm(c["ecg_lf"], c["fb"], c["ihr_lo"], c["ihr_hi"], f, a)
                agree = bool(er and er["agree"])
                if agree: ev["ecg_agree"] = 1.0
        persist = strong and s.prev_mot_f is not None and abs(f - s.prev_mot_f) <= a["cad_persist_hz"] + 1e-9
        if persist: ev["persistence"] = 1.0
        s.prev_mot_f = f if strong else None
        ev["hr_elev"] = clamp((he - a["hr_elev_bpm"][0]) / (a["hr_elev_bpm"][1] - a["hr_elev_bpm"][0]), -0.5, 1)
        label, score, conf, note = decide(ev, {"p_card": p, "hr_elev": he}, "ecg+ppg", a)
        # resting floor: rises slowly, and only on clean sitting windows (F1)
        if label == "sitting" and p >= a["hr_ref_up_purity"] and hr > s.hr_ref: s.hr_ref += a["hr_ref_up"] * (hr - s.hr_ref)
        # reason: decisive evidence first (F8)
        if label == "walking" or note == "waiting":
            parts.append("steady %.2f Hz rhythm %.1fx pulse" % (f, r))
            if agree: parts.append("also in ECG")
            if mot["harm2"]: parts.append("2nd harmonic present")
            parts.append("same cadence as previous window" if persist else "possible cadence, waiting for confirmation")
        elif label == "sitting":
            parts.append("clean pulse (%d%% of PPG power), no extra rhythm" % round(100 * p))
        else:
            parts.append("evidence mixed or weak" + ("" if not strong else "; rhythm %.2f Hz %.1fx pulse" % (f, r)))
            parts.append("%d%% of PPG power is the pulse" % round(100 * p))
        parts.append("HR %+d bpm vs resting floor" % round(he))
        res.update(act=label, act_conf=conf, act_score=score, act_basis="ecg+ppg", act_reason=_cut("; ".join(parts)))
        res["diag"].update(mot_f=f, mot_ratio=r, p_card=p, hr_ref=s.hr_ref)
        if agree is not None: res["diag"]["ecg_agree"] = agree
        return res

class StrainEstimator:
    """Inferred strain, a model index of physiological load.  ECG/PPG do not measure muscle tension."""
    NAMES = {"hr": "HR above resting floor", "hrv": "HRV drop", "perf": "perfusion drop", "pat": "pulse-timing shift",
             "hr_trend": "HR rising", "pi_trend": "PPG amplitude falling", "walk": "walking estimate"}
    def __init__(s, cfg, ext=None):
        s.sc = cfg["strain"]; s.hrv = _Ema(s.sc["ema_alpha"])
        if ext and isinstance(ext.get("hrv"), dict):
            try: s.hrv.load(ext["hrv"])
            except Exception: s.hrv = _Ema(s.sc["ema_alpha"])
    def dump(s): return {"hrv": s.hrv.dump()}

    def twin_inputs(s, u):
        """F2: 1-SD deadband, then scaled and capped.  Returns (x_pat, x_perf) or (None, None)."""
        if u is None: return None, None
        db, cap = s.sc["deadband_sd"], s.sc["twin_cap"]
        return min(cap, max(0.0, u[0] - db) / 2), min(cap, max(0.0, u[1] - db) / 2)

    def update(s, c, hist, walk):
        """Returns (strain, conf, why, parts, x).  `hist` is a list of (t_mid, hr, ln_pi, strain) of earlier windows."""
        sc = s.sc; w = sc["w"]; x = {}
        hr, he = c.get("hr"), c.get("hr_elev")
        if hr is None or he is None or not c.get("ecg_reliable"): return None, 0.0, "insufficient data: no reliable ECG heart rate", {}, {}
        x["hr"] = clamp(he / sc["hr_scale_bpm"], 0, 3)
        if c.get("rmssd") is not None and c["rmssd"] > 0:
            lr = math.log(c["rmssd"]); z = s.hrv.z(lr, sc["hrv_sd_floor"])
            if z is not None: x["hrv"] = clamp(-z, 0, 3) / 2
            s.hrv.update(lr)
        xp, xf = s.twin_inputs(c.get("u"))
        if xp is not None: x["pat"], x["perf"] = xp, xf
        n = sc["trend_windows"]; H = hist[-(n - 1):] if n > 1 else []
        pts = [(t, h) for t, h, _, _ in H if h is not None] + [(c["t_mid"], hr)]
        sl = slope_per_min([p[0] for p in pts], [p[1] for p in pts], sc["trend_min_points"])
        if sl is not None: x["hr_trend"] = clamp(sl / sc["hr_trend_scale"], 0, 2)
        if c.get("ln_pi") is not None:
            pp = [(t, l) for t, _, l, _ in H if l is not None] + [(c["t_mid"], c["ln_pi"])]
            sp = slope_per_min([p[0] for p in pp], [p[1] for p in pp], sc["trend_min_points"])
            if sp is not None: x["pi_trend"] = clamp(-sp / sc["pi_trend_scale"], 0, 2)
        if walk is not None: x["walk"] = walk
        parts = {k: w[k] * v for k, v in x.items() if k in w}
        load = sum(parts.values()); strain = 1 - math.exp(-load)
        conf = sum(w[k] for k in parts) / sum(w.values())
        top = sorted(((v, k) for k, v in parts.items() if v > 0.02), reverse=True)[:3]
        why = ", ".join(s.NAMES[k] for _, k in top) if top else "no inputs above personal baseline"
        return strain, conf, _cut(why), {k: round(v, 3) for k, v in parts.items()}, x

class AreaProjector:
    """Per-region projected index: if the current drive persists for H more windows, where does the region's load reach?"""
    def __init__(s, cfg, ext=None):
        s.rm = cfg["region_model"]; s.regions = s.rm["regions"]; s.walk_exp = 0.0; s.load = [None] * len(s.regions)
        if ext:
            try:
                s.walk_exp = float(ext.get("walk_exp", 0.0)); ld = ext.get("load")
                if ld and len(ld) == len(s.regions): s.load = list(ld)
            except Exception: s.walk_exp = 0.0; s.load = [None] * len(s.regions)
    def dump(s): return {"walk_exp": s.walk_exp, "load": s.load}

    def update(s, walk, strain, rise, x_perf, x_pat, cap):
        rm = s.rm; feats = rm["features"]
        s.walk_exp += rm["exposure_alpha"] * (walk - s.walk_exp)
        x = {"walk": walk, "walk_exposure": s.walk_exp, "strain": strain, "strain_rise": rise,
             "perf_drop": (x_perf or 0.0) / cap, "pat_shift": (x_pat or 0.0) / cap}
        a, H = rm["alpha_load"], int(rm["horizon_windows"]); g = 1 - (1 - a) ** H
        risks, tops = [], []
        for i, r in enumerate(s.regions):
            contrib = [(wk * x[f], f) for wk, f in zip(r["w"], feats)]
            drive = sum(v for v, _ in contrib)
            s.load[i] = drive if s.load[i] is None else s.load[i] + a * (drive - s.load[i])
            proj = s.load[i] + (drive - s.load[i]) * g          # EMA value after H more windows of the same drive
            risks.append(sigmoid(r["bias"] + rm["gain"] * proj))
            tops.append([f for v, f in sorted(contrib, reverse=True)[:2] if v > 0.02])
        return risks, tops

# ----------------------------------------------------------------------------- facade
class Analytics:
    """Per-window activity, strain and area projections.  `ext` is the dict persisted in the twin state file ('ext')."""
    def __init__(s, cfg, ext=None):
        s.cfg = cfg; s.act = ActivityEstimator(cfg, ext); s.strain = StrainEstimator(cfg, ext); s.area = AreaProjector(cfg, ext)
        s.hist = []                                              # (t_mid, hr, ln_pi, strain); not persisted
    def dump(s):
        d = {}
        for part in (s.act, s.strain, s.area): d.update(part.dump())
        return d

    def update(s, w, c):
        """Adds act*/strain*/area* keys to window record `w`.  `c` carries the inputs (see cardiotwin_board)."""
        cfg = s.cfg; sc = cfg["strain"]; rm = cfg["region_model"]
        res = s.act.update(c)
        c["hr_elev"] = res["diag"].get("hr_elev")
        walk = res["act_score"] if res["act_basis"] == "ecg+ppg" else None
        strain, sconf, swhy, parts, x = s.strain.update(c, s.hist, walk) if not s.act.calibrating else (None, 0.0, "learning resting heart rate", {}, {})
        w.update(act=res["act"], act_conf=round(res["act_conf"], 2), act_reason=res["act_reason"], act_basis=res["act_basis"])
        if res["act_score"] is not None: w["act_score"] = round(res["act_score"], 3)
        for k, v in res["diag"].items(): w[k] = v if v is None else bool(v) if isinstance(v, bool) else round(v, 3)   # v2c: None is valid (e.g. mot_f with no stride peak)
        w["strain"] = None if strain is None else round(strain, 3)
        w["strain_conf"] = round(sconf, 2); w["strain_why"] = swhy; w["strain_parts"] = parts
        w["area_h_s"] = rm["horizon_windows"] * rm["hop_s"]
        if strain is None:
            w.update(area=None, area_q="none", area_reason="insufficient data: " + swhy, area_top=[], area_elev=[])
        else:
            n = sc["trend_windows"]
            ss = [(t, st) for t, _, _, st in s.hist[-(n - 1):] if st is not None] + [(c["t_mid"], strain)]
            sl = slope_per_min([p[0] for p in ss], [p[1] for p in ss], sc["trend_min_points"])
            rise = clamp((sl or 0.0) / rm["rise_scale_per_min"], 0, 2)
            xp, xf = s.strain.twin_inputs(c.get("u"))
            risks, tops = s.area.update(walk or 0.0, strain, rise, xf, xp, sc["twin_cap"])
            if xp is not None: q, reason = "full", ""
            elif not c.get("contact"): q, reason = "partial", "PPG poor contact: PPG-derived inputs missing"
            elif c.get("ppg_usable"): q, reason = "partial", "baseline warm-up: PPG-derived inputs not yet available"
            else: q, reason = "partial", "PPG unusable (%s): PPG-derived inputs missing" % c.get("ppg_reason", "")
            w.update(area=[round(v, 3) for v in risks], area_top=tops, area_q=q, area_reason=_cut(reason),
                     area_elev=[i for i, v in enumerate(risks) if v >= rm["elevated_threshold"]])
        s.hist.append((c["t_mid"], c.get("hr"), c.get("ln_pi"), strain)); del s.hist[:-40]
        return w
