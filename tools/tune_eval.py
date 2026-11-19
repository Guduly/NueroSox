#!/usr/bin/env python3
"""Compare a model config against the code defaults by replaying recordings (laptop, stdlib only).

    python3 -I tools/tune_eval.py                          # board/twin_config.json vs defaults
    python3 -I tools/tune_eval.py --config my_config.json  # any other config

For each recording it prints: windows, how many the model labels sitting / walking,
mean inferred strain, mean worst-region load, and windows with a region above the alert level.
sit_clean and walk3 are the two labelled recordings (seated still / walking, same person,
run with --rest-hr 90); the rest are unlabelled and replayed with the board's default floor.
"""
import argparse, json, os, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARD = os.path.join(ROOT, "board", "cardiotwin_board.py")
sys.path.insert(0, os.path.join(ROOT, "board"))
import activity_model as am  # noqa: E402

LABELLED = {"sit_clean": ("sitting", 90), "walk3": ("walking", 90)}


def replay(path, cfg, rest_hr, tmp):
    args = [sys.executable, "-I", BOARD, "--replay", path, "--reset", "--config", cfg,
            "--state", os.path.join(tmp, "s.json"), "--log", os.path.join(tmp, "w.jsonl")]
    if rest_hr:
        args += ["--rest-hr", str(rest_hr)]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return [json.loads(l[4:]) for l in out.splitlines() if l.startswith("CT1 ") and '"type":"window"' in l]


def summary(ws):
    acts = [w.get("act") for w in ws]
    st = [w["strain"] for w in ws if w.get("strain") is not None]
    ar = [max(w["area"]) for w in ws if w.get("area")]
    mean = lambda v: sum(v) / len(v) if v else float("nan")
    return "%3d win  sit %2d  walk %2d  strain %.2f  area %.2f  alert %2d" % (
        len(ws), acts.count("sitting"), acts.count("walking"), mean(st), mean(ar),
        sum(1 for w in ws if w.get("area_elev")))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(ROOT, "board", "twin_config.json"))
    ap.add_argument("--data", default=os.path.join(ROOT, "data"))
    a = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "defaults.json")
        json.dump(am.DEFAULT_CONFIG, open(base, "w"))
        names = sorted(f[:-6] for f in os.listdir(a.data) if f.endswith(".jsonl"))
        names.sort(key=lambda n: n not in LABELLED)
        for n in names:
            label, rest = LABELLED.get(n, ("unlabelled", None))
            p = os.path.join(a.data, n + ".jsonl")
            print("%-14s (%s)" % (n, label))
            print("   defaults : " + summary(replay(p, base, rest, tmp)))
            print("   config   : " + summary(replay(p, a.config, rest, tmp)))


if __name__ == "__main__":
    main()
