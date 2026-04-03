# JIRA Scrum Project

AI agent project for managing JIRA tickets and Scrum sprints using CrewAI.

## Setup

1. Create virtual environment:
```bash
python -m venv venv
```

2. Activate it:
```bash
# Windows
.\venv\Scripts\Activate.ps1

# macOS/Linux
source venv/bin/activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Set up `.env` with your `CEREBRAS_API_KEY`

5. Run the project:
```bash
python main.py
```

## Project Structure

- `src/crew.py` - Crew definition with agents and tasks
- `src/config.py` - Configuration and project state
- `main.py` - Entry point
- `pyproject.toml` - Project metadata and dependencies
- `poetry.lock` / `uv.lock` - Dependency lock files
