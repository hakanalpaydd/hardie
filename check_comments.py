import subprocess
import json

def get_copilot_comments(pr_number):
    query = """query($owner: String!, $repo: String!, $pr: Int!) { repository(owner: $owner, name: $repo) { pullRequest(number: $pr) { reviewThreads(first: 100) { nodes { id isResolved comments(first: 10) { nodes { body author { login } path line } } } } } } }"""
    cmd = ["gh", "api", "graphql", "-f", f"query={query}", "-F", "owner=doordash", "-F", "repo=web-next", "-F", f"pr={pr_number}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)
    
    comments = []
    for thread in data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]:
        if not thread["isResolved"]:
            first_comment = thread["comments"]["nodes"][0]
            if first_comment["author"]["login"] == "copilot-pull-request-reviewer":
                comments.append({
                    "path": first_comment["path"],
                    "line": first_comment["line"],
                    "body": first_comment["body"][:300]
                })
    return comments

for pr in [97506, 97513, 97515]:
    print(f"\n=== PR #{pr} ===")
    comments = get_copilot_comments(pr)
    print(f"Found {len(comments)} Copilot comments")
    for i, c in enumerate(comments, 1):
        print(f"\n{i}. {c['path']}:{c['line']}")
        body_preview = c['body'].replace('\n', ' ')[:150]
        print(f"   {body_preview}...")

