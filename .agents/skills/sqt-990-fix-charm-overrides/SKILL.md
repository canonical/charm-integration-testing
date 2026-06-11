---
name: sqt-990-fix-charm-overrides
description: 'SQT-990 fix-charm-overrides workflow - fetch failed test_build_bundle runs from Test Observer, classify errors, generate a checklist, then for each charm: investigate the override YAML vs live metadata, fix static/charm-overrides/<charm>.yaml, validate, create a targeted SQT-990 attachment rule in Test Observer, bulk-attach, create a branch SQT-990/fix-<charm>-overrides with a single commit rebased onto origin/main, and open a PR. USE FOR: working through the SQT-990 triage queue, fixing UnparsableCharmException or UncompletableBundleError override failures, attaching test results to SQT-990 in Test Observer. INPUTS: Test Observer bearer token (PROD_TEST_OBSERVER_TOKEN), optional charm name to scope the work.'
argument-hint: 'charm name to fix, or omit to start from the full triage report'
---

# SQT-990: Fix Charm Overrides

End-to-end workflow for triaging `test_build_bundle` failures caused by stale charm override
YAML files, fixing them, and attaching results to [SQT-990](https://warthogs.atlassian.net/browse/SQT-990).

## Prerequisites

- Python venv: `source /home/ubuntu/.cache/pypoetry/virtualenvs/charm-integration-testing-meta-10MWES9l-py3.10/bin/activate`
- Credentials in `.agents/skills/sqt-990-fix-charm-overrides/.env` (gitignored):
  ```
  TEST_OBSERVER_API_URL=https://test-observer-api.canonical.com
  TEST_OBSERVER_API_KEY=<token>
  ```
- Load with: `export $(grep -v '^#' .agents/skills/sqt-990-fix-charm-overrides/.env | xargs)`
- SQT-990 issue id in Test Observer: **545**

## Phase 1: Fetch and Classify Failures (do once per session)

Fetch all currently-failing `test_build_bundle` runs with no issue attached:

```python
import urllib.request, json, urllib.parse, os

base = os.environ["TEST_OBSERVER_API_URL"].rstrip("/") + "/v1"
params = {
    "families": "charm",
    "test_result_statuses": "FAILED",
    "artefact_is_archived": "false",
    "rerun_is_requested": "false",
    "execution_is_latest": "true",
    "test_cases": "test_build_bundle",
    "issues": "none",
}
# The API uses offset-based pagination. Default limit is 50; use 100 for fewer round-trips.
# Response shape: {"count": N, "limit": N, "offset": N, "test_results": [...]}
params["limit"] = "100"
all_results = []
offset = 0
while True:
    params["offset"] = offset
    url = f"{base}/test-results?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url) as r:
        batch = json.loads(r.read())
    items = batch.get("test_results", [])
    all_results.extend(items)
    total = batch.get("count", 0)
    offset += len(items)
    if offset >= total or not items:
        break

with open("/tmp/failed_results.json", "w") as f:
    json.dump(all_results, f)
print(f"Fetched {len(all_results)} failed results")
```

Classify by `charm_qa:failure:message` value (truncated to 150 chars by `normalize_string()`):

- **UnparsableCharmException**: override declares endpoints absent from charm metadata at that channel.
  - Fix: update `static/charm-overrides/<charm>.yaml` with criteria blocks scoped per track.
- **UncompletableBundleError**: solver cannot fulfill a charm endpoint (missing counterpart charm).
  - Fix: mark the unfulfilled endpoint `optional: true` in `static/charm-overrides/<charm>.yaml`.
  - Attachment rule key: `charm_qa:failure:build_bundle:unfulfilled_endpoint`.
- **CharmReleaseNotFoundException**: charm revision not available for the requested channel.
  - Usually a transient Charmhub issue; may not require an override fix.

Save results to `/tmp/failed_results.json` and create `reports/checklist.md` with one entry per
distinct failure, each with three sub-steps: `[ ] Reproduced`, `[ ] Fixed override`, `[ ] Attached SQT-990`.

## Phase 2: Fix One Charm Override

For each checklist item, follow these steps.

### Step 1: Identify the exact failure message

```python
import json, collections, os

with open("/tmp/failed_results.json") as f:
    all_results = json.load(f)

charm = "identity-platform-login-ui-operator"  # change per item
msgs = collections.Counter()
for r in all_results:
    if r["artefact"]["name"] != charm:
        continue
    for msg in r["test_execution"]["execution_metadata"].get("charm_qa:failure:message", []):
        msgs[msg] += 1
for msg, n in msgs.most_common():
    print(f"[{n}] len={len(msg)}: {msg!r}")
```

The stored value is **exactly** the truncated string (max 150 chars, ending with `...` if truncated).
Use it verbatim as the attachment rule filter.

### Step 2: Investigate the override

Use the **charm-override-investigator** skill to:

1. Fetch live metadata for each failing channel via Charmhub API.
2. Compare against `static/charm-overrides/<charm>.yaml`.
3. Identify which endpoints are declared in the override but absent from metadata.

Key principle: if different tracks have different endpoint sets, use **criteria blocks** (`any_of`,
`all_of`, `none_of` on `track`/`risk`) rather than a single flat override. See
[`bundle_builder_x/bundle_builder_x/overrides.py`](../../bundle_builder_x/bundle_builder_x/overrides.py)
for the full schema.

### Step 3: Fix the override YAML

Edit `static/charm-overrides/<charm>.yaml`. Include evidence comments citing commits/PRs
that added or removed each endpoint, and which track revisions are affected.

### Step 4: Validate

```bash
./scripts/bundle-builder-x-tests.sh overrides \
  --overrides ./static/charm-overrides/ \
  --all-channels \
  -k <charm> -v
```

All channels must pass before proceeding. Iterate on the YAML if any fail.

### Step 5: Create the attachment rule in Test Observer

Identify the correct metadata key and value for the rule:

| Error type | Metadata key | Value |
|---|---|---|
| UnparsableCharmException | `charm_qa:failure:message` | exact 150-char truncated string |
| UncompletableBundleError | `charm_qa:failure:build_bundle:unfulfilled_endpoint` | e.g. `"hydra:public-route"` |
| CharmReleaseNotFoundException | `charm_qa:failure:message` | exact 150-char truncated string |

POST to create the rule (scoped to `test_case_names: ["test_build_bundle"]` and
`test_result_statuses: ["FAILED"]` to avoid over-matching):

```python
import urllib.request, json, os

token = os.environ["TEST_OBSERVER_API_KEY"]
base = os.environ["TEST_OBSERVER_API_URL"].rstrip("/") + "/v1"

rule_body = {
    "enabled": True,
    "families": ["charm"],
    "environment_names": [],
    "test_case_names": ["test_build_bundle"],
    "template_ids": [],
    "test_result_statuses": ["FAILED"],
    "execution_metadata": {
        "charm_qa:failure:message": [
            "<exact 150-char value here>"
        ]
    }
}

data = json.dumps(rule_body).encode()
req = urllib.request.Request(
    f"{base}/issues/545/attachment-rules",
    data=data, method="POST",
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
)
with urllib.request.urlopen(req) as r:
    resp = json.loads(r.read())
    print(f"Rule ID: {resp['id']}")
```

### Step 6: Bulk-attach SQT-990

The `attachment_rule` field alone is only attribution metadata - it does NOT drive which results
get attached. You must also pass `test_results_filters` to specify which results to attach.

```python
import urllib.request, json, os

token = os.environ["TEST_OBSERVER_API_KEY"]
base = os.environ["TEST_OBSERVER_API_URL"].rstrip("/") + "/v1"
rule_id = 510  # replace with actual rule id from Step 5

body = {
    "attachment_rule": rule_id,
    "test_results_filters": {
        "families": ["charm"],
        "test_cases": ["test_build_bundle"],
        "test_result_statuses": ["FAILED"],
        "artefact_is_archived": False,
        "execution_is_latest": True,
        "rerun_is_requested": False,
        "issues": "none",
        "execution_metadata": {
            "charm_qa:failure:message": [
                "<exact 150-char value here>"
            ]
        }
    }
}

data = json.dumps(body).encode()
req = urllib.request.Request(
    f"{base}/issues/545/attach",
    data=data, method="POST",
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
)
with urllib.request.urlopen(req) as r:
    print(f"HTTP {r.status}: attached")
```

Verify by checking the count drops in the `issues=none` results:
```python
url = f"{base}/test-results?families=charm&test_result_statuses=FAILED&issues=none&limit=1"
with urllib.request.urlopen(url) as r:
    print("Remaining unattached:", json.loads(r.read()).get("count"))
```

### Step 7: Create branch, commit, and PR

Run lint before committing - yamlfix in particular will reformat YAML comments
containing `#<number>` and flag other style issues:

```bash
./scripts/lint.sh
# If yamlfix reports failures, auto-fix with:
poetry run yamlfix static/charm-overrides/<charm>.yaml
# Then re-run to confirm clean:
./scripts/lint.sh
```

```bash
# Stash any unrelated changes, then create branch directly from origin/main.
# This avoids the rebase dance and ensures the branch has exactly one commit.
git stash push -m "wip" -- static/charm-overrides/<charm>.yaml   # stash ONLY the override file
git checkout origin/main -b SQT-990/fix-<charm>-overrides
git diff "stash@{0}^1" "stash@{0}" -- static/charm-overrides/<charm>.yaml | git apply
git stash drop stash@{0}

# Stage and commit only the override file
git add static/charm-overrides/<charm>.yaml
git commit -m "[SQT-990] Fix <charm> overrides

<one-paragraph summary of what changed and why, citing track revisions and
upstream commits/PRs where relevant>

Closes SQT-990"

# Push
git push origin SQT-990/fix-<charm>-overrides
```

Then open a PR via `mcp_github_mcp_se_create_pull_request`:
- **owner**: `canonical`
- **repo**: `charm-integration-testing`
- **base**: `main`
- **title**: `[SQT-990] Fix <charm> overrides`
- **body**: follow `.github/pull_request_template.md`; include validation results and failure count.

### Step 8: Update checklist

Mark all three sub-steps done for this charm in `reports/checklist.md`:
```markdown
- [x] Reproduced
- [x] Fixed override
- [x] Attached SQT-990 (rule <id>)
```

## Key File Locations

| Path | Purpose |
|---|---|
| `static/charm-overrides/<charm>.yaml` | Override YAML edited per charm |
| `bundle_builder_x/bundle_builder_x/overrides.py` | `CharmGlobalOverrides` schema |
| `scripts/bundle-builder-x-tests.sh` | Validation script |
| `reports/checklist.md` | Per-session triage checklist |
| `reports/test_build_bundle_failures.md` | Full failure classification report |
| `/tmp/failed_results.json` | Raw API results (per session) |

## Execution Metadata Keys

Set by `charm_integration_testing/test_suite/conftest.py`:

- `charm_qa:failure:message` - normalized exception string, max 150 chars (appends `...` on truncation)
- `charm_qa:failure:build_bundle:unfulfilled_endpoint` - `"<charm>:<endpoint>"` for `UncompletableBundleError`
- `charm_qa:failure:build_bundle:unfulfilled_interface` - interface name for `UncompletableBundleError`

## Notes

- Never use a broad attachment rule (e.g. matching all charm families with no metadata filter).
  Always scope to the specific `charm_qa:failure:message` or `charm_qa:failure:build_bundle:unfulfilled_endpoint`
  value that uniquely identifies the failing override.
- Load credentials before running any API calls: `export $(grep -v '^#' .agents/skills/sqt-990-fix-charm-overrides/.env | xargs)`
  The `.env` file is gitignored. Never commit it or print the token value.
- Local `main` may be many commits ahead of `origin/main` due to merged PRs not yet pushed.
  Use `git checkout origin/main -b SQT-990/fix-<charm>-overrides` to base the branch directly
  on the remote tip rather than rebasing after the fact.
- The Charmhub info API channel-map does NOT include `metadata-yaml`; use the `CharmhubHttpClient`
  refresh endpoint instead:
  ```python
  import sys; sys.path.insert(0, "bundle_builder_x")
  from bundle_builder_x.charmhub_http import CharmhubHttpClient, RefreshAction, CharmhubBase
  client = CharmhubHttpClient()
  base = CharmhubBase(name="ubuntu", channel="22.04", architecture="amd64")
  result = client.refresh(RefreshAction(charm_name="<charm>", charm_channel="<track/risk>", base=base))
  print(sorted(result.charm.metadata.requires.keys()))
  print(sorted(result.charm.metadata.provides.keys()))
  ```
- Test Observer API pagination: response shape is `{"count": N, "limit": N, "offset": N, "test_results": [...]}`.
  Use `offset` not `page`. Default `limit` is 50; pass `limit=100` for efficiency.
