import os
import json
import re
from dotenv import load_dotenv
from crewai import Agent, Task, Crew, Process, LLM
from jira import JIRA
from jira.exceptions import JIRAError

# --- 1. CONFIGURATION & SETUP ---
load_dotenv()

EPIC_REQUEST = """Build a high-performance URL Shortening web service (similar to Bitly). 
The system must achieve three specific things:
1. A secure API and UI where a user can submit a long URL and receive a unique, shortened link.
2. A blazing-fast redirect engine that intercepts the short link, looks up the original URL in the database, and executes an HTTP 301 redirect.
3. A basic analytics tracking system that increments a counter every time a short link is clicked, displayable on a simple dashboard."""

# --- 2. THE MODELS (Cerebras Approach) ---
cerebras_llm = LLM(
    model='openai/llama3.1-8b', 
    api_key=os.environ.get("CEREBRAS_API_KEY"),
    base_url="https://api.cerebras.ai/v1"
)

# --- 3. THE AGENTS ---
product_manager = Agent(
    role='Director of Product Management',
    goal='Draft exhaustive, multi-paragraph Epics and Stories, logging all scoping decisions.',
    backstory="You never write minimal tickets. Your descriptions are highly detailed. You clearly separate Registration, Login, and Password Reset into 3 distinct User Stories. You log your reasoning for why certain features are included or excluded.",
    llm=cerebras_llm,
    verbose=True,
    allow_delegation=False
)

tech_lead = Agent(
    role='Lead Identity Systems Architect',
    goal='Iteratively review the PM draft, vastly expanding technical constraints and logging architectural decisions.',
    backstory="You act as the second pass. You take the PM's draft and aggressively expand it. You mandate specific database schemas, explicit REST API payloads, and JWT structures. You log your architectural decisions (e.g., 'Why I chose bcrypt over Argon2') so they can be saved as comments.",
    llm=cerebras_llm,
    verbose=True,
    allow_delegation=False
)

qa_engineer = Agent(
    role='Agile Delivery Manager & QA',
    goal='Perform the final review, append exhaustive Acceptance Criteria, and format the strict JSON payload.',
    backstory="You act as the final iterative pass. You take the Tech Lead's massive document, review it for gaps, and append strict Given/When/Then criteria. You compile all thoughts/decisions from the PM and Tech Lead into a 'comments' array. You output ONLY flawless JSON.",
    llm=cerebras_llm,
    verbose=True,
    allow_delegation=False
)

# --- 4. THE TASKS (Iterative Flow) ---
draft_epic = Task(
    description=(
        f"Analyze this request: '{EPIC_REQUEST}'.\n"
        "1. Draft an exhaustive Epic description (at least 3 paragraphs).\n"
        "2. Draft exactly 3 distinct User Stories (Registration, Login, Password Reset).\n"
        "3. Include a 'PM_Decisions' section logging your thoughts on scoping."
    ),
    expected_output='A highly detailed Epic and 3 User Stories, including PM decisions.',
    agent=product_manager
)

add_tech_specs = Task(
    description=(
        "Review the PM's draft from the previous step.\n"
        "1. Massively expand the description of EACH of the 3 stories with deep Technical Specifications (API routes, DB tables, security headers).\n"
        "2. Include a 'Tech_Decisions' section logging your thoughts on the architecture chosen."
    ),
    expected_output='The expanded Epic and 3 User Stories, now with deep technical specs and Tech decisions.',
    agent=tech_lead
)

finalize_ticket = Task(
    description=(
        "Review the expanded draft from the Tech Lead.\n"
        "1. Append exhaustive Acceptance Criteria (Given/When/Then) to each story.\n"
        "2. Add your own 'QA_Decisions' logging your testing strategy.\n"
        "3. You MUST format your ENTIRE final output as a raw JSON object matching this schema exactly:\n"
        "{\n"
        '  "epic": {\n'
        '    "title": "Epic Title Here",\n'
        '    "description": "Massive Epic description here...",\n'
        '    "comments": ["PM Decision: ...", "Tech Decision: ...", "QA Decision: ..."]\n'
        '  },\n'
        '  "stories": [\n'
        '    {\n'
        '      "title": "Story Title",\n'
        '      "description": "Exhaustive story text including narrative and Tech Specs",\n'
        '      "acceptanceCriteria": [{"given": "...", "when": "...", "then": "..."}],\n'
        '      "comments": ["Tech Decision on DB: ...", "QA Strategy on Edge Cases: ..."]\n'
        '    }\n'
        '  ]\n'
        "}\n"
        "OUTPUT ONLY VALID JSON. Do not use markdown tags."
    ),
    expected_output='A strict JSON object containing the exhaustive Epic, Stories, Criteria, and arrays of iterative decisions/comments.',
    agent=qa_engineer
)

# --- 5. EXECUTION ---
scrum_crew = Crew(
    agents=[product_manager, tech_lead, qa_engineer],
    tasks=[draft_epic, add_tech_specs, finalize_ticket],
    process=Process.sequential, 
    verbose=True
)

print(f"🚀 Starting Iterative Agile Breakdown via Cerebras...\n")
raw_result = scrum_crew.kickoff()

print("\n✅ CrewAI processing complete. Parsing JSON and Pushing to Jira...\n")

# --- 6. BULLETPROOF JIRA INTEGRATION ---
def push_to_jira(raw_json_text):
    jira_server = os.environ.get("JIRA_SERVER")
    jira_email = os.environ.get("JIRA_EMAIL")
    jira_token = os.environ.get("JIRA_API_TOKEN")
    project_key = os.environ.get("JIRA_PROJECT_KEY")

    if not all([jira_server, jira_email, jira_token, project_key]):
        print("⚠️ Missing Jira credentials in .env file.")
        return

    # Safely extract the raw string from CrewAI's output object
    raw_string = raw_json_text.raw if hasattr(raw_json_text, 'raw') else str(raw_json_text)
    
    # Use Regex to isolate ONLY the JSON block (ignores conversational text the LLM might append)
    match = re.search(r'\{.*\}', raw_string, re.DOTALL)
    if not match:
        print(f"⛔️ Could not isolate JSON from the LLM output.\nRaw Output:\n{raw_string}")
        return
        
    json_str = match.group(0)

    try:
        # Parse the JSON safely
        agile_data = json.loads(json_str, strict=False)
    except json.JSONDecodeError as e:
        print(f"⛔️ Failed to parse LLM output as JSON: {e}\nIsolated JSON:\n{json_str}")
        return

    try:
        print("🔗 Connecting to Jira Cloud...")
        jira_client = JIRA(options={'server': jira_server}, basic_auth=(jira_email, jira_token))
        
        # --- Create Epic ---
        print(f"📝 Creating EPIC in project '{project_key}'...")
        epic_dict = {
            'project': {'key': project_key},
            'summary': agile_data.get('epic', {}).get('title', 'CIAM Implementation')[:255],
            'description': agile_data.get('epic', {}).get('description', ''),
            'issuetype': {'name': 'Epic'},
            'customfield_10011': agile_data.get('epic', {}).get('title', 'CIAM Implementation')[:255] 
        }
        
        try:
            epic_issue = jira_client.create_issue(fields=epic_dict)
        except JIRAError:
            epic_dict.pop('customfield_10011', None)
            epic_issue = jira_client.create_issue(fields=epic_dict)
            
        print(f"   ✅ Epic Created: {epic_issue.key}")

        # Post Epic Comments
        epic_comments = agile_data.get('epic', {}).get('comments', [])
        for comment in epic_comments:
            jira_client.add_comment(epic_issue.key, comment)

        # --- Create Child Stories ---
        child_issue_keys = []
        for i, story in enumerate(agile_data.get('stories', []), 1):
            print(f"📝 Creating Child Story {i}...")
            
            # Format the description cleanly
            desc = story.get('description', '')
            
            # If the LLM put Acceptance Criteria in a separate array, format it into Markdown!
            ac_data = story.get('acceptanceCriteria', [])
            if ac_data:
                desc += "\n\n*Acceptance Criteria:*\n"
                for ac in ac_data:
                    if isinstance(ac, dict):
                        desc += f"* *Given* {ac.get('given', '')}\n* *When* {ac.get('when', '')}\n* *Then* {ac.get('then', '')}\n\n"
                    else:
                        desc += f"* {ac}\n"
            
            story_dict = {
                'project': {'key': project_key},
                'summary': story.get('title', f"Story {i}")[:255],
                'description': desc,
                'issuetype': {'name': 'Story'},
            }
            story_issue = jira_client.create_issue(fields=story_dict)
            child_issue_keys.append(story_issue.key)
            print(f"   ✅ Story Created: {story_issue.key}")

            # Post Story Comments (The Agents' Thoughts/Decisions)
            story_comments = story.get('comments', [])
            for comment in story_comments:
                jira_client.add_comment(story_issue.key, comment)

        # --- Link Hierarchy ---
        print("🔗 Linking Stories to Epic parent...")
        try:
            jira_client.add_issues_to_epic(epic_issue.id, child_issue_keys)
        except JIRAError:
            for child_key in child_issue_keys:
                issue = jira_client.issue(child_key)
                issue.update(fields={'parent': {'key': epic_issue.key}})

        print(f"\n🎉 SUCCESS! Rich hierarchy created with agent thought-logs.")
        print(f"👉 View your Epic here: {jira_server}/browse/{epic_issue.key}")

    except JIRAError as e:
        print(f"\n⛔️ Jira API Error: {e.text}")
    except Exception as e:
        print(f"\n⛔️ Unexpected Error: {e}")

push_to_jira(raw_result)