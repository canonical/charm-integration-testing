# Linking sub-issues

A mention in the issue body is not a sub-issue. Link it with the GitHub API,
using the child's numeric ID rather than its number:

```bash
repo=canonical/charm-integration-testing
child_id=$(gh api "repos/$repo/issues/CHILD_NUMBER" --jq .id)
gh api --method POST "repos/$repo/issues/PARENT_NUMBER/sub_issues" \
  -F sub_issue_id="$child_id"
gh api "repos/$repo/issues/CHILD_NUMBER/parent" --jq '{number, html_url}'
```

Verify the returned parent. If linking fails, report it rather than claiming
the issues are linked.
