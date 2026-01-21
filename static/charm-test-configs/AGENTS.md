# Charm Test Configs - Agent Instructions

This document provides instructions for AI agents to create charm test configuration override files based on GitHub issues or user requests.

## Overview

Charm test configs are YAML files that define application configurations to be applied during integration testing. These configs allow testing different deployment scenarios, integration contexts, and charm behaviors by applying different Juju configurations based on specific criteria. Test configs are stored in `static/charm-test-configs/` with the naming convention `{charm-name}.yaml`.

## Purpose

During integration testing, some charms require specific configurations to:
1. **Adapt to different integration contexts** - Different configurations based on which endpoints are integrated
2. **Test multiple deployment scenarios** - Generate multiple test cases with different config values
3. **Support version-specific behavior** - Apply configs based on charm track/channel
4. **Enable role-based deployments** - Configure the charm's role (e.g., broker, controller, shard)
5. **Provide required initialization values** - Set necessary configs for the charm to function

Test configs enable comprehensive testing coverage by automatically generating test cases with appropriate configurations.

## File Structure

Each test config file is a YAML document with a `configs` list:

```yaml
---
configs:
  - criteria:
      # Optional: Conditions when this config applies
      - track: 'X.Y'
      - endpoint_integrated: endpoint-name
      - any_of: [...]
      - none_of: [...]
      - all_of: [...]
    config:
      # Juju configuration key-value pairs
      config-key: value
      another-key: value
  - config:
      # Config without criteria applies unconditionally
      config-key: different-value
```

## Config Elements

### 1. Unconditional Configs

Configs without criteria apply unconditionally and are used to test different configuration values, if multiple are supplied one is chosen randomly:

```yaml
---
configs:
  - config:
      num-history-shards: 1
  - config:
      num-history-shards: 2
  - config:
      num-history-shards: 4
```

**When to use:**
- Testing different scale configurations
- Testing enum values or modes
- Generating multiple test scenarios for the same deployment

**Example (temporal-k8s.yaml):**
Creates three separate test cases, each with a different number of history shards.

### 2. Integration-Conditional Configs

Use criteria to apply configs only when specific endpoints are integrated:

```yaml
---
configs:
  - criteria:
      - endpoint_integrated: config-server
    config:
      role: config-server
  - criteria:
      - endpoint_integrated: sharding
    config:
      role: shard
```

**When to use:**
- Role-based configuration (broker, controller, shard, etc.)
- Enabling features based on integrations (e.g., TLS when certificates are provided)
- Different behavior for different integration topologies

**Example (mongodb-k8s.yaml):**
Sets the MongoDB role based on which peer endpoint is integrated.

### 3. Track/Channel-Based Configs

Apply configs based on the charm's track or channel:

```yaml
---
configs:
  - criteria:
      - any_of:
          - track: '1.15'
          - track: '1.16'
          - track: '1.17'
      - any_of:
          - endpoint_integrated: vault-pki
          - endpoint_integrated: tls-certificates-pki
    config:
      common_name: charmqa
  - criteria:
      - track: '1.18'
      - any_of:
          - endpoint_integrated: vault-pki
          - endpoint_integrated: tls-certificates-pki
    config:
      pki_ca_common_name: charmqa
```

**When to use:**
- Configuration keys changed between versions
- Version-specific required values
- Different behavior across charm tracks

**Example (vault-k8s.yaml):**
The config key for PKI common name changed from `common_name` to `pki_ca_common_name` in track 1.18.

### 4. Complex Conditional Logic

Combine multiple criteria using logical operators:

```yaml
---
configs:
  - criteria:
      - track: '4'
      - none_of:
          - endpoint_integrated: peer-cluster
          - endpoint_integrated: peer-cluster-orchestrator
    config:
      roles: broker,controller
  - criteria:
      - track: '4'
      - endpoint_integrated: peer-cluster
    config:
      roles: broker
  - criteria:
      - track: '4'
      - endpoint_integrated: peer-cluster-orchestrator
    config:
      roles: broker,controller
```

**Operators:**
- `all_of`: All conditions must be true (implicit at criteria level)
- `any_of`: At least one condition must be true
- `none_of`: None of the conditions can be true
- `endpoint_integrated: <name>`: Check if endpoint `<name>` is integrated
- `track: '<version>'`: Check if charm is on specific track
- `endpoint_feature: <endpoint>:<feature>`: Check if endpoint has a specific feature enabled

**When to use:**
- Exclusive role assignment based on topology
- Feature combinations requiring specific configs
- Complex deployment scenarios

**Example (kafka-k8s.yaml):**
In track 4, Kafka can run as broker+controller (KRaft mode) or just broker (if using peer-cluster).

### 5. Simple Boolean Configs

Enable/disable features based on integration presence:

```yaml
---
configs:
  - criteria:
      endpoint_integrated: send-ca-cert
    config:
      ldaps_enabled: true
```

**When to use:**
- TLS/SSL enablement when certificates are provided
- Feature toggles based on optional integrations
- Single-condition configurations

**Example (glauth-k8s.yaml):**
Enable LDAPS when CA certificates are provided via the send-ca-cert endpoint.

### 6. Conditional Default Values

Provide different default configs based on integration presence:

```yaml
---
configs:
  - criteria:
      - none_of:
          - endpoint_integrated: vault
    config:
      uuid: aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
      public-key: pE8oX25fmNPzg7i195spacUxTPLpioiSv/BG9FLgtDY=
      private-key: SdAKRHy3LW7vkmXFhOPTM43kwhgcot0mZrHX8rkHDlM=
      dns-name: jimm-test.local
      postgres-secret-storage: true
  - criteria:
      - endpoint_integrated: vault
    config:
      uuid: aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
      public-key: pE8oX25fmNPzg7i195spacUxTPLpioiSv/BG9FLgtDY=
      private-key: SdAKRHy3LW7vkmXFhOPTM43kwhgcot0mZrHX8rkHDlM=
      dns-name: jimm-test.local
```

**When to use:**
- Providing test secrets when secret management is not integrated
- Different initialization values based on integration topology
- Enabling/disabling features based on backend choice

**Example (juju-jimm-k8s.yaml):**
Uses postgres-secret-storage when vault is not integrated, otherwise secrets are stored in Vault.

## Reading Issues to Create Test Configs

When reading a GitHub issue requesting a test config, look for:

### Issue Indicators

1. **Keywords:**
   - "requires configuration"
   - "config must be set"
   - "role must be configured"
   - "different config for"
   - "test with different values"
   - "configuration depends on"
   - "charm blocks without config"
   - "must configure X when Y is integrated"

2. **Charm behavior descriptions:**
   - "The charm requires role configuration based on integration"
   - "TLS must be enabled when certificates are provided"
   - "Config key changed in version X"
   - "Need to test with multiple shard counts"
   - "Different modes: ingress/egress"

3. **Testing requirements:**
   - "Should test multiple deployment scenarios"
   - "Need coverage for all roles"
   - "Test different scale configurations"
   - "Verify behavior with/without integration X"

### Creating the Test Config

**Step 1: Identify the charm name**
- Extract from issue title or body
- Create file: `static/charm-test-configs/{charm-name}.yaml`

**Step 2: Determine configuration requirements**
- List all config keys that need to be set
- Identify conditions (integrations, tracks, features)
- Note any version-specific differences

**Step 3: Map configurations to criteria**

For **role-based configurations:**
```yaml
configs:
  - criteria:
      - endpoint_integrated: <peer-or-relation>
    config:
      role: <role-name>
```

For **testing multiple values:**
```yaml
configs:
  - config:
      key: value1
  - config:
      key: value2
  - config:
      key: value3
```

For **version-specific configs:**
```yaml
configs:
  - criteria:
      - track: 'X.Y'
    config:
      new-key: value
  - criteria:
      - track: 'A.B'
    config:
      old-key: value
```

For **integration-dependent configs:**
```yaml
configs:
  - criteria:
      - endpoint_integrated: <endpoint-name>
    config:
      feature-enabled: true
```

**Step 4: Handle mutual exclusivity**

When configs are mutually exclusive (e.g., only one role applies):
```yaml
configs:
  - criteria:
      - endpoint_integrated: A
      - none_of:
          - endpoint_integrated: B
    config:
      role: role-a
  - criteria:
      - endpoint_integrated: B
    config:
      role: role-b
  - criteria:
      - none_of:
          - endpoint_integrated: A
          - endpoint_integrated: B
    config:
      role: default-role
```

**Step 5: Validate the logic**
- Ensure all criteria combinations are covered
- Check for conflicts or overlaps
- Verify track/version numbers are correct
- Confirm config key names match the charm's config.yaml

## Common Patterns

### Pattern 1: Role Assignment
```yaml
configs:
  - criteria:
      - endpoint_integrated: role-defining-endpoint
    config:
      role: specific-role
```
**Used by:** mongodb-k8s, kafka-k8s

### Pattern 2: Multi-Scenario Testing
```yaml
configs:
  - config: {key: value1}
  - config: {key: value2}
  - config: {key: value3}
```
**Used by:** temporal-k8s, istio-gateway

### Pattern 3: Version-Specific Configuration
```yaml
configs:
  - criteria:
      - track: 'old-version'
    config:
      old-key: value
  - criteria:
      - track: 'new-version'
    config:
      new-key: value
```
**Used by:** vault-k8s

### Pattern 4: Integration-Enabled Features
```yaml
configs:
  - criteria:
      - endpoint_integrated: feature-provider
    config:
      feature-enabled: true
```
**Used by:** glauth-k8s

### Pattern 5: Conditional Defaults with Secrets
```yaml
configs:
  - criteria:
      - none_of:
          - endpoint_integrated: secret-backend
    config:
      secret-value: test-value-for-testing
  - criteria:
      - endpoint_integrated: secret-backend
    config:
      # Secrets managed by backend, different config
```
**Used by:** juju-jimm-k8s

## Tips for Agents

1. **Always check the charm's config.yaml** in the charm repository to verify:
   - Correct config key names
   - Valid config value types and ranges
   - Required vs optional configs

2. **Consider the integration topology:**
   - How do different integrations affect the charm's role?
   - Are there mutually exclusive integrations?
   - What configs are needed for each topology?

3. **Version awareness:**
   - Check if config keys changed between versions
   - Note which tracks/channels the charm supports
   - Verify version-specific behaviors

4. **Testing coverage:**
   - Include configs that test different code paths
   - Cover major deployment scenarios
   - Test boundary values and edge cases

5. **Keep it simple:**
   - Start with unconditional configs for simple cases
   - Add criteria only when necessary
   - Use clear, descriptive criteria

6. **Documentation:**
   - Add comments explaining complex criteria
   - Link to issues or PRs that describe the requirement
   - Note any workarounds or test-specific values

## Example: Creating a Config from an Issue

**Issue Description:**
> The postgresql-k8s charm requires role configuration. When integrated via the db-admin endpoint, it should be configured as "admin". When integrated via the db endpoint, it should be "replica". We should also test with different numbers of replicas: 1, 3, and 5.

**Analysis:**
1. Charm: postgresql-k8s
2. Configs needed: 
   - `role` (based on integration)
   - `replicas` (multiple test values)
3. Criteria: endpoint_integrated for role

**Resulting Config:**
```yaml
---
# Based on issue #XYZ - postgresql role and replica testing
configs:
  # Role-based configs
  - criteria:
      - endpoint_integrated: db-admin
    config:
      role: admin
      replicas: 3  # Default replica count
  - criteria:
      - endpoint_integrated: db
    config:
      role: replica
      replicas: 3
  
  # Additional test scenarios with different replica counts
  - criteria:
      - endpoint_integrated: db-admin
    config:
      role: admin
      replicas: 1
  - criteria:
      - endpoint_integrated: db-admin
    config:
      role: admin
      replicas: 5
  - criteria:
      - endpoint_integrated: db
    config:
      role: replica
      replicas: 1
  - criteria:
      - endpoint_integrated: db
    config:
      role: replica
      replicas: 5
```

## Validation Checklist

Before submitting a test config file, verify:

- [ ] File name matches charm name: `{charm-name}.yaml`
- [ ] YAML syntax is valid
- [ ] All config keys exist in the charm's config.yaml
- [ ] Criteria logic is correct and complete
- [ ] No conflicting configurations for the same criteria
- [ ] Track/version numbers are accurate
- [ ] Test coverage is comprehensive
- [ ] Comments explain complex logic
- [ ] File is placed in `static/charm-test-configs/`
