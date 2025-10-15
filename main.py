from fastapi import FastAPI, HTTPException, Request
import os
from github import Github
from datetime import datetime
import requests

app = FastAPI()

# Read secrets from environment
SECRET_KEY = os.environ.get("SECRET_KEY", "testsecret")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
AIPIPE_KEY = os.environ.get("AIPIPE_KEY")  # <-- read AI pipeline token

@app.get("/")
async def root():
    return {"status": "ok", "message": "FastAPI server is running!"}

@app.post("/api-endpoint")
async def receive_task(request: Request):
    data = await request.json()
    
    # Verify secret
    if data.get("secret") != SECRET_KEY:
        raise HTTPException(status_code=403, detail="Invalid secret")
    
    task_brief = data.get("brief", "Hello World App")
    evaluation_url = data.get("evaluation_url")
    email = data.get("email", "student@example.com")
    task_id = data.get("task", "task-unknown")
    round_index = data.get("round", 1)
    nonce = data.get("nonce", "")

    print("\n===== Task Received =====")
    print(data)
    print("=========================\n")
    
    # --- GitHub: Create repo ---
    g = Github(GITHUB_TOKEN)
    user = g.get_user()
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    repo_name = f"hello-world-{timestamp}"
    
    try:
        repo = user.create_repo(repo_name, private=False)
        print("✅ Repo created:", repo_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Repo creation failed: {e}")
    
    # --- Dynamic files ---
    index_html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>{task_brief}</title>
    <meta charset="UTF-8">
</head>
<body>
    <h1 id="message">{task_brief}</h1>
</body>
</html>"""

    readme_content = f"# {repo_name}\n\n{task_brief}\n\nMinimal test app created via API."
    license_content = """MIT License

Copyright (c) 2025

Permission is hereby granted, free of charge, to any person obtaining a copy
...
"""

    files = {"index.html": index_html_content, "README.md": readme_content, "LICENSE": license_content}
    for path, content in files.items():
        repo.create_file(path, f"Add {path}", content)
    
    # --- GitHub Pages ---
    pages_api_url = f"https://api.github.com/repos/{user.login}/{repo_name}/pages"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    payload = {"source": {"branch": "main", "path": "/"}}
    try:
        r = requests.post(pages_api_url, headers=headers, json=payload)
        if r.status_code in [201, 204]:
            print("✅ GitHub Pages enabled successfully!")
        else:
            print(f"⚠️ GitHub Pages enable failed: {r.status_code}, {r.text}")
    except Exception as e:
        print("⚠️ Exception enabling GitHub Pages:", e)

    pages_url = f"https://{user.login}.github.io/{repo_name}/"

    # --- Notify evaluation URL ---
    if evaluation_url:
        eval_payload = {
            "email": email,
            "task": task_id,
            "round": round_index,
            "nonce": nonce,
            "repo_url": repo.html_url,
            "commit_sha": repo.get_commits()[0].sha,
            "pages_url": pages_url
        }
        try:
            resp = requests.post(evaluation_url, json=eval_payload)
            if resp.status_code == 200:
                print("✅ Evaluation notified successfully")
            else:
                print(f"⚠️ Evaluation POST failed: {resp.status_code}, {resp.text}")
        except Exception as e:
            print("⚠️ Exception notifying evaluation:", e)

    return {
        "status": "ok",
        "message": "Task received and repo created successfully",
        "repo_url": repo.html_url,
        "pages_url": pages_url
    }
