"""Risk Monitor — standalone agent that scans active projects for risk signals.

Launched by the cron worker weekly. Reads sim_time from Redis, queries the
firm database, flags risk conditions, and generates an LLM narrative analysis.

Risk conditions flagged:
  - Overdue deliverables (past planned_end and not complete)
  - Slip factor above threshold (actual hours exceeding earned planned hours)
  - Fixed-price margin erosion (costs-to-date approaching contract value)

Severity levels: WARN (early signal), HIGH (needs immediate attention).
Appends a JSON record per run to model_data/risk_alerts.jsonl.
"""
import calendar
import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import anthropic
import redis

_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip().removeprefix("export").strip(), _v.strip().strip('"').strip("'"))

DB_PATH  = "model_data/firm.db"
_LOG     = Path("model_data") / "risk_alerts.jsonl"

_SLIP_WARN   = 1.2
_SLIP_HIGH   = 1.5
_MARGIN_WARN = 0.65
_MARGIN_HIGH = 0.85


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _sim_time() -> datetime:
    raw = os.environ.get("SIM_TIME")
    if not raw:
        r = redis.Redis(host="localhost", port=6379, db=1, decode_responses=True)
        raw = r.get("sim:clock:time")
    if not raw:
        raise RuntimeError("sim:clock:time not available")
    return datetime.fromisoformat(raw)


# --- query helpers -----------------------------------------------------------

def _overdue_deliverables(conn, sim_date: str) -> list[dict]:
    rows = conn.execute("""
        SELECT d.name, d.planned_end, d.pct_complete, p.name, p.project_type
        FROM deliverables d
        JOIN projects p ON p.project_id = d.project_id
        WHERE d.status != 'complete'
          AND d.planned_end < ?
          AND p.status = 'active'
        ORDER BY d.planned_end
    """, (sim_date,)).fetchall()
    return [
        {"deliverable": r[0], "planned_end": r[1], "pct_complete": r[2],
         "project": r[3], "project_type": r[4]}
        for r in rows
    ]


def _slip_factor(conn, project_id: int) -> float:
    row = conn.execute("""
        SELECT
          (SELECT COALESCE(SUM(t.hours),0) FROM time_entries t
           JOIN deliverables d ON d.deliverable_id=t.deliverable_id
           WHERE d.project_id = ?) AS hours_to_date,
          (SELECT COALESCE(SUM(ph.planned_hours),0)
           FROM deliverable_plan_hours ph
           JOIN deliverables d ON d.deliverable_id=ph.deliverable_id
           WHERE d.project_id = ? AND d.status='complete') AS completed_planned
    """, (project_id, project_id)).fetchone()
    hours_to_date, completed_planned = row
    if completed_planned <= 0:
        return 1.0
    return max(1.0, hours_to_date / completed_planned)


def _payroll_cost_in_window(conn, start_ym: str, end_ym: str, project_id: int) -> float:
    rows = conn.execute("""
        SELECT t.hours, t.year_month, h.annual_salary, h.start_date, h.end_date
        FROM time_entries t
        JOIN deliverables d ON d.deliverable_id = t.deliverable_id
        JOIN consultants c  ON c.consultant_id = t.consultant_id
        JOIN consultant_title_history h
          ON h.consultant_id = c.consultant_id
         AND h.start_date <= date(t.year_month || '-01', '+1 month', '-1 day')
         AND (h.end_date IS NULL OR h.end_date > (t.year_month || '-01'))
        WHERE t.year_month BETWEEN ? AND ?
          AND d.project_id = ?
    """, (start_ym, end_ym, project_id)).fetchall()

    total = 0.0
    for hours, year_month, salary, h_start, h_end in rows:
        y, m = int(year_month[:4]), int(year_month[5:7])
        month_length = calendar.monthrange(y, m)[1]
        month_start  = date(y, m, 1)
        month_end    = date(y, m, month_length)
        title_start  = date.fromisoformat(h_start)
        title_last   = (date.fromisoformat(h_end) - timedelta(days=1)) if h_end else month_end
        active_start = max(month_start, title_start)
        active_end   = min(month_end, title_last)
        days_held    = (active_end - active_start).days + 1
        if days_held <= 0:
            continue
        total += hours * (days_held / month_length) * (salary / 2080.0)
    return total


def _costs_ytd(conn, project_id: int, sim_time: datetime) -> float:
    year_start = f"{sim_time.year}-01"
    current_ym = sim_time.strftime("%Y-%m")
    labor = _payroll_cost_in_window(conn, year_start, current_ym, project_id)
    expenses = conn.execute("""
        SELECT COALESCE(SUM(e.amount), 0)
        FROM expenses e
        JOIN deliverables d ON d.deliverable_id = e.deliverable_id
        WHERE d.project_id = ?
    """, (project_id,)).fetchone()[0]
    return labor + expenses


# --- LLM analysis ------------------------------------------------------------

def _llm_analysis(sim_date: str, alerts: list[dict]) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return

    if not alerts:
        alert_text = "No alerts this week — all projects are on track."
    else:
        lines = []
        for a in alerts:
            if a["type"] == "overdue_deliverable":
                lines.append(
                    f"- OVERDUE [{a['severity']}]: '{a['deliverable']}' on project "
                    f"'{a['project']}' ({a['project_type']}) is {a['days_late']} days late, "
                    f"{a['pct_complete']}% complete"
                )
            elif a["type"] == "slip_factor":
                lines.append(
                    f"- SLIP [{a['severity']}]: Project '{a['project']}' ({a['project_type']}) "
                    f"has a slip factor of {a['slip_factor']}x (actual hours exceed plan)"
                )
            elif a["type"] == "margin_erosion":
                lines.append(
                    f"- MARGIN [{a['severity']}]: Fixed-price project '{a['project']}' has consumed "
                    f"{a['cost_ratio']*100:.0f}% of contract value "
                    f"(${a['costs_to_date']:,.0f} of ${a['contract_value']:,.0f})"
                )
        alert_text = "\n".join(lines)

    prompt = (
        f"You are a consulting firm PMO analyst preparing a weekly risk briefing for senior leadership.\n\n"
        f"Risk alerts detected for the week of {sim_date}:\n{alert_text}\n\n"
        "Write an executive risk report in markdown. Structure it as follows:\n\n"
        "# Weekly Risk Report — {sim_date}\n\n"
        "## Executive Summary\n"
        "2-3 sentences on overall risk posture and the most critical item requiring leadership attention.\n\n"
        "## Risk Findings\n"
        "A table or bullet list of all alerts grouped by severity (HIGH first), with project name, "
        "risk type, and a one-line impact statement for each.\n\n"
        "## Root Cause Analysis\n"
        "For each HIGH alert: what is driving it and what has likely compounded it.\n\n"
        "## Recommended Actions\n"
        "Numbered list of specific, actionable steps. Each action should name who should own it "
        "and what the expected outcome is.\n\n"
        "## Outlook\n"
        "1-2 sentences on trajectory — is risk increasing, stabilizing, or improving?\n\n"
        "Use professional business language. Be direct and specific. Avoid filler phrases."
    ).format(sim_date=sim_date)

    out_dir = Path("model_data") / "risk_analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=1024,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
        )
        narrative = "\n".join(b.text for b in response.content if b.type == "text")
        print("[risk_monitor] LLM analysis:")
        print(narrative)
        (out_dir / f"{sim_date}.md").write_text(narrative)
    except Exception as e:
        (out_dir / f"{sim_date}.error.log").write_text(str(e))
        print(f"[risk_monitor] LLM error: {e}")


# --- main --------------------------------------------------------------------

def main():
    sim_time = _sim_time()
    sim_date = sim_time.date().isoformat()
    alerts   = []

    conn = _connect()
    try:
        for d in _overdue_deliverables(conn, sim_date):
            days_late = (sim_time.date() - datetime.fromisoformat(d["planned_end"]).date()).days
            alerts.append({
                "type":        "overdue_deliverable",
                "severity":    "HIGH" if days_late > 14 else "WARN",
                "project":     d["project"],
                "project_type": d["project_type"],
                "deliverable": d["deliverable"],
                "days_late":   days_late,
                "pct_complete": round(d["pct_complete"], 1),
            })

        for project_id, name, ptype in conn.execute(
            "SELECT project_id, name, project_type FROM projects WHERE status = 'active'"
        ).fetchall():
            sf = _slip_factor(conn, project_id)
            if sf >= _SLIP_WARN:
                alerts.append({
                    "type":         "slip_factor",
                    "severity":     "HIGH" if sf >= _SLIP_HIGH else "WARN",
                    "project":      name,
                    "project_type": ptype,
                    "slip_factor":  round(sf, 3),
                })

        for project_id, name, contract_value in conn.execute("""
            SELECT project_id, name, contract_value FROM projects
            WHERE project_type = 'fixed_price' AND status = 'active' AND contract_value > 0
        """).fetchall():
            cost  = _costs_ytd(conn, project_id, sim_time)
            ratio = cost / contract_value
            if ratio >= _MARGIN_WARN:
                alerts.append({
                    "type":           "margin_erosion",
                    "severity":       "HIGH" if ratio >= _MARGIN_HIGH else "WARN",
                    "project":        name,
                    "costs_to_date":  round(cost, 2),
                    "contract_value": round(contract_value, 2),
                    "cost_ratio":     round(ratio, 3),
                })
    finally:
        conn.close()

    highs = [a for a in alerts if a["severity"] == "HIGH"]
    warns  = [a for a in alerts if a["severity"] == "WARN"]
    print(f"[risk_monitor] {sim_date}  {len(highs)} HIGH  {len(warns)} WARN")
    for a in highs + warns:
        if a["type"] == "overdue_deliverable":
            print(f"  [{a['severity']}] OVERDUE {a['days_late']}d — "
                  f"'{a['deliverable']}' on '{a['project']}' ({a['pct_complete']}% done)")
        elif a["type"] == "slip_factor":
            print(f"  [{a['severity']}] SLIP {a['slip_factor']}x — "
                  f"'{a['project']}' ({a['project_type']})")
        elif a["type"] == "margin_erosion":
            print(f"  [{a['severity']}] MARGIN {a['cost_ratio']*100:.0f}% — "
                  f"'{a['project']}' ${a['costs_to_date']:,.0f} / ${a['contract_value']:,.0f}")

    _LOG.parent.mkdir(exist_ok=True)
    with _LOG.open("a") as f:
        f.write(json.dumps({"sim_date": sim_date, "alerts": alerts}) + "\n")

    _llm_analysis(sim_date, alerts)


if __name__ == "__main__":
    main()
