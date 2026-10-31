# FloodSense AI

A Forge hackathon project for an agentic geospatial flood-risk investigation system. The planned system will combine real weather forecasts, geographic data, and an LLM tool-calling agent to produce evidence-grounded risk assessments and preparedness recommendations.

## Current stage

This minimal Streamlit scaffold provides latitude/longitude inputs and an **Investigate** button. It validates that both coordinates are present and acknowledges the selected location. It does not fetch data or produce risk assessments. Weather APIs, terrain analysis, maps, and AI agents are not implemented.

FloodSense AI does not provide scientifically validated flood predictions.

## Run locally

Requires Python 3.11 or newer. From the project directory on macOS:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run app.py
```

Open the local URL printed by Streamlit. Stop the server with Control-C. No API keys are needed for this scaffold.

## Project structure

```text
app.py             Streamlit UI and coordinate input validation
data/__init__.py   Reserved for real API and permitted dataset access
analysis/__init__.py Reserved for analysis logic
ai/__init__.py     Reserved for the future LLM agent
AGENTS.md          Development and hackathon rules
requirements.txt   Python dependencies
.gitignore         Excludes virtual environments, caches, and secrets
README.md          Project status and setup instructions
```

## Data integrity and security

- Use real APIs or permitted datasets; never fabricate observations.
- Never hardcode rainfall values or flood-risk results.
- Keep fetching, analysis, AI, and UI separate.
- Report API failures and missing data without inventing substitutes.
- Keep keys and credentials out of version control.
- Explain uncertainty and limitations; do not claim scientific validation.

Planned integrations are Open-Meteo, OpenTopoData, and an LLM tool-calling agent. These are future work only.
