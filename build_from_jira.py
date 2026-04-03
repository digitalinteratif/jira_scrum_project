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
        return f"TITLE: {issue.fields.summary}\n\nDESCRIPTION:\n{issue.fields.description}\n"
    except JIRAError as e:
        return f"Error: {e.text}"

# --- 3. RETRIEVE EXISTING CODE (With UI Check) ---
def get_existing_code():
    if os.path.exists(CODE_FILE):
        with open(CODE_FILE, "r", encoding="utf-8") as f:
            content = f.read()
            # Safety check: If the code is missing UI markers, flag it for the Architect
            if "render_template_string" not in content and "render_layout" not in content:
                logger.warning("⚠️ Existing code appears to be 'headless'. Forcing UI implementation.")
                return f"EXISTING CODE (HEADLESS):\n{content}\n\nNOTE: This code lacks a UI. You MUST add the HTML templates."
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
arch_model = parse_model_env(os.environ.get("architect_llm"), "o3-mini")
code_model = parse_model_env(os.environ.get("coder_llm"), "gpt-5-codex")
architect_llm = LLM(model=arch_model)
coder_llm = LLM(model=code_model)

# --- 6. THE AGENTS ---
architect = Agent(
    role='Full-Stack Python Architect',
    goal='Design surgical updates that PRESERVE or ADD a functional HTML UI.',
    backstory="""You are a full-stack expert. You understand that a 'headless' API 
    is a failure for this project. You ensure every blueprint includes the 
    HTML templates and the 'render_layout' wrapper. You never allow a UI-driven 
    app to revert to a text-only API.""",
    llm=architect_llm,
    verbose=True
)

coder = Agent(
    role='Senior Python Developer',
    goal='Implement the blueprint in Python/Flask with a persistent HTML UI.',
    backstory="""You write production-ready Python. You ensure the '/' route 
    serves a beautiful HTML index page. You surgically merge new logic 
    without deleting the existing template strings or the UI layout wrapper.""",
    llm=coder_llm,
    verbose=True
)

qa_auditor = Agent(
    role='Python QA Auditor',
    goal='Ensure the app has a functional UI and correct Domain settings.',
    backstory="""You check the generated code. If the root route serves 
    plain text instead of HTML, you REJECT it. You verify the BASE_URL 
    points to digitalinteractif.com if specified in the .env.""",
    llm=coder_llm,
    verbose=True,
    tools=[python_tester_tool]
)

# --- 7. THE TASKS ---
blueprint_task = Task(
    description=(
        f"Requirements:\n{jira_requirements}\n\nContext:\n{existing_context}\n\n"
        "TASK: Create a SURGICAL Blueprint. MANDATORY: The resulting app MUST "
        "have a functional HTML UI (Home, Login, Register, Dashboard). "
        "Integrate the domain 'digitalinteractif.com' for all link generation."
    ),
    expected_output="A targeted Technical Specification mandating UI persistence.",
    agent=architect
)

coding_task = Task(
    description=(
        "Write the COMPLETE updated Python Flask application.\n"
        f"EXISTING SOURCE:\n{existing_context}\n\n"
        "Rules: DO NOT return plain text for user routes. The index page MUST be HTML. "
        "Use render_template_string and the layout wrapper helper."
    ),
    expected_output="The full updated Python script with UI and Domain logic.",
    agent=coder,
    context=[blueprint_task]
)

audit_task = Task(
    description=(
        "Audit for UI and syntax. Check 1: Does '/' return HTML? "
        "Check 2: Does it use digitalinteractif.com for links? "
        "Check 3: Run the stability test tool."
    ),
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
    print(f"\n🎉 SUCCESS! Python code with UI saved to '{CODE_FILE}'.")
else:
    print("\n⛔ Failed to isolate Python code block.")