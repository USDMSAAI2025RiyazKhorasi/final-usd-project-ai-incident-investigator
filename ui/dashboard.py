"""Dashboard: investigation reports, reasoning trail, evidence checks, approvals, results.

    streamlit run ui/dashboard.py

Pages
  Investigate   start an investigation on a snapshot through the backend, then approve or reject
  Runs          browse any experiment investigation under results/runs (no backend needed)
  Results       the results report and figures from eval/analysis/results.py
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pandas as pd
import streamlit as st
import yaml

ROOT = Path(__file__).resolve().parents[1]
API = os.environ.get("INVESTIGATOR_API", "http://localhost:8080")
USERS = yaml.safe_load((ROOT / "agent" / "users.yaml").read_text())
st.set_page_config(page_title="AI Incident Investigator", layout="wide")


def evidence_table(evidence: list[dict]) -> None:
    if not evidence:
        st.info("No evidence was cited.")
        return
    rows = []
    for e in evidence:
        usable = e["structural"] == "valid" and e.get("verdict") in ("SUPPORTS", "CONTRADICTS") and \
            ((e["claimed"] == "supports") == (e["verdict"] == "SUPPORTS")) and e.get("precedes_symptom") is not False
        rows.append({"id": e["id"], "agent": e["agent"], "hypothesis": e["hypothesis_id"], "claimed": e["claimed"],
                     "structural": e["structural"] + (f" ({e['structural_reason']})" if e.get("structural_reason") else ""),
                     "verifier": e.get("verdict") or "-", "before symptom": e.get("precedes_symptom"),
                     "usable": usable, "summary": e["summary"], "tool": e["provenance"]["tool"]})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def trail(trace: list[dict]) -> None:
    for ev in trace:
        if ev["kind"] == "tool":
            st.markdown(f"`{ev['t']:>6}s` **{ev['agent']}** called `{ev['tool']}({json.dumps(ev.get('args', {}))})` "
                        f"→ {ev.get('call_id')}")
            with st.expander("result", expanded=False):
                st.text(ev.get("summary", ""))
        elif ev["kind"] == "decision":
            st.markdown(f"`{ev['t']:>6}s` **decision** {json.dumps({k: v for k, v in ev.items() if k not in ('t', 'kind')})}")
        elif ev["kind"] == "plan":
            st.markdown(f"`{ev['t']:>6}s` **supervisor plan**: " +
                        "; ".join(f"{h['id']} {h['cause_type']}@{h['service']}" for h in ev.get("hypotheses", [])))


page = st.sidebar.radio("Page", ["Investigate", "Runs", "Results"])
st.sidebar.caption(f"Backend: {API}")

if page == "Investigate":
    st.title("Investigate")
    snaps = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "snapshots").glob("*/sc-*"))
    c1, c2, c3 = st.columns([3, 1, 1])
    snap = c1.selectbox("Snapshot", snaps)
    cfg = c2.selectbox("Configuration", ["C2", "C0", "C1", "A1", "A2", "A3", "A4", "A5", "A6"])
    if c3.button("Start investigation", type="primary"):
        try:
            r = httpx.post(f"{API}/investigations/snapshot", json={"snapshot": snap, "config": cfg}, timeout=10)
            st.session_state["inv"] = r.json()["id"]
        except httpx.HTTPError as e:
            st.error(f"Backend not reachable at {API}: {e}. Start it with `make api`.")
    try:
        invs = httpx.get(f"{API}/investigations", timeout=5).json()
    except httpx.HTTPError:
        invs = []
        st.warning("Backend not running. Start it with `make api`, or use the Runs page.")
    if invs:
        ids = [i["id"] for i in invs][::-1]
        cur = st.selectbox("Investigation", ids, index=ids.index(st.session_state["inv"]) if st.session_state.get("inv") in ids else 0)
        d = httpx.get(f"{API}/investigations/{cur}", timeout=10).json()
        st.subheader(f"Status: {d['status']}")
        if d["status"] in ("investigating", "executing"):
            time.sleep(1)
            st.rerun()
        if d.get("markdown"):
            left, right = st.columns([3, 2])
            with left:
                st.markdown(d["markdown"])
            with right:
                if d.get("proposal") and d["status"] == "awaiting_approval":
                    p = d["proposal"]
                    st.markdown(f"### Approve action\n**{p['action']} {p['target']}** {p['params']}  \n"
                                f"expires {p['expires_at'][11:19]} UTC  \nhash `{d['proposal_hash']}`")
                    user = st.selectbox("Acting as", list(USERS), index=list(USERS).index("alice"))
                    a1, a2 = st.columns(2)
                    if a1.button("Approve", type="primary"):
                        r = httpx.post(f"{API}/investigations/{cur}/approve", json={"hash": d["proposal_hash"]},
                                       headers={"X-User": user}, timeout=10)
                        (st.success if r.status_code == 200 else st.error)(r.json())
                    if a2.button("Reject"):
                        r = httpx.post(f"{API}/investigations/{cur}/reject", json={"reason": "rejected in UI"},
                                       headers={"X-User": user}, timeout=10)
                        (st.success if r.status_code == 200 else st.error)(r.json())
                if d.get("audit"):
                    st.markdown("### Audit log")
                    st.dataframe(pd.DataFrame(d["audit"]), hide_index=True)
            st.markdown("### Evidence checks")
            evidence_table(d.get("evidence", []))

elif page == "Runs":
    st.title("Experiment investigations")
    runs = sorted(p.name for p in (ROOT / "results" / "runs").glob("*") if p.is_dir())
    if not runs:
        st.info("No runs yet. Run `python -m eval.experiment`.")
    else:
        run = st.selectbox("Run", runs)
        base = ROOT / "results" / "runs" / run
        scores = {}
        if (base / "scores.jsonl").exists():
            df = pd.DataFrame([json.loads(l) for l in (base / "scores.jsonl").read_text().splitlines()])
            scores = {(r.scenario, r.run): r for r in df.itertuples()}
            m = st.columns(4)
            m[0].metric("Joint accuracy", f"{df.joint_correct.mean():.1%}")
            m[1].metric("Answered", f"{df.answered.mean():.1%}")
            m[2].metric("Tokens / investigation", f"{df.tokens.mean():,.0f}")
            m[3].metric("US$ / investigation", f"{df.cost_usd.mean():.4f}")
        invs = sorted(str(p.relative_to(base)) for p in base.glob("sc-*/r*") if (p / "report.json").exists())
        inv = st.selectbox("Investigation", invs)
        if inv:
            d = base / inv
            sid, r = inv.split("/")
            row = scores.get((sid, int(r[1:])))
            if row is not None:
                (st.success if row.joint_correct else st.error)(
                    f"Ground truth: {row.true_type} in {row.true_service} — predicted {row.pred_type} in {row.pred_service}")
            tab1, tab2, tab3 = st.tabs(["Report", "Evidence", "Reasoning trail"])
            with tab1:
                st.markdown((d / "report.md").read_text())
            with tab2:
                evidence_table(json.loads((d / "evidence.json").read_text()))
                led = json.loads((d / "ledger.json").read_text())
                if led:
                    st.markdown("**Contradiction ledger**")
                    st.dataframe(pd.DataFrame(led), hide_index=True)
            with tab3:
                trail([json.loads(l) for l in (d / "trace.jsonl").read_text().splitlines()])

else:
    st.title("Results")
    reports = sorted((ROOT / "results").glob("report-*.md"))
    if not reports:
        st.info("No results yet. Run `python -m eval.experiment`.")
    for rep in reports:
        text = rep.read_text()
        st.markdown("\n".join(l for l in text.splitlines() if not l.startswith("![")))
        for fig in (ROOT / "results").glob(f"fig_*{rep.stem.split('-', 1)[1]}.png"):
            st.image(str(fig))
