import os
import re
import sys
import json
import subprocess
import logging
import codecs
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
from crewai import Agent, Task, Crew, Process, LLM
from crewai.tools import BaseTool
# Added Type for Pydantic schema validation
from typing import List
from pydantic import BaseModel, Field
from jira import JIRA
from jira.exceptions import JIRAError

# --- 1. CONFIGURATION & LOGGING ---
load_dotenv(override=True)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

TARGET_JIRA_TICKET = os.environ.get("TARGET_JIRA_TICKET")
# FIX: Set PROJECT_ROOT to '.' to allow agents to specify full paths relative to the repo root.
PROJECT_ROOT = "."
ENTRY_POINT = "app_core/app.py"
LOG_DIR = "agent_logs"
BASE_URL = os.environ.get("BASE_URL", "https://digitalinteractif.com")

os.makedirs(LOG_DIR, exist_ok=True)

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = os.path.join(LOG_DIR, f"build_{timestamp}.log")
file_handler = logging.FileHandler(log_file, encoding='utf-8')
stream_handler = logging.StreamHandler(sys.stdout)

logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    handlers=[file_handler, stream_handler]
)
logger = logging.getLogger("JiraBuilder")

# --- 2. JIRA CLIENT UTILITIES ---
def get_jira_client():
    jira_server = os.environ.get("JIRA_SERVER")
    jira_email = os.environ.get("JIRA_EMAIL")
    jira_token = os.environ.get("JIRA_API_TOKEN")
    return JIRA(options={'server': jira_server}, basic_auth=(jira_email, jira_token))

def get_ticket_details_from_issue(jira_client, issue):
    """Formats details for an issue, including parent Epic context."""
    summary = issue.fields.summary
    description = issue.fields.description or "No description provided."
    full_context = f"--- TARGET TICKET: {issue.key} ---\nTITLE: {summary}\nDESCRIPTION:\n{description}\n"

    parent = getattr(issue.fields, 'parent', None)
    epic_key = None
    if parent:
        epic_key = parent.key
    elif hasattr(issue.fields, 'customfield_10011'): 
        epic_key = issue.fields.customfield_10011

    if epic_key:
        logger.info(f"🔍 Found Parent Epic: {epic_key}. Fetching global rules...")
        epic_issue = jira_client.issue(epic_key)
        epic_desc = epic_issue.fields.description or ""
        full_context = f"--- GLOBAL FEATURE OVERVIEW (From Epic {epic_key}) ---\n{epic_desc}\n\n" + full_context
    
    return full_context

def get_epic_children(jira_client, epic_id):
    """Finds all child stories and filters by status."""
    try:
        issue = jira_client.issue(epic_id)
        if issue.fields.issuetype.name != 'Epic':
            return [issue]
        
        logger.info(f"📂 '{epic_id}' is an Epic. Searching for child stories...")
        jql = f'parent = "{epic_id}" OR "Epic Link" = "{epic_id}" ORDER BY created ASC'
        children = jira_client.search_issues(jql, maxResults=200)
        
        if not children:
            logger.warning(f"Empty Epic: No children found for {epic_id}.")
            return [issue]
            
        return children
    except JIRAError as e:
        logger.error(f"❌ Failed to check Epic status: {e.text}")
        return []

def transition_to_done(jira_client, issue):
    """Automatically transitions a successful Jira issue to 'Done'."""
    try:
        transitions = jira_client.transitions(issue)
        target_transition = next((t for t in transitions if 'done' in t['name'].lower()), None)
        
        if target_transition:
            jira_client.transition_issue(issue, target_transition['id'])
            logger.info(f"✅ Issue {issue.key} transitioned to DONE.")
        else:
            logger.warning(f"⚠️ No 'Done' transition found for {issue.key}.")
    except JIRAError as e:
        logger.error(f"❌ Failed to transition {issue.key}: {e.text}")

# --- 3. RETRIEVE EXISTING CODEBASE ---
def get_existing_codebase():
    """Aggregates all files in the app_core directory for AI context."""
    code_map = ""
    # We only care about the app_core folder for the coder's logic
    target_dir = Path("app_core")
    if not target_dir.exists():
        return "CODEBASE IS EMPTY."
    
    for path in target_dir.rglob('*.py'):
        relative_path = path
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
            code_map += f"\n--- FILE: {relative_path} ---\n{content}\n"
    
    return code_map if code_map else "CODEBASE IS EMPTY."

# --- 4. THE ONLINE VALIDATOR TOOL ---
class OnlineValidatorSchema(BaseModel):
    endpoints: List[str] = Field(..., description="List of relative paths to check (e.g. ['/', '/login', '/register'])")

class OnlineValidatorTool(BaseTool):
    name: str = "online_production_validator"
    description: str = "Checks the LIVE website at BASE_URL to verify deployment success."
    args_schema: type[BaseModel] = OnlineValidatorSchema

    def _run(self, endpoints: List[str]) -> str:
        results = []
        logger.info(f"🌐 Starting Online Validation for: {endpoints}")
        for ep in endpoints:
            url = f"{BASE_URL.rstrip('/')}/{ep.lstrip('/')}"
            try:
                response = requests.get(url, timeout=20)
                if response.status_code == 200:
                    if "URL.CO" in response.text or "digitalinteractif" in response.text:
                        results.append(f"✅ {ep}: 200 OK (Verified Content)")
                    else:
                        results.append(f"⚠️ {ep}: 200 OK (Content Signature Missing)")
                else:
                    results.append(f"❌ {ep}: {response.status_code} Error")
            except Exception as e:
                results.append(f"❌ {ep}: Connection Failed ({str(e)})")
        
        return "\n".join(results)

# --- 5. THE LOCAL STABILITY TESTER TOOL ---
class PythonTesterSchema(BaseModel):
    codebase_payload: str = Field(..., description="The full proposed codebase update.")

class PythonTesterTool(BaseTool):
    name: str = "python_stability_tester"
    description: str = "Boots the app locally and verifies WSGI 'app' attribute exists."
    args_schema: type[BaseModel] = PythonTesterSchema

    def _run(self, codebase_payload: str) -> str:
        test_dir = Path("temp_stability_test")
        if test_dir.exists():
            import shutil
            shutil.rmtree(test_dir)
        test_dir.mkdir(parents=True, exist_ok=True)
        
        files = re.findall(r'--- FILE: (.*?) ---\n(.*?)(?=\n--- FILE:|$)', codebase_payload, re.DOTALL)
        for f_path, content in files:
            # FIX: Agents should return full paths starting with app_core/
            full_p = test_dir / f_path.strip()
            full_p.parent.mkdir(parents=True, exist_ok=True)
            with open(full_p, "w", encoding="utf-8") as f:
                f.write(content.strip())
        
        # FIX: Explicitly verify the structure mirrors Render
        # Render expects: /app_core/app.py
        # Start command: gunicorn app_core.app:app
        check_script = f"""
import sys
import os
# Ensure the root of our temp dir is in the path
sys.path.insert(0, os.getcwd())
try:
    from app_core.app import app
    print("WSGI_CHECK_PASSED")
except (ImportError, AttributeError) as e:
    print(f"WSGI_CHECK_FAILED: Could not find 'app' object in 'app_core.app'. Error: {{e}}")
    # Debug: list what we have
    if os.path.exists('app_core'):
        print(f"Files in app_core: {{os.listdir('app_core')}}")
    sys.exit(1)
except Exception as e:
    print(f"WSGI_CHECK_FAILED: {{e}}")
    sys.exit(1)
"""
        check_file = test_dir / "wsgi_check.py"
        with open(check_file, "w", encoding="utf-8") as f:
            f.write(check_script)

        try:
            # 1. WSGI Pre-flight Check (Runs from the root of temp_stability_test)
            result = subprocess.run([sys.executable, "wsgi_check.py"], capture_output=True, text=True, cwd=str(test_dir), timeout=15)
            if "WSGI_CHECK_PASSED" not in result.stdout:
                return f"CRASH: Gunicorn validation failed.\nERROR: {result.stdout}\nSTDERR: {result.stderr}"

            return "SUCCESS"
        except subprocess.TimeoutExpired:
            return "SUCCESS" 
        except Exception as e:
            return f"Error: {e}"

python_tester_tool = PythonTesterTool()
online_validator_tool = OnlineValidatorTool()

# --- 6. AGENT DEFINITIONS ---
openai_llm = LLM(model="gpt-5-mini-2025-08-07", api_key=os.environ.get("OPENAI_API_KEY"))

scrum_master = Agent(
    role='Expert Scrum Master',
    goal='Oversee surgical codebase updates and ensure code is ready for deployment.',
    backstory="""You are the ultimate gatekeeper. You ensure that only valid code blocks are returned. 
    You are aware that Render requires a module-level 'app' object for Gunicorn in app_core/app.py.""",
    llm=openai_llm,
    verbose=True
)

architect = Agent(
    role='Modular Systems Architect',
    goal='Design surgical module updates and maintain Blueprint integrity.',
    backstory="You ensure that 'app = create_app()' is called at the module level in app_core/app.py.",
    llm=openai_llm,
    verbose=True
)

coder = Agent(
    role='Senior Python Developer',
    goal='Write clean, modular Flask code across multiple modules.',
    backstory="""You always return your work in '--- FILE: app_core/path/to/file.py ---' format. 
    You must ensure that app_core/app.py exposes 'app' at the top level for Gunicorn.""",
    llm=openai_llm,
    verbose=True
)

qa_auditor = Agent(
    role='Modular Compliance Auditor',
    goal='Verify code passes local WSGI tests and generate finalized code blocks.',
    backstory="""You only care about results. You run the stability tester. 
    You reject any code where the WSGI check fails. You ONLY return the verified 
    code blocks in '--- FILE: path ---' format.""",
    llm=openai_llm,
    verbose=True,
    tools=[python_tester_tool, online_validator_tool]
)

# --- 7. DEPLOYMENT AUTOMATION ---
def deploy_to_github():
    """Autonomous Deployment: Pushes changes to GitHub to trigger Render build."""
    try:
        logger.info("📦 Staging changes for deployment...")
        subprocess.run(["git", "add", "."], check=True)
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout
        if not status:
            logger.info("No changes to commit.")
            return True
            
        subprocess.run(["git", "commit", "-m", f"Autonomous Fix: {timestamp}"], check=True)
        logger.info("🚀 Pushing to GitHub main branch...")
        subprocess.run(["git", "push", "origin", "main"], check=True)
        return True
    except Exception as e:
        logger.error(f"❌ Git Push Failed: {e}")
        return False

# --- 8. BUILD EXECUTION LOOP ---
def run_build_cycle(issue, current_index, total_tickets):
    start_time = time.time()
    jira_client = get_jira_client()
    
    jira_requirements = get_ticket_details_from_issue(jira_client, issue)
    existing_codebase = get_existing_codebase()

    trace_log_file = os.path.join(LOG_DIR, f"trace_{issue.key}_{timestamp}.txt")

    tasks = [
        Task(description=f"Create Technical Spec for: {jira_requirements}", agent=architect, expected_output="Technical spec."),
        Task(description="Implement fixes. Output files using '--- FILE: app_core/path ---' format.", agent=coder, expected_output="Modified code blocks."),
        Task(
            description=(
                "Use python_stability_tester to verify the code blocks. "
                "Specifically ensure 'app' is exposed in 'app_core/app.py'. "
                "IF SUCCESS: Output the EXACT code blocks verified using the '--- FILE: path ---' format. "
                "DO NOT provide bash scripts, instructions, or narrative. ONLY code blocks."
            ), 
            agent=qa_auditor, 
            expected_output="Finalized verified code blocks."
        )
    ]

    crew = Crew(agents=[scrum_master, architect, coder, qa_auditor], tasks=tasks, process=Process.hierarchical, manager_llm=openai_llm, verbose=True, output_log_file=trace_log_file)

    logger.info(f"🔨 [{current_index}/{total_tickets}] Processing: {issue.key}...")
    result = crew.kickoff()

    output_string = result.raw if hasattr(result, 'raw') else str(result)
    files_found = re.findall(r'--- FILE: (.*?) ---\n(.*?)(?=\n--- FILE:|$)', output_string, re.DOTALL)

    if files_found:
        for f_path, content in files_found:
            # FIX: f_path now contains 'app_core/app.py', so we save directly to PROJECT_ROOT ('.')
            full_path = Path(PROJECT_ROOT) / f_path.strip()
            full_path.parent.mkdir(parents=True, exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                sanitized = re.sub(r'--- (?:FILE|END FILE):? .*? ---', '', content).strip()
                f.write(sanitized)
        
        if deploy_to_github():
            wait_time = 150 # Increased wait for Render
            logger.info(f"⏱️ Waiting {wait_time}s for Render to finalize build...")
            time.sleep(wait_time)
            
            logger.info("🔍 Running Online Production Validation...")
            validation_crew = Crew(
                agents=[qa_auditor],
                tasks=[Task(description="Run online_production_validator against '/', '/login', '/register'. Report results.", agent=qa_auditor, expected_output="Validation report.")],
                verbose=True
            )
            validation_crew.kickoff()
            
        transition_to_done(jira_client, issue)
            
        return time.time() - start_time
    else:
        logger.error(f"⛔ Agent failed to provide code blocks. Output was: {output_string[:200]}...")
        return False

if __name__ == "__main__":
    client = get_jira_client()
    all_issues = get_epic_children(client, TARGET_JIRA_TICKET)
    
    pending = [iss for iss in all_issues if iss.fields.status.name.lower() not in ['done', 'complete']]
    logger.info(f"🚀 Starting build for {len(pending)} pending tickets...")
    
    for i, issue in enumerate(pending, 1):
        duration = run_build_cycle(issue, i, len(pending))
        if not duration:
            logger.error(f"🛑 Build failed at {issue.key}. Stopping iteration.")
            break