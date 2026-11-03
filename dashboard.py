"""CardioTwin - Streamlit replay dashboard for the recorded AFE4950 session."""
import json
from pathlib import Path

import numpy as np
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from analyze_signals import (CSV, DATA, ECG_BAND, PPG_BAND, bandpass,
                             detect_r_peaks, load_channels, sample_rate)

st.set_page_config(page_title="CardioTwin", page_icon="🫀", layout="wide")


@st.cache_data
def load_all():
    t_e, ecg, t_p, ppg = load_channels(CSV)
    fs_e, fs_p = sample_rate(t_e), sample_rate(t_p)
    ecg_f = bandpass(ecg, fs_e, *ECG_BAND)
    ppg_f = bandpass(ppg, fs_p, *PPG_BAND)
    r_idx = detect_r_peaks(ecg_f, fs_e)
    return dict(t_e=t_e, ecg=ecg, ecg_f=ecg_f, t_p=t_p, ppg=ppg, ppg_f=ppg_f,
                r_idx=r_idx, fs_e=fs_e, fs_p=fs_p)


def show(fig):
    try:
        st.plotly_chart(fig, width="stretch")
    except TypeError:  # older Streamlit
        st.plotly_chart(fig, use_container_width=True)


st.title("CardioTwin")
st.caption("Replay of a previously recorded AFE4950 session (not live streaming). "
           "Peak/match values are prototype candidates, not clinical measurements.")

if not CSV.exists():
    st.error(f"Missing {CSV}. Copy AFE4950_CAPTURED_DATA.csv into the data folder.")
    st.stop()

d = load_all()
metrics_file = DATA / "metrics.json"
if metrics_file.exists():
    m = json.loads(metrics_file.read_text())
    hr, dur = m["heart_rate_bpm"], m["recording_length_s"]
else:
    st.warning("metrics.json not found - run `python analyze_signals.py` first. Using a quick estimate.")
    tr = d["t_e"][d["r_idx"]]
    hr, dur = 60 / np.mean(np.diff(tr)), d["t_e"][-1]

st.subheader("Cardiovascular Overview")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Heart Rate", f"{hr:.0f} BPM")
c2.metric("Recording Duration", f"{dur:.1f} sec")
c3.metric("ECG Sampling", f"{d['fs_e']:.0f} Hz")
c4.metric("PPG Sampling", f"{d['fs_p']:.0f} Hz")

st.subheader("Physiological Signal Monitoring")
total = float(max(d["t_e"][-1], d["t_p"][-1]))
col_a, col_b, col_c = st.columns([3, 1, 1])
window = col_a.slider("Viewing window (seconds)", 2.0, min(30.0, total), min(10.0, total), 0.5)
filt_on = col_b.toggle("Digital Filtering", value=True)
peaks_on = col_c.toggle("R-Peak Markers", value=True)
start = st.slider("Navigate Recording", 0.0, max(total - window, 0.1),
                  0.0, 0.1) if total > window else 0.0
end = start + window

ecg_y = d["ecg_f"] if filt_on else d["ecg"] - np.mean(d["ecg"])
ppg_y = d["ppg_f"] if filt_on else d["ppg"]
em = (d["t_e"] >= start) & (d["t_e"] <= end)
pm = (d["t_p"] >= start) & (d["t_p"] <= end)

fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.12,
                    subplot_titles=("Electrocardiogram — ECG", "Optical Pulse Channel — PPG"))
fig.add_trace(go.Scatter(x=d["t_e"][em], y=ecg_y[em], name="ECG", mode="lines",
                         line=dict(color="#38bdf8", width=1.4)), row=1, col=1)
if peaks_on:
    ri = d["r_idx"]
    ri = ri[(d["t_e"][ri] >= start) & (d["t_e"][ri] <= end)]
    fig.add_trace(go.Scatter(x=d["t_e"][ri], y=ecg_y[ri], name="R-Peaks", mode="markers",
                             marker=dict(color="#fb923c", size=9)), row=1, col=1)
fig.add_trace(go.Scatter(x=d["t_p"][pm], y=ppg_y[pm], name="PPG", mode="lines",
                         line=dict(color="#2dd4bf", width=1.8)), row=2, col=1)
fig.update_xaxes(title_text="Time (seconds)", range=[start, end], row=2, col=1)
fig.update_yaxes(title_text="Amplitude", row=1, col=1)
fig.update_yaxes(title_text="Amplitude", row=2, col=1)
fig.update_layout(height=650, template="plotly_dark", margin=dict(l=40, r=20, t=60, b=40),
                  legend=dict(orientation="h", y=1.08, x=1, xanchor="right"))
show(fig)