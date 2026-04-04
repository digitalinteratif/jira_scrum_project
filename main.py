import os
import json
import re
import logging
import sys
from datetime import datetime
from dotenv import load_dotenv
from crewai import Agent, Task, Crew, Process, LLM
from jira import JIRA
from jira.exceptions import JIRAError

# --- 1. CONFIGURATION & SETUP ---
# override=True ensures the .env file values win over existing system environment variables
load_dotenv(override=True)

# Jira Configuration
TARGET_JIRA_TICKET = os.environ.get("TARGET_JIRA_TICKET") # The Epic ID to deconstruct
JIRA_SERVER = os.environ.get("JIRA_SERVER")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL")
JIRA_TOKEN = os.environ.get("JIRA_API_TOKEN")
JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY")

# --- 2. THE MODELS (Native OpenAI API) ---
# Ensure OPENAI_API_KEY is present in your .env file
# Using the specific requested model for high reasoning and context management
openai_llm = LLM(
    model="gpt-5-mini-2025-08-07",
    api_key=os.environ.get("OPENAI_API_KEY")
)

# Initial validation of required environment variables
if not os.environ.get("OPENAI_API_KEY"):
    print("❌ ERROR: OPENAI_API_KEY not found in .env file.")
    exit(1)

if not TARGET_JIRA_TICKET:
    print("❌ ERROR: TARGET_JIRA_TICKET (The Parent Epic) not found in .env.")
    exit(1)

# --- 3. FETCH THE EPIC DETAILS ---
def get_epic_context(ticket_id):
    print(f"🔗 Fetching Epic {ticket_id} from Jira...")
    jira_client = JIRA(options={'server': JIRA_SERVER}, basic_auth=(JIRA_EMAIL, JIRA_TOKEN))
    try:
        issue = jira_client.issue(ticket_id)
        if issue.fields.issuetype.name != 'Epic':
            print(f"⚠️ Warning: {ticket_id} is a {issue.fields.issuetype.name}, not an Epic.")
        return {
            "id": issue.id,
            "key": issue.key,
            "summary": issue.fields.summary,
            "description": issue.fields.description or "No description."
        }
    except JIRAError as e:
        print(f"⛔️ Jira Error: {e.text}")
        exit(1)

epic_context = get_epic_context(TARGET_JIRA_TICKET)

# --- 4. THE AGENTS (ORCHESTRATED SCRUM TEAM) ---

product_owner = Agent(
    role='Expert Product Owner',
    goal=f'Define a comprehensive, non-overlapping roadmap of user stories for Epic {epic_context["key"]}.',
    backstory="""You are a world-class Product Owner. You excel at taking a high-level 
    vision and deconstructing it into a logical flow of independent, valuable user stories. 
    You are obsessive about context; you ensure that Story B builds upon Story A 
    without duplicating requirements. You drive the definition of 'What' and 'Why'.""",
    llm=openai_llm,
    verbose=True
)

tech_architect = Agent(
    role='Senior Technical Architect',
    goal='Ensure every user story is technically sound, secure, and architecturally consistent.',
    backstory="""You provide the 'How' for the Scrum team. You review the PO's stories 
    and inject deep technical considerations. You ensure that the database schemas, 
    API designs, and security protocols discussed in previous stories are maintained 
    as context for the current one. You prevent technical debt before it's written.""",
    llm=openai_llm,
    verbose=True
)

qa_lead = Agent(
    role='Quality Assurance Lead',
    goal='Define rigorous acceptance criteria and testing strategies for the backlog.',
    backstory="""You are the gatekeeper of quality. You ensure that every story has 
    'Given/When/Then' acceptance criteria that are actually testable. You look at 
    previously defined stories to ensure integration testing is considered as the 
    feature set grows. You define the 'Definition of Done'.""",
    llm=openai_llm,
    verbose=True
)

# --- 5. THE TASKS (ITERATIVE REFINEMENT) ---

roadmap_task = Task(
    description=(
        f"Analyze the following Epic: {epic_context['summary']}\n"
        f"DESCRIPTION: {epic_context['description']}\n\n"
        "TASK 1: Generate a 'Master Backlog Inventory'. This is a list of all required "
        "User Stories to fully realize the Epic. Ensure a logical sequence (Foundational first). "
        "Avoid creating a massive single story; break them into granular, valuable units."
    ),
    expected_output='A prioritized inventory of User Story titles and high-level summaries.',
    agent=product_owner
)

detailing_task = Task(
    description=(
        "Review the Master Backlog Inventory. For each story identified:\n"
        "1. Write a formal 'User Story Statement' (As a... I want... So that...).\n"
        "2. Define specific 'Functional Requirements'.\n"
        "3. Cross-reference with prior stories to ensure no functional gaps or overlaps.\n"
        "4. Ensure requirements reflect the high-level direction in the EPIC description."
    ),
    expected_output='A detailed draft of all User Stories with statements and requirements.',
    agent=product_owner,
    context=[roadmap_task]
)

technical_refinement_task = Task(
    description=(
        "Refine the detailed stories with Technical Specifications.\n"
        "1. For each story, provide 'Technical Considerations' (e.g., specific endpoints, "
        "logic handlers, or DB interactions).\n"
        "2. Ensure the technical path is consistent across all stories in the set."
    ),
    expected_output='Detailed User Stories now including Technical Considerations.',
    agent=tech_architect,
    context=[detailing_task]
)

finalization_task = Task(
    description=(
        "Perform the final quality pass on the entire deconstructed set.\n"
        "1. Append 'Acceptance Criteria' (Given/When/Then) to every story.\n"
        "2. Append 'Testing Considerations' (Unit, Integration, and Edge Case strategies).\n"
        "3. Format the entire output as a raw JSON object matching this schema exactly:\n"
        "{\n"
        '  "stories": [\n'
        '    {\n'
        '      "title": "Short descriptive title",\n'
        '      "statement": "As a... I want... So that...",\n'
        '      "acceptance_criteria": "...",\n'
        '      "requirements": "...",\n'
        '      "technical_considerations": "...",\n'
        '      "testing_considerations": "..."\n'
        '    }\n'
        '  ]\n'
        "}\n"
        "OUTPUT ONLY RAW JSON. No markdown tags or conversational text."
    ),
    expected_output='A strict JSON object of deconstructed, high-quality user stories.',
    agent=qa_lead,
    context=[technical_refinement_task]
)

# --- 6. EXECUTION (HIERARCHICAL ORCHESTRATION) ---
# We use Process.hierarchical to allow a manager agent to maintain context across the tasks
crew = Crew(
    agents=[product_owner, tech_architect, qa_lead],
    tasks=[roadmap_task, detailing_task, technical_refinement_task, finalization_task],
    process=Process.hierarchical,
    manager_llm=openai_llm, # The manager ensures context is passed effectively between agents
    verbose=True
)

print(f"🚀 Scrum Team is deconstructing Epic {TARGET_JIRA_TICKET} iteratively...\n")
raw_result = crew.kickoff()

# --- 7. PUSH TO JIRA ---
def push_stories_to_jira(raw_json_text, parent_epic):
    print("\n✅ Validating Output and Pushing child stories to Jira...")
    
    raw_string = raw_json_text.raw if hasattr(raw_json_text, 'raw') else str(raw_json_text)
    match = re.search(r'\{.*\}', raw_string, re.DOTALL)
    if not match:
        print("⛔️ Error: Agent failed to provide valid JSON. Check build logs.")
        return

    try:
        agile_data = json.loads(match.group(0), strict=False)
        jira_client = JIRA(options={'server': JIRA_SERVER}, basic_auth=(JIRA_EMAIL, JIRA_TOKEN))
        
        stories = agile_data.get('stories', [])
        print(f"📦 Found {len(stories)} stories to process.")

        for i, s in enumerate(stories, 1):
            # Format the comprehensive description using Jira Heading syntax
            full_description = (
                f"h2. User Story Statement\n{s.get('statement')}\n\n"
                f"h2. Acceptance Criteria\n{s.get('acceptance_criteria')}\n\n"
                f"h2. Requirements\n{s.get('requirements')}\n\n"
                f"h2. Technical Considerations\n{s.get('technical_considerations')}\n\n"
                f"h2. Testing Considerations\n{s.get('testing_considerations')}"
            )

            story_dict = {
                'project': {'key': JIRA_PROJECT_KEY},
                'summary': s.get('title', f"Story {i}")[:255],
                'description': full_description,
                'issuetype': {'name': 'Story'},
                'parent': {'key': parent_epic['key']} 
            }

            print(f"📝 Creating Story {i}/{len(stories)}: {s.get('title')}...")
            new_story = jira_client.create_issue(fields=story_dict)
            print(f"   ✅ Created: {new_story.key}")

        print(f"\n🎉 Process Complete! {len(stories)} stories linked to {parent_epic['key']}.")

    except Exception as e:
        print(f"⛔️ Error during Jira Upload: {e}")

if __name__ == "__main__":
    push_stories_to_jira(raw_result, epic_context)