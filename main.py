from fastapi import FastAPI, HTTPException, Request
import os, requests, uuid
from datetime import datetime
from github import Github
from openai import OpenAI

# ----------------------------- CONFIG --------------------------------
app = FastAPI()

SECRET_KEY = os.environ.get("SECRET_KEY", "testsecret")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
AIPIPE_KEY = os.environ.get("AIPIPE_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", AIPIPE_KEY)

# Initialize OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)

# ----------------------------- IN-MEMORY STORAGE ---------------------
# { (email, task): {"repo_url":..., "commit_sha":..., "pages_url":..., "timestamp":..., "files": {filename: content} } }
task_repos = {}

# ----------------------------- HELPERS --------------------------------

def call_llm_generate_code(brief: str, checks: list = None, attachments: dict = None,
                           existing_files: dict = None, round_num: int = 1) -> dict:
    checks_text = "\n".join(checks or [])
    attach_text = "\n".join([f"- {k}" for k in attachments.keys()] if attachments else "")
    existing_code_text = ""
    if existing_files:
        for fname, content in existing_files.items():
            existing_code_text += f"### File: {fname}\n{content}\n"

    system_prompt = (
        "You are an expert full-stack web engineer. "
        "Generate fully working front-end code (HTML + optional CSS + JS) "
        "for the described app. Do NOT echo the brief. "
        "Do NOT include explanations or markdown fences. "
        "Output raw code files with filenames (e.g., index.html, script.js, style.css)."
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

### Existing Files
{existing_code_text[:15000]}
"""

    print(f"\n🔹 Sending prompt to LLM (Round {round_num}) ...")

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.3,
    )

    code_text = response.choices[0].message.content.strip()

    # naive splitting: assume LLM outputs "filename.ext:\n<code>"
    files = {}
    current_file = None
    buffer = []
    for line in code_text.splitlines():
        if line.strip().endswith((".html", ".js", ".css")) and not line.startswith(" "):
            if current_file:
                files[current_file] = "\n".join(buffer).strip()
            current_file = line.strip()
            buffer = []
        else:
            buffer.append(line)
    if current_file:
        files[current_file] = "\n".join(buffer).strip()

    # fallback if no splitting detected
    if not files:
        files["index.html"] = f"<!DOCTYPE html>\n<html><body><pre>{code_text}</pre></body></html>"

    # merge attachments if provided
    if attachments:
        for fname, content in attachments.items():
            files[fname] = content

    return files


def safe_repo_name(task_id: str):
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    name = "".join(c if c.isalnum() else "-" for c in task_id)[:50] or "repo"
    return f"{name}-{ts}"


def create_or_update_repo(email: str, task: str, round_num: int, code_files: dict):
    g = Github(GITHUB_TOKEN)
    user = g.get_user()
    key = (email, task)

    if round_num == 1:
        repo_name = safe_repo_name(task)
        repo = user.create_repo(repo_name, private=False)
        print(f"✅ Created repo: {repo_name}")
        for path, content in code_files.items():
            repo.create_file(path, f"Add {path}", content)
        repo.create_file("README.md", "Add README", f"# {task}\n\nGenerated via LLM pipeline")
        stored_files = code_files.copy()
    else:
        prev = task_repos.get(key)
        if not prev:
            raise HTTPException(status_code=404, detail="Round 1 repo not found")
        repo_url = prev["repo_url"]
        repo_name = repo_url.rstrip("/").split("/")[-1]
        repo = user.get_repo(repo_name)
        stored_files = prev.get("files", {}).copy()
        for path, content in code_files.items():
            try:
                f = repo.get_contents(path)
                repo.update_file(f.path, f"Round {round_num} update", content, f.sha)
                print(f"✅ Updated {path} for round {round_num}")
            except Exception:
                repo.create_file(path, f"Round {round_num} add {path}", content)
                print(f"✅ Created {path} for round {round_num}")
            stored_files[path] = content

    # Enable GitHub Pages
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

    task_repos[key] = {
        "repo_url": repo.html_url,
        "commit_sha": commit_sha,
        "pages_url": pages_url,
        "timestamp": datetime.utcnow().isoformat(),
        "files": stored_files
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

    briefs_rounds = [{"brief": data.get("brief", ""), "checks": data.get("checks", [])}]
    for r2 in data.get("round2", []):
        briefs_rounds.append({"brief": r2.get("brief", ""), "checks": r2.get("checks", [])})

    existing_files = task_repos.get((email, task), {}).get("files", {})
    results = []

    for i, r in enumerate(briefs_rounds):
        rn = i + 1
        print(f"\n--- Processing Round {rn} ---")
        try:
            code_files = call_llm_generate_code(
                r["brief"], r.get("checks", []),
                attachments=data.get("attachments", {}),
                existing_files=existing_files,
                round_num=rn
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"LLM generation failed: {e}")

        try:
            repo, repo_url, pages_url, commit_sha = create_or_update_repo(email, task, rn, code_files)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"GitHub operation failed: {e}")

        existing_files = task_repos.get((email, task), {}).get("files", {})
        results.append({
            "round": rn,
            "repo_url": repo_url,
            "pages_url": pages_url,
            "commit_sha": commit_sha
        })

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
