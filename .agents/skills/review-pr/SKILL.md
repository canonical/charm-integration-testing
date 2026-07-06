---
name: review-pr
description: Review a pull request, address inline reviewer comments, post replies, and apply fixes. Use when asked to review PR feedback, respond to comments, or fix issues raised in a PR.
---

# Task: review and address PR feedback

## Goal

Read all open reviewer comments on a pull request, apply any necessary fixes to
the code, post a reply to each comment explaining what was done, and resolve the
threads where possible.

## AI disclaimer

**Every comment you post must end with the following disclaimer on its own line:**

```
> *This comment was posted by an AI assistant (GitHub Copilot) on behalf of the repository maintainer.*
```

This applies to all PR comment types: inline replies, general PR comments, and
review submission bodies.

---

## Step 1 — Read the PR

```bash
# Get PR title, author, description
# Note: avoid --jq for complex filters — it silently exits 1 in this environment.
# Pipe to python3 instead for reliable parsing.
gh api repos/canonical/charm-integration-testing/pulls/<number> \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Title: {d[\"title\"]}\nAuthor: {d[\"user\"][\"login\"]}\nBranch: {d[\"head\"][\"ref\"]}\n\nBody:\n{d[\"body\"]}')"

# Read all inline review comments (per_page=100 avoids the default 30-result truncation)
gh api "repos/canonical/charm-integration-testing/pulls/<number>/comments?per_page=100" \
  | python3 -c "
import sys, json
for c in json.load(sys.stdin):
    print(f\"{c['path']}:{c.get('line','?')} [{c['user']['login']}] (id:{c['id']})\")
    print(c['body'])
    print('---')
"

# Read general (issue-style) comments
gh api "repos/canonical/charm-integration-testing/issues/<number>/comments?per_page=100" \
  | python3 -c "
import sys, json
for c in json.load(sys.stdin):
    print(f\"[{c['user']['login']}] (id:{c['id']})\")
    print(c['body'])
    print('---')
"
```

Read the review instructions file at `.github/instructions/python-pr-review.instructions.md`
before evaluating any Python changes.

---

## Step 2 — Read the changed files

Check out the PR branch and read the relevant source files before making any changes:

```bash
# Find the branch name
gh api repos/canonical/charm-integration-testing/pulls/<number> \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['head']['ref'])"

git fetch origin <branch>
git checkout <branch>
```

---

## Step 3 — Apply fixes

Make targeted fixes in the source files. Follow the repository coding conventions.
Do not modify files outside the scope of the comments being addressed.

**Important — credential-string redaction:** The sandbox tools (`view`, `cat`,
and the `edit` tool's `old_str` matching) redact strings that look like
credentials (e.g. any value adjacent to the word `password`), replacing them
with `******`. If the file you are editing contains such strings, the `edit`
tool's `old_str` will not match. Use `python3` file I/O to read and rewrite
those files directly instead.

After editing, commit:

```bash
git add <files>
git commit -m "fix: <short description>

<detail of what was fixed and why>

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Step 4 — Post replies

Reply to each inline comment using the `gh api` tool (not `curl` — `curl` returns
404 for this endpoint in this environment):

```bash
gh api repos/canonical/charm-integration-testing/pulls/comments/<comment_id>/replies \
  -X POST --field body="<your reply>

> *This comment was posted by an AI assistant (GitHub Copilot) on behalf of the repository maintainer.*" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('reply id:', d.get('id','ERROR'), d.get('message',''))"
```

For general PR comments, `curl` works fine:

```bash
curl -s -X POST \
  -H "Authorization: token ${GH_TOKEN}" \
  -H "Content-Type: application/json" \
  "https://api.github.com/repos/canonical/charm-integration-testing/issues/<number>/comments" \
  -d '{"body": "<your reply>\n\n> *This comment was posted by an AI assistant (GitHub Copilot) on behalf of the repository maintainer.*"}'
```

Verify each reply by checking the returned `id` field is non-empty.

---

## Step 5 — Resolve threads (optional)

Resolve each review thread using the GraphQL API:

```bash
# First, get the node IDs for all open threads
curl -s -X POST \
  -H "Authorization: token ${GH_TOKEN}" \
  -H "Content-Type: application/json" \
  "https://api.github.com/graphql" \
  -d '{
    "query": "{ repository(owner: \"canonical\", name: \"charm-integration-testing\") { pullRequest(number: <number>) { reviewThreads(first: 20) { nodes { id isResolved comments(first: 1) { nodes { databaseId } } } } } } }"
  }' | python3 -c "
import sys, json
d = json.load(sys.stdin)
threads = d['data']['repository']['pullRequest']['reviewThreads']['nodes']
for t in threads:
    cid = t['comments']['nodes'][0]['databaseId'] if t['comments']['nodes'] else None
    print(t['id'], t['isResolved'], cid)
"

# Resolve each open thread
curl -s -X POST \
  -H "Authorization: token ${GH_TOKEN}" \
  -H "Content-Type: application/json" \
  "https://api.github.com/graphql" \
  -d '{"query": "mutation { resolveReviewThread(input: {threadId: \"<thread_node_id>\"}) { thread { id isResolved } } }"}'
```

**Note:** `resolveReviewThread` requires `pull_requests=write` on the token.
If it returns `FORBIDDEN`, the token lacks this scope — skip silently and note
that threads need manual resolution.

---

## Step 6 — Push (if applicable)

Only push if you have confirmed write access to the branch:

```bash
git push origin <branch>
```

If push is blocked (SSH key missing, token scope insufficient), commit locally
and report the commit SHA so a maintainer can cherry-pick or force-push.

---

## Summary output

After completing all steps, report:

- Which comments were addressed and how
- Which files were changed (with a one-line description per file)
- Which threads were resolved vs. left open (and why)
- The commit SHA (if a commit was made)
