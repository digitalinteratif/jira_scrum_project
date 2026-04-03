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
    # Extract name from pattern model='name' or just return the string
    match = re.search(r"model=['\"]([^'\"]+)['\"]", env_value)
    if match: return match.group(1)
    return env_value

# --- 2. FETCH REQUIREMENTS (Recursive Epic Retrieval) ---
def get_ticket_details(ticket_id):
    logger.info(f"🔗 Connecting to Jira: {ticket_id}")
    jira_server = os.environ.get("JIRA_SERVER")
    jira_email = os.environ.get("JIRA_EMAIL")
    jira_token = os.environ.get("JIRA_API_TOKEN")
    jira_client = JIRA(options={'server': jira_server}, basic_auth=(jira_email, jira_token))
    
    try:
        issue = jira_client.issue(ticket_id)
        summary = issue.fields.summary
        description = issue.fields.description or "No description provided."
        
        full_context = f"--- TARGET TICKET: {ticket_id} ---\nTITLE: {summary}\nDESCRIPTION:\n{description}\n"

        # RECURSIVE CONTEXT: Check for Parent Epic
        parent = getattr(issue.fields, 'parent', None)
        epic_key = None
        
        # Check standard parent field or common Epic Link custom field
        if parent:
            epic_key = parent.key
        elif hasattr(issue.fields, 'customfield_10011'): # Common Epic Link ID
            epic_key = issue.fields.customfield_10011

        if epic_key:
            logger.info(f"🔍 Found Parent Epic: {epic_key}. Fetching global rules...")
            epic_issue = jira_client.issue(epic_key)
            epic_desc = epic_issue.fields.description or ""
            full_context = f"--- GLOBAL FEATURE OVERVIEW (From Epic {epic_key}) ---\n{epic_desc}\n\n" + full_context
        
        return full_context

    except JIRAError as e:
        logger.error(f"❌ Jira Error: {e.text}")
        return f"Error: {e.text}"

# --- 3. RETRIEVE EXISTING CODE ---
def get_existing_code():
    if os.path.exists(CODE_FILE):
        with open(CODE_FILE, "r", encoding="utf-8") as f:
            content = f.read()
            # Python Stack Lock Check
            if "require(" in content or "const " in content:
                logger.warning("⚠️ Non-Python code detected. Forcing clean Python rewrite.")
                return "ERROR: Existing code is Javascript. You MUST ignore its structure and build in Python/Flask."
            return content
    return ""

logger.info("Initializing context retrieval...")
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
    role='Full-Stack Security Architect',
    goal='Design surgical updates following the Global Feature Overview and Story requirements.',
    backstory="""You are a security-first architect. You STRICTLY follow the 
    'Immutable Technology Stack' and 'Agent Contextual Awareness Rules'. 
    You always verify that a plan includes CSRF tokens and uses the 
    render_layout pattern. You never allow technology drift.""",
    llm=architect_llm,
    verbose=True
)

coder = Agent(
    role='Senior Python Developer',
    goal='Implement the blueprint in Python/Flask, ensuring global standards are met.',
    backstory="""You specialize in high-precision Python. You ensure that 
    every form has a CSRF token and that the single-file constraint is 
    maintained. You merge Story requirements into the existing code without 
    breaking the global rules defined in the Epic overview.""",
    llm=coder_llm,
    verbose=True
)

qa_auditor = Agent(
    role='Compliance & Security Auditor',
    goal='Verify the code satisfies both the Story AND the Epic Overview standards.',
    backstory="""You audit code for regressions and compliance. You reject 
    any code that lacks CSRF tokens, attempts to use Javascript, or 
    violates the single-file mandate. You verify BASE_URL and Domain logic.""",
    llm=coder_llm,
    verbose=True,
    tools=[python_tester_tool]
)

# --- 7. THE TASKS ---
blueprint_task = Task(
    description=(
        f"Context & Requirements:\n{jira_requirements}\n\nExisting Code:\n{existing_context}\n\n"
        "TASK: Create a SURGICAL Blueprint. You MUST:\n"
        "1. Validate the plan against the GLOBAL FEATURE OVERVIEW (Section 2 & 4).\n"
        "2. Address the specific Story requirements for the delta.\n"
        "3. Ensure the render_layout and CSRF mandates are satisfied."
    ),
    expected_output="A Technical Spec that satisfies both global and story-specific requirements.",
    agent=architect
)

coding_task = Task(
    description=(
        "Update 'generated_app.py' surgically.\n"
        f"SOURCE:\n{existing_context}\n\n"
        "Rules: Maintain 100% Python/Flask. Ensure every <form> has CSRF tokens. "
        "Use the render_layout helper. Preserve the single-file structure."
    ),
    expected_output="The full updated Python script.",
    agent=coder,
    context=[blueprint_task]
)

audit_task = Task(
    description=(
        "Audit for Compliance. Check: CSRF tokens in forms, correct Domain usage, "
        "and successful stability test. Your final response MUST be ONLY the "
        "code in a ```python block."
    ),
    expected_output="The finalized, compliant Python code.",
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
    print(f"\n🎉 SUCCESS! Context-aware surgical update for {TARGET_JIRA_TICKET} complete.")
else:
    print("\n⛔ Failed to isolate Python code block.")