# Risk Monitor Agent

A weekly agent that scans active projects for risk signals and generates an executive risk briefing using Claude AI.

## What It Does

Fires every Monday via the simulation's cron worker. For each run it:

1. Queries the firm database for three risk conditions:
   - **Overdue deliverables** — past their planned end date and not complete
   - **Slip factor** — actual hours significantly exceeding earned planned hours
   - **Margin erosion** — fixed-price project costs approaching contract value
2. Classifies each alert as `WARN` (early signal) or `HIGH` (needs immediate attention)
3. Calls `claude-opus-4-7` to generate a narrative executive risk report in markdown
4. Appends structured alert data to `model_data/risk_alerts.jsonl`
5. Writes the markdown report to `model_data/risk_analysis/YYYY-MM-DD.md`

## Installation

Clone into the framework's `agents/` directory and run setup:

```bash
git clone https://github.com/aurite-simulator/risk-monitor-agent agents/risk_monitor
bash setup.sh
```

`setup.sh` automatically installs dependencies from `requirements.txt` into the shared virtualenv.

## Configuration

Create a `.env` file in this directory with your Anthropic API key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

If no API key is present the agent still runs and writes structured alert data — the LLM narrative is silently skipped.

## Running

The agent is launched automatically by the simulation's cron worker every Monday. To run manually:

```bash
source venv/bin/activate
python agents/risk_monitor/risk_monitor.py
```

The simulation must be running (or have recently run) so that `sim:clock:time` is available in Redis.

## Output

| File | Description |
|------|-------------|
| `model_data/risk_alerts.jsonl` | One JSON record per run with all alerts |
| `model_data/risk_analysis/YYYY-MM-DD.md` | Executive risk report for that week |
| `model_data/risk_analysis/YYYY-MM-DD.error.log` | LLM error details if the API call fails |
