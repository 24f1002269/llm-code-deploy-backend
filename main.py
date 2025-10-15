# main.py
from fastapi import FastAPI, HTTPException, Request
import os, requests
from datetime import datetime
from github import Github
import openai

# ----------------------------- CONFIG --------------------------------
app = FastAPI()

SECRET_KEY = os.environ.get("SECRET_KEY", "testsecret")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
AIPIPE_KEY = os.environ.get("AIPIPE_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", AIPIPE_KEY)
openai.api_key = OPENAI_API_KEY

# ----------------------------- HELPERS --------------------------------

def call_llm_generate_code(brief: str, checks: list = None, attachments: list = None,
                           existing_code: str = "", round_num: int = 1):
    """
    Generates app code using LLM (OpenAI or compatible AIPIPE endpoint).
    Returns dict of {filename: content}.
    """
    checks_text = "\n".join(checks or [])
    attach_text = "\n".join([f"- {a}" for a in attachments or []])

    system_prompt = (
        "You are an expert full-stack web engineer. "
        "Generate minimal self-contained front-end code (HTML + optional CSS + JS) "
        "for the described app. Do NOT include explanations or markdown fences. "
        "Output only raw code for index.html and related static files."
    )

    user_prompt = f"""
### Task Brief
{brief}

### Validation Checks
{checks_text}

### Attachments / Notes
{attach_text}

### Round Number
{round_num}

### Existing Code (if continuing iteration)
{existing_code[:6000]}
"""

    print("\n🔹 Sending prompt to LLM ...")

    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.3,
    )

    html_code = response.choices[0].message["content"].strip()

    # Basic sanity check
    if "<html" not in html_code.lower():
        html_code = f"<!DOCTYPE html><html><body><pre>{html_code}</pre></body></html>"

    return {"index.html": html_code}


def create_repo_and_push(github_token: str, task_brief: str, code_files: dict):
    """
    Create new GitHub repo, upload files, enable Pages, return repo + URLs.
    """
    g = Github(github_token)
    user = g.get_user()
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    repo_name = f"llm-app-{timestamp}"
    repo = user.create_repo(repo_name, private=False)
    print(f"✅ Created repo: {repo_name}")

    for path, content in code_files.items():
        repo.create_file(path, f"Add {path}", content)

    readme_content = f"# {task_brief}\n\nGenerated automatically via LLM pipeline."
    repo.create_file("README.md", "Add README", readme_content)

    # Enable GitHub Pages
    pages_api_url = f"https://api.github.com/repos/{user.login}/{repo_name}/pages"
    headers = {"Authorization": f"token {github_token}", "Accept": "application/vnd.github+json"}
    payload = {"source": {"branch": "main", "path": "/"}}
    try:
        r = requests.post(pages_api_url, headers=headers, json=payload)
        if r.status_code in [201, 204]:
            print("✅ GitHub Pages enabled successfully.")
        else:
            print(f"⚠️ GitHub Pages setup failed: {r.status_code}, {r.text}")
    except Exception as e:
        print("⚠️ Exception enabling GitHub Pages:", e)

    pages_url = f"https://{user.login}.github.io/{repo_name}/"
    return repo, repo.html_url, pages_url


def notify_evaluation(evaluation_url, payload):
    try:
        r = requests.post(evaluation_url, json=payload, timeout=15)
        print(f"Evaluation POST → {r.status_code}")
        return r.status_code == 200
    except Exception as e:
        print("⚠️ Evaluation callback failed:", e)
        return False


# ----------------------------- ROUTES --------------------------------

@app.get("/")
async def root():
    return {"status": "ok", "message": "FastAPI LLM deploy server running."}


@app.post("/api-endpoint")
async def handle_request(request: Request):
    data = await request.json()
    print("\n===== Incoming Task =====")
    print(data)
    print("=========================\n")

    # Verify secret
    if data.get("secret") != SECRET_KEY:
        raise HTTPException(status_code=403, detail="Invalid secret key")

    brief = data.get("brief", "")
    email = data.get("email", "")
    task = data.get("task", "task-unknown")
    round_num = int(data.get("round", 1))
    nonce = data.get("nonce", "")
    checks = data.get("checks", [])
    attachments = data.get("attachments", [])
    evaluation_url = data.get("evaluation_url")

    # Optional: handle continuation between rounds (skipped for simplicity)
    existing_code = ""

    # --- LLM: Generate app code ---
    try:
        code_files = call_llm_generate_code(
            brief, checks, attachments, existing_code, round_num
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LLM generation failed: {e}")

    # --- GitHub Repo creation ---
    try:
        repo, repo_url, pages_url = create_repo_and_push(GITHUB_TOKEN, brief, code_files)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"GitHub operation failed: {e}")

    # --- Get latest commit SHA ---
    try:
        commit_sha = repo.get_commits()[0].sha
    except Exception:
        commit_sha = "unknown"

    # --- Notify Evaluation API (if provided) ---
    if evaluation_url:
        eval_payload = {
            "email": email,
            "task": task,
            "round": round_num,
            "nonce": nonce,
            "repo_url": repo_url,
            "commit_sha": commit_sha,
            "pages_url": pages_url,
        }
        notify_evaluation(evaluation_url, eval_payload)

    return {
        "status": "ok",
        "message": "App created successfully via LLM",
        "repo_url": repo_url,
        "pages_url": pages_url,
        "commit_sha": commit_sha,
    }


# ----------------------------- MAIN ENTRY -----------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    print(f"🚀 Starting server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
