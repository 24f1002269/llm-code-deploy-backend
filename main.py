# main.py
from fastapi import FastAPI, HTTPException, Request
import os, requests, uuid, time
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

# ----------------------------- IN-MEMORY STORAGE ---------------------
# { (email, task, round): {"repo_url":..., "commit_sha":..., "pages_url":..., "timestamp":...} }
task_repos = {}

# ----------------------------- HELPERS --------------------------------

def call_llm_generate_code(brief: str, checks: list = None, attachments: list = None,
                           existing_code: str = "", round_num: int = 1) -> dict:
    """
    Generate code for a task using LLM.
    Returns dict of {filename: content}.
    """
    checks_text = "\n".join(checks or [])
    attach_text = "\n".join([f"- {a}" for a in attachments or []])

    system_prompt = (
        "You are an expert full-stack web engineer. "
        "Generate minimal self-contained front-end code (HTML + optional CSS + JS) "
        "for the described app. Do NOT include explanations or markdown fences. "
        "Output only raw code files (e.g., index.html, script.js, style.css)."
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

    print(f"\n🔹 Sending prompt to LLM (Round {round_num}) ...")
    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.3,
    )
    html_code = response.choices[0].message["content"].strip()
    if "<html" not in html_code.lower():
        html_code = f"<!DOCTYPE html><html><body><pre>{html_code}</pre></body></html>"
    return {"index.html": html_code}


def safe_repo_name(task_id: str):
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    name = "".join(c if c.isalnum() else "-" for c in task_id)[:50] or "repo"
    return f"{name}-{ts}"


def create_or_update_repo(email: str, task: str, round_num: int, code_files: dict):
    """
    Create a new repo (round 1) or update existing repo (round 2+)
    """
    g = Github(GITHUB_TOKEN)
    user = g.get_user()
    key = (email, task)

    if round_num == 1:
        repo_name = safe_repo_name(task)
        repo = user.create_repo(repo_name, private=False)
        print(f"✅ Created repo: {repo_name}")
        # Add files
        for path, content in code_files.items():
            repo.create_file(path, f"Add {path}", content)
        # Add README
        repo.create_file("README.md", "Add README", f"# {task}\n\nGenerated via LLM pipeline")
    else:
        # round >=2: update repo from round 1
        prev = task_repos.get(key)
        if not prev:
            raise HTTPException(status_code=404, detail="Round 1 repo not found")
        repo_url = prev["repo_url"]
        repo_name = repo_url.rstrip("/").split("/")[-1]
        repo = user.get_repo(repo_name)
        for path, content in code_files.items():
            try:
                f = repo.get_contents(path)
                repo.update_file(f.path, f"Round {round_num} update", content, f.sha)
                print(f"✅ Updated {path} for round {round_num}")
            except Exception:
                repo.create_file(path, f"Round {round_num} add {path}", content)
                print(f"✅ Created {path} for round {round_num}")

    # Enable GitHub Pages (best-effort)
    pages_api_url = f"https://api.github.com/repos/{user.login}/{repo.name}/pages"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    payload = {"source": {"branch": "main", "path": "/"}}
    try:
        r = requests.post(pages_api_url, headers=headers, json=payload)
        if r.status_code in [201, 204]:
            print("✅ GitHub Pages enabled")
        else:
            print(f"⚠️ Pages API response: {r.status_code}, {r.text}")
    except Exception as e:
        print("⚠️ Pages setup exception:", e)

    pages_url = f"https://{user.login}.github.io/{repo.name}/"
    commit_sha = repo.get_commits()[0].sha if repo.get_commits() else "unknown"

    # store repo info in memory
    task_repos[key] = {
        "repo_url": repo.html_url,
        "commit_sha": commit_sha,
        "pages_url": pages_url,
        "timestamp": datetime.utcnow().isoformat()
    }
    return repo, repo.html_url, pages_url, commit_sha


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
async def handle_task(request: Request):
    data = await request.json()
    print("\n===== Incoming Task =====")
    print(data)
    print("=========================\n")

    if data.get("secret") != SECRET_KEY:
        raise HTTPException(status_code=403, detail="Invalid secret key")

    email = data.get("email") or f"user-{uuid.uuid4().hex[:6]}"
    task = data.get("task") or f"task-{uuid.uuid4().hex[:6]}"
    round_num = int(data.get("round", 1))
    nonce = data.get("nonce") or ""
    evaluation_url = data.get("evaluation_url")

    # Round 1
    briefs_rounds = [{"brief": data.get("brief", ""), "checks": data.get("checks", [])}]
    # Append additional rounds if present
    for r2 in data.get("round2", []):
        briefs_rounds.append({"brief": r2.get("brief", ""), "checks": r2.get("checks", [])})

    existing_code = ""
    results = []

    for i, r in enumerate(briefs_rounds):
        rn = i + 1
        print(f"\n--- Processing Round {rn} ---")
        try:
            code_files = call_llm_generate_code(
                r["brief"], r.get("checks", []), attachments=data.get("attachments", []),
                existing_code=existing_code, round_num=rn
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"LLM generation failed: {e}")

        try:
            repo, repo_url, pages_url, commit_sha = create_or_update_repo(email, task, rn, code_files)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"GitHub operation failed: {e}")

        # Save code for next round
        existing_code = code_files.get("index.html", "")
        results.append({
            "round": rn,
            "repo_url": repo_url,
            "pages_url": pages_url,
            "commit_sha": commit_sha
        })

        # Notify evaluation for this round
        if evaluation_url:
            payload = {
                "email": email,
                "task": task,
                "round": rn,
                "nonce": nonce,
                "repo_url": repo_url,
                "pages_url": pages_url,
                "commit_sha": commit_sha
            }
            notify_evaluation(evaluation_url, payload)

    return {"status": "ok", "message": "Task processed via LLM", "results": results}


# ----------------------------- MAIN ----------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    print(f"🚀 Starting server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
