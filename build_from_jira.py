import os
import re
import sys
import json
import subprocess
import logging
import codecs
import time
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
from crewai import Agent, Task, Crew, Process, LLM
from crewai.tools import BaseTool
from pydantic import BaseModel, Field
from jira import JIRA
from jira.exceptions import JIRAError

# --- 1. CONFIGURATION, LOGGING & WINDOWS UNICODE FIX ---
load_dotenv(override=True)

# Force UTF-8 for Windows Console to prevent 'charmap' errors with emojis
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

TARGET_JIRA_TICKET = os.environ.get("TARGET_JIRA_TICKET")
PROJECT_ROOT = "app_core"  # The directory containing the modular codebase
ENTRY_POINT = "app.py"     # The Flask application factory file
LOG_DIR = "agent_logs"

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(PROJECT_ROOT, exist_ok=True)

# Generate unique session log filename
log_ticket_id = re.sub(r'[^\w\s-]', '', TARGET_JIRA_TICKET if TARGET_JIRA_TICKET else "UNTITLED").strip().replace(' ', '_')
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = os.path.join(LOG_DIR, f"build_{log_ticket_id}_{timestamp}.log")

# Setup Handlers with UTF-8 encoding for reliable logging
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
    """Formats details for an issue, ensuring the Parent Epic context is retrieved."""
    summary = issue.fields.summary
    description = issue.fields.description or "No description provided."
    full_context = f"--- TARGET TICKET: {issue.key} ---\nTITLE: {summary}\nDESCRIPTION:\n{description}\n"

    # Hierarchy Check: Always pull the 'Constitution' from the Epic
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
    """Finds all child stories. Includes fix for 50-issue pagination limit."""
    try:
        issue = jira_client.issue(epic_id)
        if issue.fields.issuetype.name != 'Epic':
            return [issue]
        
        logger.info(f"📂 '{epic_id}' is an Epic. Searching for child stories...")
        # JQL search for all linked children. maxResults=200 ensures discovery of Story #51+
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
    """Automatically transitions a successful Jira issue to the 'Done' status."""
    try:
        transitions = jira_client.transitions(issue)
        target_transition = next((t for t in transitions if 'done' in t['name'].lower()), None)
        
        if target_transition:
            jira_client.transition_issue(issue, target_transition['id'])
            logger.info(f"✅ Issue {issue.key} transitioned to DONE.")
        else:
            logger.warning(f"⚠️ No 'Done' transition found for {issue.key}. Check workflow.")
    except JIRAError as e:
        logger.error(f"❌ Failed to transition {issue.key}: {e.text}")

# --- 3. RETRIEVE EXISTING CODEBASE (Modular Context) ---
def get_existing_codebase():
    """Aggregates all files in the PROJECT_ROOT for agent context."""
    code_map = ""
    if not os.path.exists(PROJECT_ROOT):
        return "CODEBASE IS EMPTY."
    
    for path in Path(PROJECT_ROOT).rglob('*.py'):
        relative_path = path.relative_to(PROJECT_ROOT)
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
            code_map += f"\n--- FILE: {relative_path} ---\n{content}\n"
    
    return code_map if code_map else "CODEBASE IS EMPTY."

# --- 4. THE QA TESTING TOOL (Stability Guarantee) ---
class PythonTesterSchema(BaseModel):
    codebase_payload: str = Field(..., description="The full proposed codebase update.")

class PythonTesterTool(BaseTool):
    name: str = "python_stability_tester"
    description: str = "Boots the MODULAR app entry point. Returns SUCCESS if stable for 10s."
    args_schema: type[BaseModel] = PythonTesterSchema

    def _run(self, codebase_payload: str) -> str:
        test_dir = "temp_stability_test"
        os.makedirs(test_dir, exist_ok=True)
        
        # Parse multi-file markers from the agent output
        files = re.findall(r'--- FILE: (.*?) ---\n(.*?)(?=\n--- FILE:|$)', codebase_payload, re.DOTALL)
        
        for file_path, content in files:
            full_path = Path(test_dir) / file_path.strip()
            full_path.parent.mkdir(parents=True, exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content.strip())
        
        entry_file = Path(test_dir) / ENTRY_POINT
        if not entry_file.exists():
            return f"CRASH: Missing entry point '{ENTRY_POINT}' in update."

        try:
            # Test booting the entire module with the current interpreter
            result = subprocess.run([sys.executable, str(entry_file)], capture_output=True, text=True, timeout=10)
            if result.returncode != 0: 
                return f"CRASH:\n{result.stderr[-500:]}"
            return "SUCCESS"
        except subprocess.TimeoutExpired:
            return "SUCCESS" # Web servers timeout by design
        except Exception as e:
            return f"Error: {e}"

python_tester_tool = PythonTesterTool()

# --- 5. THE MODELS ---
openai_llm = LLM(
    model="gpt-5-mini-2025-08-07",
    api_key=os.environ.get("OPENAI_API_KEY")
)

# --- 6. THE AGENTS (Orchestrated Scrum Team) ---

scrum_master = Agent(
    role='Expert Scrum Master',
    goal='Oversee the surgical integration of Jira tickets into a MODULAR codebase.',
    backstory="""You are the manager of a high-performing team. You ensure 
    the team respects the 'Modular Service Architecture' and 'ID Filter' rules. 
    You prevent context drift and ensure architectural integrity.""",
    llm=openai_llm,
    verbose=True
)

architect = Agent(
    role='Modular Systems Architect',
    goal='Design surgical updates across multiple Python modules.',
    backstory="""You design the path. You mandate SQLAlchemy naming conventions 
    (ix, uq, ck, fk, pk) and ensure CSRF protection is in place across all modules. 
    You identify exactly which files need to be modified.""",
    llm=openai_llm,
    verbose=True
)

coder = Agent(
    role='Senior Python Developer',
    goal='Implement modular Flask code across multiple files.',
    backstory="""You write robust, decoupled Python. You return updates as 
    file blocks starting with '--- FILE: path ---'. You never truncate existing 
    logic and ensure imports are syntactically correct.""",
    llm=openai_llm,
    verbose=True
)

qa_auditor = Agent(
    role='Modular Compliance Auditor',
    goal='Verify the codebase update passes stability and security audits.',
    backstory="""You are the gatekeeper. You run the stability tester and 
    ensure all security guardrails (CSRF, ID filters) are maintained. 
    You reject any code containing AI artifacts like '--- END FILE ---'.""",
    llm=openai_llm,
    verbose=True,
    tools=[python_tester_tool]
)

# --- 7. THE BUILD EXECUTION LOOP ---
def run_build_cycle(issue, current_index, total_tickets):
    """Executes the hierarchical build for a single pending ticket."""
    start_time = time.time()
    jira_client = get_jira_client()
    jira_requirements = get_ticket_details_from_issue(jira_client, issue)
    existing_codebase = get_existing_codebase()

    # Create trace log for internal agent dialogue review
    trace_log_file = os.path.join(LOG_DIR, f"trace_{issue.key}_{timestamp}.txt")

    blueprint_task = Task(
        description=(
            f"Requirements:\n{jira_requirements}\n\nExisting Codebase:\n{existing_codebase}\n\n"
            "TASK: Create a SURGICAL Blueprint identifying specific module changes."
        ),
        expected_output="A technical specification for the modular update.",
        agent=architect
    )

    coding_task = Task(
        description=(
            "Implement the update using Blueprints. Return full content of modified files.\n"
            "Format: --- FILE: path/to/file.py ---\n[content]\n\n"
            "MANDATORY: Do not include markers like '--- END FILE ---' inside the code content."
        ),
        expected_output="The complete codebase update with FILE markers.",
        agent=coder,
        context=[blueprint_task]
    )

    audit_task = Task(
        description="Run stability tests and verify security guardrails. Output ONLY the finalized code blocks.",
        expected_output="The finalized modular codebase update.",
        agent=qa_auditor,
        context=[coding_task]
    )

    # Execute Hierarchical Process
    crew = Crew(
        agents=[scrum_master, architect, coder, qa_auditor],
        tasks=[blueprint_task, coding_task, audit_task],
        process=Process.hierarchical,
        manager_llm=openai_llm,
        verbose=True,
        output_log_file=trace_log_file
    )

    logger.info(f"🔨 [{current_index}/{total_tickets}] Processing ticket: {issue.key}...")
    result = crew.kickoff()

    # Parse Multi-File Output and Save to PROJECT_ROOT
    output_string = result.raw if hasattr(result, 'raw') else str(result)
    files_found = re.findall(r'--- FILE: (.*?) ---\n(.*?)(?=\n--- FILE:|$)', output_string, re.DOTALL)

    if files_found:
        for file_path, content in files_found:
            full_path = Path(PROJECT_ROOT) / file_path.strip()
            full_path.parent.mkdir(parents=True, exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                # Sanitization: Ensure no AI markers leaked into the final save
                sanitized = content.replace("--- END FILE", "# END FILE").strip()
                f.write(sanitized)
        
        # Mark as complete in Jira
        transition_to_done(jira_client, issue)
        
        duration = time.time() - start_time
        logger.info(f"✅ Issue {issue.key} integrated. Cycle took {duration:.2f}s.")
        return duration
    else:
        logger.error(f"⛔ Failed to isolate code blocks for {issue.key}.")
        return False

# --- 8. MAIN ENTRY POINT ---
if __name__ == "__main__":
    if not TARGET_JIRA_TICKET:
        logger.error("❌ TARGET_JIRA_TICKET not found in .env.")
        sys.exit(1)

    client = get_jira_client()
    all_issues = get_epic_children(client, TARGET_JIRA_TICKET)
    
    # Filter: Skip stories already marked as 'Done' or 'Complete'
    tickets_to_process = [iss for iss in all_issues if iss.fields.status.name.lower() not in ['done', 'complete']]
    total = len(tickets_to_process)
    skipped = len(all_issues) - total

    if skipped > 0:
        logger.info(f"⏭️ Skipped {skipped} completed tickets.")

    if total == 0:
        logger.info("🎉 No pending tickets found. Backlog is clear!")
        sys.exit(0)

    logger.info(f"🚀 Starting build for {total} pending tickets...")
    
    overall_start_time = time.time()
    cycle_durations = []

    for i, issue in enumerate(tickets_to_process, 1):
        if i > 1:
            avg = sum(cycle_durations) / len(cycle_durations)
            eta = str(timedelta(seconds=int(avg * (total - (i-1)))))
            logger.info(f"📊 Progress: {((i-1)/total)*100:.1f}% | Avg: {avg:.1f}s | ETA: {eta}")

        duration = run_build_cycle(issue, i, total)
        if duration:
            cycle_durations.append(duration)
        else:
            logger.error(f"🛑 Build failed at {issue.key}. Stopping iteration to prevent codebase drift.")
            break

    total_time = str(timedelta(seconds=int(time.time() - overall_start_time)))
    logger.info(f"🏁 Finished. Total Run Time: {total_time}")