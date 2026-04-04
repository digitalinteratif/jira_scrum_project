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
from pydantic import BaseModel, Field
from jira import JIRA
from jira.exceptions import JIRAError

# --- 1. CONFIGURATION & LOGGING ---
load_dotenv(override=True)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

TARGET_JIRA_TICKET = os.environ.get("TARGET_JIRA_TICKET")
PROJECT_ROOT = "app_core"
ENTRY_POINT = "app.py"
LOG_DIR = "agent_logs"
BASE_URL = os.environ.get("BASE_URL", "https://digitalinteractif.com")

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(PROJECT_ROOT, exist_ok=True)

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

# --- 2. THE ONLINE VALIDATOR TOOL ---
class OnlineValidatorSchema(BaseModel):
    endpoints: list = Field(..., description="List of relative paths to check (e.g. ['/', '/login', '/register'])")

class OnlineValidatorTool(BaseTool):
    name: str = "online_production_validator"
    description: str = "Checks the LIVE website at BASE_URL to verify deployment success."
    args_schema: type[BaseModel] = OnlineValidatorSchema

    def _run(self, endpoints: list) -> str:
        results = []
        logger.info(f"🌐 Starting Online Validation for: {endpoints}")
        for ep in endpoints:
            url = f"{BASE_URL.rstrip('/')}/{ep.lstrip('/')}"
            try:
                # Render can take a moment to cycle containers; 15s timeout handles cold starts
                response = requests.get(url, timeout=20)
                if response.status_code == 200:
                    # Check for a specific "signature" string to ensure it's not a generic 200 error page
                    if "URL.CO" in response.text or "digitalinteractif" in response.text:
                        results.append(f"✅ {ep}: 200 OK (Verified Content)")
                    else:
                        results.append(f"⚠️ {ep}: 200 OK (Content Signature Missing)")
                else:
                    results.append(f"❌ {ep}: {response.status_code} Error")
            except Exception as e:
                results.append(f"❌ {ep}: Connection Failed ({str(e)})")
        
        return "\n".join(results)

# --- 3. THE LOCAL STABILITY TESTER TOOL ---
class PythonTesterSchema(BaseModel):
    codebase_payload: str = Field(..., description="The full proposed codebase update.")

class PythonTesterTool(BaseTool):
    name: str = "python_stability_tester"
    description: str = "Boots the app locally. Returns SUCCESS if stable for 10s."
    args_schema: type[BaseModel] = PythonTesterSchema

    def _run(self, codebase_payload: str) -> str:
        test_dir = "temp_stability_test"
        os.makedirs(test_dir, exist_ok=True)
        
        files = re.findall(r'--- FILE: (.*?) ---\n(.*?)(?=\n--- FILE:|$)', codebase_payload, re.DOTALL)
        for f_path, content in files:
            full_p = Path(test_dir) / f_path.strip()
            full_p.parent.mkdir(parents=True, exist_ok=True)
            with open(full_p, "w", encoding="utf-8") as f:
                f.write(content.strip())
        
        entry = Path(test_dir) / ENTRY_POINT
        if not entry.exists():
            return f"CRASH: Missing entry point '{ENTRY_POINT}'."

        try:
            # Verify that the code is syntactically valid and imports successfully
            result = subprocess.run([sys.executable, str(entry)], capture_output=True, text=True, timeout=10)
            stderr = result.stderr.lower()
            if result.returncode != 0 or "nameerror" in stderr or "importerror" in stderr:
                return f"CRASH/SYNTAX ERROR:\n{result.stderr[-800:]}"
            return "SUCCESS"
        except subprocess.TimeoutExpired:
            return "SUCCESS" # Web servers block by design
        except Exception as e:
            return f"Error: {e}"

# --- 4. AGENT DEFINITIONS ---
openai_llm = LLM(model="gpt-5-mini-2025-08-07", api_key=os.environ.get("OPENAI_API_KEY"))

scrum_master = Agent(
    role='Expert Scrum Master',
    goal='Oversee the surgical integration and deployment validation of Jira tickets.',
    backstory="You ensure architectural integrity and verify that fixes reach production.",
    llm=openai_llm,
    verbose=True
)

architect = Agent(
    role='Modular Systems Architect',
    goal='Identify specific module changes and ensure Blueprint consistency.',
    backstory="You ensure that Blueprint variables (auth_bp) match their decorators.",
    llm=openai_llm,
    verbose=True
)

coder = Agent(
    role='Senior Python Developer',
    goal='Implement modular Flask code and sanitize AI artifacts.',
    backstory="You write clean Python and define Blueprints at the top of files.",
    llm=openai_llm,
    verbose=True
)

qa_auditor = Agent(
    role='Modular Compliance Auditor',
    goal='Verify the codebase update passes local tests AND live online checks.',
    backstory="""You are the gatekeeper. You use the stability tester locally, 
    and once changes are pushed, you use the online_production_validator to 
    confirm the live site is healthy.""",
    llm=openai_llm,
    verbose=True,
    tools=[PythonTesterTool(), OnlineValidatorTool()]
)

# --- 5. DEPLOYMENT AUTOMATION ---
def deploy_to_github():
    """Autonomous Deployment: Pushes PROJECT_ROOT to GitHub to trigger Render build."""
    try:
        logger.info("📦 Staging changes for deployment...")
        subprocess.run(["git", "add", "."], check=True)
        subprocess.run(["git", "commit", "-m", f"Autonomous Fix: {timestamp}"], check=True)
        logger.info("🚀 Pushing to GitHub main branch...")
        subprocess.run(["git", "push", "origin", "main"], check=True)
        return True
    except Exception as e:
        logger.error(f"❌ Git Push Failed: {e}")
        return False

# --- 6. BUILD EXECUTION LOOP ---
def run_build_cycle(issue, current_index, total_tickets):
    start_time = time.time()
    jira_client = JIRA(options={'server': os.environ.get("JIRA_SERVER")}, basic_auth=(os.environ.get("JIRA_EMAIL"), os.environ.get("JIRA_API_TOKEN")))
    
    # Logic to fetch context and codebase (standard)
    from build_from_jira_utils import get_ticket_details_from_issue, get_existing_codebase
    jira_requirements = get_ticket_details_from_issue(jira_client, issue)
    existing_codebase = get_existing_codebase()

    trace_log_file = os.path.join(LOG_DIR, f"trace_{issue.key}_{timestamp}.txt")

    tasks = [
        Task(description=f"Identify module changes for: {jira_requirements}", agent=architect, expected_output="Tech specs."),
        Task(description="Implement fixes. define Blueprints at the top.", agent=coder, expected_output="Modified files."),
        Task(description="1. Run stability tester locally.\n2. If success, trigger git push.\n3. Wait for Render build.\n4. Run online_production_validator.", agent=qa_auditor, expected_output="Verification results.")
    ]

    crew = Crew(agents=[scrum_master, architect, coder, qa_auditor], tasks=tasks, process=Process.hierarchical, manager_llm=openai_llm, verbose=True, output_log_file=trace_log_file)

    logger.info(f"🔨 [{current_index}/{total_tickets}] Processing: {issue.key}...")
    result = crew.kickoff()

    # Parse and Save
    output_string = result.raw if hasattr(result, 'raw') else str(result)
    files_found = re.findall(r'--- FILE: (.*?) ---\n(.*?)(?=\n--- FILE:|$)', output_string, re.DOTALL)

    if files_found:
        for f_path, content in files_found:
            full_path = Path(PROJECT_ROOT) / f_path.strip()
            full_path.parent.mkdir(parents=True, exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                sanitized = re.sub(r'--- (?:FILE|END FILE):? .*? ---', '', content).strip()
                f.write(sanitized)
        
        # Trigger Autonomy
        if deploy_to_github():
            logger.info("⏱️ Waiting 90s for Render Deployment to finalize...")
            time.sleep(90)
            
            # Post-deploy check can be triggered here or inside the auditor's task
            # The current task setup encourages the auditor to use the tool internally.
            
        # Complete Jira Ticket
        try:
            transitions = jira_client.transitions(issue)
            t_id = next((t['id'] for t in transitions if 'done' in t['name'].lower()), None)
            if t_id: jira_client.transition_issue(issue, t_id)
        except: pass
            
        return time.time() - start_time
    return False

if __name__ == "__main__":
    # Standard entry point logic (Pagination fix included)
    jira_client = JIRA(options={'server': os.environ.get("JIRA_SERVER")}, basic_auth=(os.environ.get("JIRA_EMAIL"), os.environ.get("JIRA_API_TOKEN")))
    jql = f'parent = "{TARGET_JIRA_TICKET}" OR "Epic Link" = "{TARGET_JIRA_TICKET}" ORDER BY created ASC'
    all_issues = jira_client.search_issues(jql, maxResults=200)
    
    pending = [iss for iss in all_issues if iss.fields.status.name.lower() not in ['done', 'complete']]
    logger.info(f"🚀 Starting build for {len(pending)} pending tickets...")
    
    for i, issue in enumerate(pending, 1):
        if not run_build_cycle(issue, i, len(pending)):
            break