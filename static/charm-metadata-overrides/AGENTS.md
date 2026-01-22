# Charm Metadata Overrides - Agent Instructions

This document provides instructions for AI agents to create charm metadata override files based on GitHub issues or user requests.

## Overview

Charm metadata overrides are YAML files that modify the behavior of the bundle builder algorithm by specifying endpoint optionality, limits, and features for charm integrations. These overrides are stored in `static/charm-metadata-overrides/` with the naming convention `{charm-name}.yaml`.

## Purpose

The bundle builder attempts to automatically resolve all non-optional charm integration endpoints. However, some charms have endpoints that:
1. Should be optional in certain contexts (e.g., observability endpoints)
2. Have conditional requirements based on other integrations
3. Need integration limits to prevent conflicts
4. Support optional features like TLS

Overrides allow us to encode these rules so the bundle builder makes intelligent decisions.

## File Structure

Each override file is a YAML document with three main sections:

```yaml
---
# Optional: Comment explaining the override or linking to the issue/PR
provides:
  endpoint-name:
    # Override properties...
requires:
  endpoint-name:
    # Override properties...
peers:
  endpoint-name:
    # Override properties...
```

## Override Properties

### 1. Simple Optionality

Use `optional: true` when an endpoint should always be optional:

```yaml
provides:
  metrics-endpoint:
    optional: true
  grafana-dashboard:
    optional: true
```

**When to use:**
- Observability endpoints (metrics, dashboards, logs) that are nice-to-have but not required
- Endpoints that provide auxiliary functionality

### 2. Conditional Optionality (`optional_if`)

Use `optional_if` when an endpoint is optional only under specific conditions:

```yaml
requires:
  certificates:
    optional_if:
      - none_of:
          - endpoint_integrated: database
```

**Operators:**
- `all_of`: All conditions must be true
- `any_of`: At least one condition must be true  
- `none_of`: None of the conditions can be true
- `endpoint_integrated: <name>`: Check if endpoint `<name>` is integrated
- `endpoint_feature: <endpoint>:<feature>`: Check if endpoint has a specific feature enabled

**When to use:**
- Endpoints that become optional/required based on other integrations
- Dependencies between integrations (e.g., TLS certificates only needed when TLS is enabled)
- Alternative integration paths (e.g., cloud-config OR specific integrations)

**Common patterns:**

1. **Mutual exclusivity** - Only one of multiple endpoints should be integrated:
```yaml
provides:
  database:
    limit_if:
      - criteria:
          - any_of:
              - endpoint_integrated: db
              - endpoint_integrated: db-admin
        limit: 0
    limit: 1
```

2. **Conditional dependency** - Require integration only when another is present:
```yaml
requires:
  certificates:
    optional_if:
      - none_of:
          - endpoint_integrated: client-cas
```

3. **Alternative paths** - Optional if ANY alternative is satisfied:
```yaml
requires:
  send-remote-write:
    optional_if:
      - any_of:
          - none_of:
              - endpoint_integrated: metrics-endpoint
          - all_of:
              - endpoint_integrated: metrics-endpoint
              - endpoint_integrated: grafana-cloud-config
```

### 3. Integration Limits (`limit` and `limit_if`)

Control how many integrations can be made to an endpoint:

```yaml
provides:
  database:
    limit: 1  # Only allow 1 integration
```

Or conditionally:

```yaml
provides:
  database:
    limit_if:
      - criteria:
          - any_of:
              - endpoint_integrated: db
              - endpoint_integrated: db-admin
        limit: 0  # Set to 0 if db or db-admin is integrated
    limit: 1  # Otherwise allow 1
```

**When to use:**
- Preventing multiple integrations that would conflict
- Implementing mutual exclusivity between similar endpoints
- Enforcing architectural constraints

### 4. Features

Specify features that an endpoint supports:

```yaml
provides:
  database:
    optional: true
    features: [tls]
```

**When to use:**
- Indicating TLS support on database/communication endpoints
- Marking capabilities that affect integration behavior

## Reading Issues to Create Overrides

When reading a GitHub issue requesting an override, look for:

### Issue Indicators

1. **Keywords:**
   - "should be optional"
   - "only required when"
   - "conflicts with"
   - "mutual exclusivity"
   - "limit to N integrations"
   - "supports TLS"
   - "observability endpoint"
   - "blocks on" / "blocked by"

2. **Charm behavior descriptions:**
   - "The charm goes into blocked state when X is missing and Y is present"
   - "Either X or Y must be integrated, but not both"
   - "Requires certificates only when TLS is enabled"
   - "Must have at least one of: X, Y, or Z"

3. **Links to charm source code:**
   - Check for validation logic in hooks/events
   - Look for `defer()` calls that indicate blocking behavior
   - Examine status messages and error conditions

### Creating the Override

**Step 1: Identify the charm name**
- Extract from issue title or body
- Create file: `static/charm-metadata-overrides/{charm-name}.yaml`

**Step 2: Determine affected endpoints**
- List all endpoints mentioned in the issue
- Classify as `provides`, `requires`, or `peers`

**Step 3: Map the logic**
- Simple optionality → `optional: true`
- Conditional logic → `optional_if` with appropriate operators
- Limits → `limit` or `limit_if`
- Capabilities → `features`

**Step 4: Add documentation**
- Start file with `---`
- Add comment explaining the override
- Link to the GitHub issue/PR: `# Remove after: https://github.com/...`

**Step 5: Test logic mentally**
- Walk through scenarios to ensure conditions are correct
- Check for logical contradictions (e.g., `any_of: []` is always false)
- Verify mutual exclusivity is properly expressed

## Examples from Existing Overrides

### Example 1: Simple Observability Endpoints

```yaml
---
# MinIO - high-performance object storage
provides:
  # Remove after: https://github.com/canonical/charm-integration-testing/issues/282
  metrics-endpoint:
    optional: true
  grafana-dashboard:
    optional: true
```

**Reasoning:** Observability endpoints are always optional - the charm functions without them.

### Example 2: Conditional Dependency

```yaml
---
# Remove after: https://github.com/canonical/kafka-k8s-operator/pull/189
requires:
  # The `client-cas` integration creates a hard dependency on the `certificates` 
  # integration - when `client-cas` is present, the charm defers processing 
  # indefinitely until `certificates` is also integrated.
  certificates:
    interface: tls-certificates
    optional_if:
      - none_of:
          - endpoint_integrated: client-cas
  client-cas:
    interface: certificate_transfer
    optional: true
```

**Reasoning:** `certificates` is required ONLY when `client-cas` is integrated. The `none_of` condition makes it non-optional when `client-cas` is present.

### Example 3: Complex Multi-Path Logic (Grafana Agent)

```yaml
provides:
  logging-provider:
    optional_if:
      - any_of:
          - endpoint_integrated: metrics-endpoint
          - endpoint_integrated: tracing-provider
          - endpoint_integrated: grafana-dashboards-consumer
requires:
  send-remote-write:
    optional_if:
      - any_of:
          - none_of:
              - endpoint_integrated: metrics-endpoint
          - all_of:
              - endpoint_integrated: metrics-endpoint
              - endpoint_integrated: grafana-cloud-config
```

**Reasoning:** 
- `logging-provider` is optional if ANY other input integration exists (agent needs at least one input)
- `send-remote-write` is optional if either: no `metrics-endpoint` OR metrics-endpoint + cloud-config (alternative output)

### Example 4: Mutual Exclusivity with Limits

```yaml
provides:
  database:
    limit_if:
      - criteria:
          - any_of:
              - endpoint_integrated: db
              - endpoint_integrated: db-admin
        limit: 0
    limit: 1
  db:
    limit_if:
      - criteria:
          - any_of:
              - endpoint_integrated: database
              - endpoint_integrated: db-admin
        limit: 0
    limit: 1
```

**Reasoning:** Only one of `database`, `db`, or `db-admin` can be integrated at a time. When any one is integrated, the others are limited to 0.

## Common Pitfalls

1. **Empty `any_of: []`** - Always evaluates to false (never optional)
2. **Empty `all_of: []`** - Always evaluates to true (always optional)
3. **Confusing `none_of`** - "none of these can be true" means ALL must be false
4. **Over-constraining** - Adding too many conditions can make valid bundles impossible
5. **Under-constraining** - Too few conditions may allow invalid bundle states

## Validation Checklist

Before submitting an override:
- [ ] File name matches charm name exactly: `{charm-name}.yaml`
- [ ] Starts with `---` YAML document separator
- [ ] Includes comment linking to issue/PR
- [ ] Logic has been tested mentally with example scenarios
- [ ] No contradictory conditions
- [ ] Endpoints are in correct section (provides/requires/peers)
- [ ] YAML syntax is valid (proper indentation, no tabs)

## Advanced Patterns

### Pattern: "At least one of" requirement

```yaml
# All three are optional IF at least one other exists
provides:
  endpoint-a:
    optional_if:
      - any_of:
          - endpoint_integrated: endpoint-b
          - endpoint_integrated: endpoint-c
  endpoint-b:
    optional_if:
      - any_of:
          - endpoint_integrated: endpoint-a
          - endpoint_integrated: endpoint-c
  endpoint-c:
    optional_if:
      - any_of:
          - endpoint_integrated: endpoint-a
          - endpoint_integrated: endpoint-b
```

### Pattern: "Requires X only when feature Y is enabled"

```yaml
requires:
  certificates:
    optional_if:
      - none_of:
          - all_of:
              - endpoint_integrated: database
              - endpoint_feature: database:tls
```

### Pattern: "Universal alternative" (like grafana-cloud-config)

```yaml
requires:
  specific-backend:
    optional_if:
      - any_of:
          - none_of:
              - endpoint_integrated: data-source
          - all_of:
              - endpoint_integrated: data-source
              - endpoint_integrated: universal-backend
```

## Getting Help

If the logic in an issue is unclear:
1. Ask for clarification on what conditions make the endpoint optional/required
2. Request links to charm source code showing the blocking logic
3. Look for similar charms with existing overrides as templates
4. Test your understanding by describing scenarios back to the requester

## Code Integration

These overrides are consumed by:
- `bundle_builder/bundle_builder/overrides.py` - Reads and parses override files
- `bundle_builder/bundle_builder/charm.py` - Contains the data models for endpoints, optionality, and limits
- The bundle builder algorithm evaluates these conditions during graph traversal to determine valid bundle configurations

Remember: The goal is to help the bundle builder make intelligent decisions about which endpoints must be fulfilled versus which can be left unintegrated for a valid deployment.
