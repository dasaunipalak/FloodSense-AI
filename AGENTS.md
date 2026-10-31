# FloodSense AI project rules

FloodSense AI is a Forge hackathon project for evidence-grounded flood-risk investigations and preparedness guidance.

- Use only real APIs or permitted datasets. Never fabricate observational data.
- Never hardcode rainfall values or flood-risk results.
- Keep data fetching (`data/`), analysis (`analysis/`), AI (`ai/`), and UI (`app.py`) separate.
- Handle API failures explicitly. Report unavailable data; never substitute invented values.
- Never commit API keys, credentials, or local environment files. Use environment variables or Streamlit secrets when needed.
- Never describe results as scientifically validated flood predictions. Clearly communicate uncertainty and limitations.
- Implement only the requested stage. Weather APIs, terrain analysis, maps, and LLM agents are deferred in the initial scaffold.
- Do not create a GitHub repository or push code without explicit user authorization.

## Local development

Use Python 3.11 or newer. Create `.venv`, install `requirements.txt`, and run `streamlit run app.py` from the project root. Keep generated files and the virtual environment out of version control.
