# Bundle Builder Algorithm - Complete Step-by-Step Plan

## Overview
The bundle builder uses an iterative approach with Z3 constraint solving and scriptlet validation. It progressively expands the application search space when constraints cannot be satisfied, then validates each candidate bundle by executing scriptlets on the actual bundle topology.

---

## Phase 0: Initialization

- **Input:** `base: Bundle` (required apps + integrations)
- **Start:** `applications = base.applications.copy()`
- **Tracking:**
  - `outer_iterations = 0`, `max_outer = 10`
  - `inner_iterations = 0`, `max_inner = 20`
  - `expanded_interfaces = set()` (for circular dependency detection)

---

## Phase 1: OUTER LOOP (Expand Application Search Space)

**Loop Condition:** `while outer_iterations < max_outer`

### Step 1.1: Build Fresh Problem Space
- Create new Z3 `Solver`
- Call `_create_problem_space(solver, applications)`:
  - For each app in applications dict:
    - Create `app_var[app_name]` boolean variable
  - For each endpoint on each app:
    - Create `count_var[app:endpoint]` integer variable (≥ 0)
  - For each possible integration (provider endpoint matches requirer endpoint interface):
    - Create `int_var[integration]` boolean variable
    - Add implication: `int_var → (app_var[provider] ∧ app_var[requirer])`
    - Collect in integrations list
  - Link count to integrations: `count_var = sum(If(int_var, 1, 0) for int_var in integrations)`
  - Return `ProblemSpace(app_vars, integration_vars, endpoint_integration_counts)`

### Step 1.2: Add Base Bundle Constraints
- Call `_add_base_bundle_constraints(solver, problem_space, base)`:
  - For each base app:
    - Add constraint: `app_var[app] = True` (forced included)
  - For each base integration:
    - Add constraint: `int_var[integration] = True` (forced included)

### Step 1.3: Set Optimization Objective
- Call `_set_optimization_objective(solver, problem_space, applications)`:
  - Build objective: `minimize: sum(app_var[i] * cost[i]) + sum(int_var[j])`
  - Where `cost[i] = 1 / priority[i]` (lower priority = higher cost)
  - This incentivizes high-priority charms and minimizes total apps/integrations
  - Add to solver: `solver.minimize(objective)`

---

## Phase 2: INNER LOOP (Iterative Scriptlet Validation)

**Loop Condition:** `while inner_iterations < max_inner`

### Step 2.1: Solve with Z3

Call `solver.check()`:

#### Case 2.1a: UNSAT
- Extract `unsat_core = solver.unsat_core()` (tracked conflicting assertions)
- Call `_extract_blocking_interfaces_from_unsat_core(unsat_core)`:
  - Parse core assertions to identify patterns like `count[app:endpoint] >= 1`
  - Map endpoint to interface via charm metadata
  - Return `set[interface_names]`
- Call `_find_app_providers(blocking_interfaces, applications)`:
  - For each blocking interface:
    - Query charmhub: `find_charms(provides=interface, platform=base.platform)`
    - Filter: exclude apps already in `applications`
    - Select highest-priority provider
  - Return dict of new apps: `{app_name: Application}`
- If no providers found:
  - Raise `UnresolvableBundleError("Cannot satisfy: " + ", ".join(blocking_interfaces))`
- Add new apps: `applications.update(new_apps)`
- **Break inner loop** → Outer loop continues with new problem space

#### Case 2.1b: SAT
- Continue to Step 2.2

### Step 2.2: Extract Candidate Bundle
- Get model: `model = solver.model()`
- Call `_extract_bundle_from_model(model, base, problem_space)`:
  - `selected_applications = {}`
  - For each `(app_name, app_var)` in `problem_space.app_vars`:
    - If `model.eval(app_var, model_completion=True)`:
      - Add to selected: `selected_applications[app_name] = base.applications[app_name]`
  - `selected_integrations = set()`
  - For each `(integration, int_var)` in `problem_space.integration_vars`:
    - If `model.eval(int_var, model_completion=True)`:
      - Add to selected: `selected_integrations.add(integration)`
  - Return `Bundle(applications=selected_applications, integrations=selected_integrations, platform=base.platform, arch=base.arch)`

### Step 2.3: Validate with Scriptlets
- Call `_validate_bundle_with_scriptlets(candidate_bundle)` → `dict[app_name, ParsedConstraint]`:
  - `rejections = {}`
  - For each app in candidate:
    - If app has no scriptlet: skip
    - Build relation dict from candidate bundle:
      - `relations = {}` 
      - For each integration with requirer=app:
        - `endpoint = integration.requirer.endpoint`
        - Add relation ID to `relations[endpoint]`
      - Example: `{'database': [1, 2], 'cache': [3]}`
    - **[QUESTION: WHICH EVENT TO FIRE?]** (See "Open Questions" section)
    - Fire event with relations using `ScriptletInvoker`
    - If rejection:
      - Parse with `parse_error_code_rejection(rejection)` → `ParsedConstraint`
      - Add to rejections: `rejections[app_name] = constraint`
  - Return `rejections` dict (empty if all pass)

### Step 2.4: Check Result
- **If no rejections:**
  - ✅ **SUCCESS!** Log and `return candidate_bundle`
  
- **If rejections exist:**
  - For each `(app_name, constraint)` in `rejections.items()`:
    - Call `_apply_constraint(solver, problem_space, app_name, constraint)`:
      - (See Phase 3 below)
  - **Continue inner loop** → Re-solve with new constraints

---

## Phase 3: Constraint Application

Called during Step 2.4, adds constraints to active solver.

For each constraint type, add to solver:

### `required` constraint
```
count_var[app:endpoint] >= 1
```
Meaning: This endpoint must have at least one integration

### `mutual_exclusion` constraint
```
For each pair (ep1, ep2) in conflicting_endpoints:
  Or(count[app:ep1] = 0, count[app:ep2] = 0)
```
Meaning: At most one of these endpoints can have integrations

### `limit` constraint
```
count_var[app:endpoint] <= max_value
```
Meaning: Maximum number of integrations on this endpoint

### `conditional` constraint
```
Or(count[app:ep1] >= 1, count[app:ep2] >= 1, ...)
```
Meaning: At least one of these endpoints must have an integration

---

## Phase 4: UNSAT Handling

### Step 4.1: Extract Blocking Interfaces
- Call `_extract_blocking_interfaces_from_unsat_core(solver)`:
  - Get unsat core assertions
  - Look for patterns: `count[app:endpoint] >= 1`
  - Extract `endpoint` name
  - Look up endpoint definition in charm: `endpoint.interface`
  - Collect all interface names
  - Return `set[interface_names]`

### Step 4.2: Search Charmhub
- Call `_find_app_providers(blocking_interfaces, applications)`:
  - `new_apps = {}`
  - For each interface in blocking_interfaces:
    - Query charmhub: `find_charms(provides=interface, platform=base.platform, arch=base.arch)`
    - Filter results:
      - Exclude charms already in `applications`
      - Exclude charms in `expanded_interfaces` (prevent cycles)
    - Sort by priority (descending)
    - Take highest-priority provider
    - Fetch full charm via `charmhub_client.charm_from_store(...)`
    - Add to new_apps
    - Add interface to `expanded_interfaces`
  - Return `new_apps`

### Step 4.3: Expand and Restart
- Add new apps: `applications.update(new_apps)`
- **Break inner loop** (outer loop will rebuild problem space)

---

## Termination Conditions

| Condition | Result | Action |
|-----------|--------|--------|
| ✅ Scriptlets accept | SUCCESS | Return bundle |
| ❌ `inner_iterations >= max_inner` | FAILURE | Raise `UnresolvableBundleError("Max inner iterations")` |
| ❌ `outer_iterations >= max_outer` | FAILURE | Raise `UnresolvableBundleError("Max outer iterations")` |
| ❌ No providers for blocking interface | FAILURE | Raise `UnresolvableBundleError("Cannot satisfy: ...")` |
| ❌ Interface in `expanded_interfaces` again | FAILURE | Raise `UnresolvableBundleError("Circular dependency: ...")` |

---

## Data Structures

### ProblemSpace
```python
class ProblemSpace(BaseModel):
    app_vars: dict[str, z3.BoolRef]
    integration_vars: dict[Integration, z3.BoolRef]
    endpoint_integration_counts: dict[ApplicationEndpoint, z3.ArithRef]
```

### ParsedConstraint
```python
class ParsedConstraint(BaseModel):
    constraint_type: str  # 'required', 'mutual_exclusion', 'limit', 'conditional', 'data_validation'
    details: str | list[str]
    
    # Constraint-specific fields
    required_endpoint: str | None
    conflicting_endpoints: list[str] | None
    endpoint: str | None
    max: int | None
    acceptable_endpoints: list[str] | None
```

### Bundle
```python
class Bundle(BaseModel):
    applications: dict[str, Application]
    integrations: set[Integration]
    platform: str
    arch: str
```

---

## Key Implementation Notes

1. **Z3 Tracking:** Use `solver.set("core.validate", True)` to enable unsat_core generation
2. **Relation State:** Map Integration objects to relation IDs by counting integrations per endpoint
3. **Event Firing:** [SEE OPEN QUESTIONS]
4. **Circular Dependency:** Track expanded interfaces to prevent re-requesting same interface
5. **Objective Function:** Use `z3.PbLe` (pseudo-boolean) for weighted minimization if Z3.Optimize supports it
6. **Logging:** Debug each phase transition, constraint additions, and scriptlet results

---

## Open Questions (To Be Clarified)

1. **Which event to fire on scriptlets?** (Step 2.3)
   - `install` with actual relations from candidate bundle?
   - `relation-joined` for each relation?
   - Synthetic `bundle-ready` event?
   - What order if multiple events?

2. **Relation state representation** (Step 2.3)
   - How to map Integration topology to relation dict?
   - Should `relations = {'db': [1, 2], ...}` or different format?
   - How to get relation IDs for scriptlet?

3. **Charmhub search criteria** (Step 4.2)
   - Filter by platform and arch?
   - Any other constraints (channel, series)?
   - How to select among multiple providers?

4. **Max iteration limits:**
   - `max_outer = 10` reasonable?
   - `max_inner = 20` reasonable?
   - Should these be configurable?

5. **Error reporting:**
   - When raising `UnresolvableBundleError`, what "best bundle" to include?
   - Latest candidate or something else?

---

## Implementation Checklist

- [ ] Implement `build()` method with nested loops
- [ ] Implement `_set_optimization_objective()`
- [ ] Implement `_validate_bundle_with_scriptlets()`
- [ ] Implement `_extract_blocking_interfaces_from_unsat_core()`
- [ ] Implement `_find_app_providers()`
- [ ] Add circular dependency detection
- [ ] Add max iteration checking
- [ ] Add comprehensive logging
- [ ] Write unit tests for each phase
- [ ] Clarify open questions above
