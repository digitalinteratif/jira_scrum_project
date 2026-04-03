import os
import re
import subprocess
import logging
from datetime import datetime
from dotenv import load_dotenv
from crewai import Agent, Task, Crew, Process, LLM
from crewai.tools import BaseTool
from pydantic import BaseModel, Field
from jira import JIRA
from jira.exceptions import JIRAError

# --- 1. CONFIGURATION & SETUP ---
load_dotenv(override=True)

TARGET_JIRA_TICKET = os.environ.get("TARGET_JIRA_TICKET")
CODE_FILE = "generated_app.py"
LOG_DIR = "agent_logs"
os.makedirs(LOG_DIR, exist_ok=True)

log_ticket_id = re.sub(r'[^\w\s-]', '', TARGET_JIRA_TICKET if TARGET_JIRA_TICKET else "UNTITLED").strip().replace(' ', '_')
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = os.path.join(LOG_DIR, f"build_{log_ticket_id}_{timestamp}.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(log_file), logging.StreamHandler()]
)
logger = logging.getLogger("JiraBuilder")

if not TARGET_JIRA_TICKET:
    logger.error("TARGET_JIRA_TICKET not found in .env file.")
    exit(1)

def parse_model_env(env_value, default):
    if not env_value: return default
    match = re.search(r"model=['\"]([^'\"]+)['\"]", env_value)
    if match: return match.group(1)
    return env_value

# --- 2. FETCH REQUIREMENTS ---
def get_ticket_details(ticket_id):
    logger.info(f"🔗 Connecting to Jira: {ticket_id}")
    jira_server = os.environ.get("JIRA_SERVER")
    jira_email = os.environ.get("JIRA_EMAIL")
    jira_token = os.environ.get("JIRA_API_TOKEN")
    jira_client = JIRA(options={'server': jira_server}, basic_auth=(jira_email, jira_token))
    try:
        issue = jira_client.issue(ticket_id)
        requirements_text = f"TITLE: {issue.fields.summary}\n\nDESCRIPTION:\n{issue.fields.description}\n"
        return requirements_text
    except JIRAError as e:
        return f"Error: {e.text}"

# --- 3. RETRIEVE EXISTING CODE ---
def get_existing_code():
    if os.path.exists(CODE_FILE):
        with open(CODE_FILE, "r", encoding="utf-8") as f:
            content = f.read()
            # If the file contains Node.js or Javascript, we tell the Architect it's an error
            if "require('express')" in content or "const " in content:
                logger.warning("⚠️ Detected non-Python code in generated_app.py. Instructing Architect to RE-ARCHITECT in Python.")
                return "ERROR: The existing file is written in Javascript. You MUST ignore its structure and REWRITE it entirely in Python/Flask."
            return content
    return ""

jira_requirements = get_ticket_details(TARGET_JIRA_TICKET)
existing_context = get_existing_code()

# --- 4. THE QA TESTING TOOL ---
class PythonTesterSchema(BaseModel):
    code_string: str = Field(..., description="The full Python source code to test.")

class PythonTesterTool(BaseTool):
    name: str = "Python Code Execution and Syntax Tester"
    description: str = "Executes code. Returns SUCCESS if stable, or error."
    args_schema: type[BaseModel] = PythonTesterSchema

    def _run(self, code_string: str) -> str:
        code = code_string.replace('```python', '').replace('```', '').strip()
        with open("ai_test_run.py", "w", encoding="utf-8") as f:
            f.write(code)
        try:
            result = subprocess.run(["python", "ai_test_run.py"], capture_output=True, text=True, timeout=4)
            if result.returncode != 0: return f"CRASH:\n{result.stderr[-500:]}"
            return "SUCCESS"
        except subprocess.TimeoutExpired: return "SUCCESS"
        except Exception as e: return f"Error: {e}"

python_tester_tool = PythonTesterTool()

# --- 5. THE MODELS ---
arch_model = parse_model_env(os.environ.get("architect_llm"), "gpt-5-mini-2025-08-07")
code_model = parse_model_env(os.environ.get("coder_llm"), "gpt-5-mini-2025-08-07")
architect_llm = LLM(model=arch_model)
coder_llm = LLM(model=code_model)

# --- 6. THE AGENTS ---
architect = Agent(
    role='Python/Flask Architect',
    goal='Design a surgical Python/Flask blueprint. NO OTHER LANGUAGES ALLOWED.',
    backstory="""You are a Python specialist. You strictly use Flask and SQLAlchemy with SQLite. 
    You forbid the use of Node.js, Express, or Javascript for backend logic. You ensure 
    the blueprint mandates a functional HTML UI served via render_template_string.""",
    llm=architect_llm,
    verbose=True
)

coder = Agent(
    role='Senior Python Developer',
    goal='Implement the Python/Flask blueprint into a single runnable file.',
    backstory="""You only write Python. You never write scripts that 'create' other files. 
    You write the actual Flask application. You ensure the index route serves a full 
    HTML page, not just a message. You preserve existing Python logic surgically.""",
    llm=coder_llm,
    verbose=True
)

qa_auditor = Agent(
    role='Python QA Auditor',
    goal='Verify the code is valid Python/Flask and includes a functional UI.',
    backstory="""You reject any code that is not Python. You verify that the Flask 
    server includes HTML templates and functional routes. You run the stability test.""",
    llm=coder_llm,
    verbose=True,
    tools=[python_tester_tool]
)

# --- 7. THE TASKS ---
blueprint_task = Task(
    description=(
        f"Requirements:\n{jira_requirements}\n\nContext:\n{existing_context}\n\n"
        "TASK: Create a SURGICAL Blueprint in PYTHON/FLASK. "
        "Mandate: Use render_template_string for a UI including Login and Dashboard. "
        "Strictly ignore any existing Node.js/Javascript code and replace it with Python."
    ),
    expected_output="A targeted Python/Flask Technical Specification.",
    agent=architect
)

coding_task = Task(
    description=(
        "Write the COMPLETE updated Python Flask application in ONE file. "
        "You must include the HTML templates for the UI. "
        "Do NOT write a setup script; write the application itself."
    ),
    expected_output="The full updated Python script.",
    agent=coder,
    context=[blueprint_task]
)

audit_task = Task(
    description="Audit for Python syntax and UI presence. Run the tester tool.",
    expected_output="The finalized Python code in a ```python block.",
    agent=qa_auditor,
    context=[coding_task]
)

# --- 8. EXECUTION ---
crew = Crew(agents=[architect, coder, qa_auditor], tasks=[blueprint_task, coding_task, audit_task], process=Process.sequential, verbose=True)
result = crew.kickoff()

# --- 9. SAVE ---
output_string = result.raw if hasattr(result, 'raw') else str(result)
code_match = re.search(r'```python\n(.*?)\n```', output_string, re.DOTALL)
if code_match:
    with open(CODE_FILE, "w", encoding="utf-8") as f:
        f.write(code_match.group(1))
    print(f"\n🎉 SUCCESS! Python code saved to '{CODE_FILE}'. Run it with 'python {CODE_FILE}'")
else:
    print("\n⛔ Failed to isolate Python code block.")