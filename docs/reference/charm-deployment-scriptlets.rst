=============================================
Scriptlets for Charm Deployment Constraints
=============================================

What is a Scriptlet?
====================

A scriptlet is a small, executable code fragment written in a restricted language (typically a sandboxed Python or Starlark dialect). Scriptlets are designed to be:

**Sandboxed and Safe**
  - Executed in a restricted interpreter with no filesystem, network, or system access
  - Deterministic and side-effect-free
  - Only able to read inputs provided via a context object

**Versioned and Distributed**
  - Bundled with specific charm revisions
  - Versioned alongside the charm itself
  - Ensures validation logic matches the deployed charm version

**Interface-Driven**
  - Expose a standard interface for external systems to invoke
  - The interface and function signatures depend on the specific use case

Scriptlets are a general-purpose mechanism for embedding executable validation logic within a larger system. They are not specific to charm constraints—they are a tool that can be applied to many problems requiring flexible, versioned, sandboxed validation.

How Scriptlets Solve the Charm Constraint Problem
==================================================

The Problem
-----------

Charm deployment constraints are complex and varied (see ``charm-deployment-constraints.rst`` for a full taxonomy). Static YAML metadata is insufficient for expressing:

- Conditional requirements ("if A is present, B must be >= 2")
- Cross-charm dependencies ("if charm X is colocated with Y, Z must not be present")
- Transitive and global constraints ("at most one of this type in the entire model")
- Version-dependent logic ("this constraint only applies in version >= 3.0")

We need a mechanism that is both expressive enough to capture all real-world constraints and maintainable enough to be distributed per-charm.

The Solution: Per-Charm Scriptlets
-----------------------------------

Each charm ships with a scriptlet that encodes **all its deployment constraints** in executable code. This scriptlet:

- Lives alongside the charm metadata (e.g., ``constraints.py`` or similar)
- Is invoked by external systems (the bundle builder, Juju) to validate a deployment state
- Returns ``True`` if the deployment is valid, or error messages if invalid

This approach provides:

**Expressiveness**
  - Any constraint that can be expressed in code can be validated
  - No artificial limitations imposed by static schema

**Maintainability**
  - Constraints are co-located with the charm they describe
  - Changes to charm behavior can be accompanied by constraint updates
  - No centralized constraint database to maintain

**Single Source of Truth**
  - The same scriptlet is used by both the bundle builder (at design time) and Juju (at runtime)
  - No risk of validation logic divergence between systems

The Role of Z3 vs Scriptlets in Bundle Building
================================================

Bundle building uses **Z3 constraint solver** + **scriptlets** in a feedback loop. Understanding the division of responsibility is critical:

**Z3's Role (Basic Graph Search)**
  Z3 finds combinations of applications and integrations that satisfy basic structural constraints:
  
  - Interface names must match (both sides agree on the protocol)
  - Directionality is correct (requires connects to provides, not vice versa)
  - Basic graph connectivity (can form a valid deployment graph)
  
  Z3 does *not* encode:
  
  - Optional vs required integrations
  - Limits (max concurrent relations per endpoint)
  - Mutual exclusion (if endpoint A, not endpoint B)
  - Conditional requirements (if relation X exists, relation Y must exist)
  - Any charm-specific logic

**Scriptlets' Role (Full Constraint Validation)**
  Scriptlets validate *all* deployment constraints that cannot be expressed as simple metadata:
  
  - Required integrations (reject if missing in on_validate or on_deploy)
  - Limits (reject if too many relations in on_validate or on_integrate)
  - Mutual exclusion (reject if conflicting endpoints in on_validate or on_integrate)
  - Conditional requirements (reject if prerequisites not met in on_validate or on_deploy)
  - Relation data validation (reject if relation data incomplete or invalid)
  - Any charm-specific validation logic

**The Feedback Loop**
  
  1. Z3 proposes a bundle (apps + integrations with matching interfaces/directionality)
  2. Bundle builder fires on_validate event for each application with complete bundle topology
  3. Scriptlets validate the proposal, rejecting invalid configurations with error messages
  4. Z3 learns from rejections and generates new proposals that avoid the same errors
  5. Process repeats until a valid bundle is found

This separation allows:

- Z3 to stay simple (basic graph search)
- Scriptlets to express the full constraint language
- No duplication of constraint logic between Z3 and scriptlets

The bundle builder constructs event objects representing the proposed bundle state and invokes scriptlets with these events. For bundle validation, only the ``on_validate`` event is used, providing the complete topology at once.

Runtime Validation (Incremental Juju)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

In real Juju deployments, the scriptlet event model enables incremental validation:

- **on_deploy**: Fired when `juju deploy` is executed. Validates topology requirements (may see empty relations initially).
- **on_integrate**: Fired when `juju integrate` is executed. Validates the specific integration being added.
- **on_config**: Fired when `juju config` is executed. Validates configuration constraints.

Scriptlets validate each operation as it occurs, providing immediate feedback. Rejections can be treated as warnings (proceed anyway) or errors (fail operation), depending on deployment policy.

Bundle Validation (Design Time)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For bundle building, we need to validate a **complete, proposed deployment** before it exists. The bundle builder:

1. Constructs complete bundle topology with all applications and relations
2. Fires **only the on_validate event** with the complete bundle state visible
3. Collects all rejections from the validation
4. Converts rejections to Z3 constraints
5. Uses Z3 to generate a new proposal that avoids the rejected constraints
6. Repeats until a valid bundle is found or determined unsolvable

This approach ensures:

- **Efficiency**: Single event call per app instead of N events (deploy + integrate × relations + config)
- **Complete information**: Z3 receives all constraint violations at once for optimal solving
- **Same scriptlet code**: Charm authors write once, works for both bundle validation and incremental Juju
- Constraint validation is fully expressible in scriptlet code

How Scriptlets Are Invoked (Per JU055)
=======================================

JU055 defines an **event-driven, observer pattern** for scriptlet invocation. Scriptlets do not expose a simple ``validate()`` function. Instead, they follow a three-phase execution model:

Execution Phases
----------------

**1. Script Setup**
  The scriptlet is parsed, compiled, and its global scope is executed. During this phase, all methods on the application object (e.g., ``juju``) are unavailable.

**2. Initialization (``init()``)**
  The scriptlet's ``init()`` function is called. During this phase:
  
  - Only the ``observe()`` method is available on the application object
  - The scriptlet registers event handlers using ``juju.observe("event_name", handler_function)``
  - All other methods on the application object are unavailable
  
  Example::
  
    def init():
        juju.observe("validate", on_validate)
        juju.observe("deploy", on_deploy)
        juju.observe("integrate", on_integrate)
        juju.observe("config", on_config)

**3. Event Handling**
  When events occur in the host application (e.g., config changes, unit additions), the host invokes the scriptlet by calling ``ModuleGroup.HandleEvent(event, threadLocals)``:
  
  - The ``observe()`` method is now unavailable
  - All registered handlers for that event are called in registration order
  - Each handler receives an ``Event`` object with event-specific attributes
  - Handlers can reject the event using ``event.reject(reason)``

Event Object Structure
----------------------

The ``Event`` object passed to handlers contains:

- **Name**: The event identifier (e.g., ``"config_change"``)
- **Attributes**: Event-specific data accessible as object attributes

  Example::
  
    def on_config_change(event):
        cfg = event.config
        delta = event.delta
        if 'foo' in delta and delta['foo'].len() > 50:
            event.reject('foo', 'value too long')

Application Object
------------------

The application object (e.g., ``juju``) provides access to the deployment state. It is available during event handling but not during ``init()`` or script setup. The exact attributes depend on the host application, but typically include:

- Current configuration (``juju.config``)
- Application state (``juju.app``, ``juju.apps``)
- Model topology information

Concurrent Execution
--------------------

According to JU055, calls to ``HandleEvent()`` can be concurrent. Each handler is executed independently in its own Starlark thread, with thread-local values provided by the host.

Event Types and Design Decisions
=================================

JU055 does not formally specify which events exist—this is left to the host application. The specification only defines the **mechanism** for event handling (observer pattern, rejection API), not the **semantics** of what events should be fired.

Design Decision: Split Event Model
-----------------------------------

We implement **four validation events** to support both complete bundle validation and incremental Juju operations:

**Bundle Validation Event:**
- **on_validate**: Called with complete bundle topology visible. Z3 gets all rejections at once for efficient solving.

**Incremental Juju Events:**
- **on_deploy**: Called when an application is deployed. Validates topology-level constraints (required relations, global cardinality).
- **on_integrate**: Called when a relation is being added. Validates relation-specific constraints (endpoint limits, mutual exclusion).
- **on_config**: Called when application configuration is changed. Validates config-level constraints.

Rationale
---------

**Why not just one event?**

If we only used ``on_validate``, real Juju deployments would validate only at the end, after 90% of the infrastructure is deployed. This is a poor user experience:

.. code-block:: bash

   juju deploy wordpress-k8s       # succeeds
   juju deploy mysql-k8s           # succeeds
   juju integrate wordpress mysql  # succeeds
   juju config wordpress ...       # FAILS with validation error

By that point, resources are allocated, units are starting, time is wasted.

**Why not just three events?**

If we only used ``on_deploy``, ``on_integrate``, ``on_config``, bundle validation would fire events in sequence with the same complete state repeated 13+ times (redundant), and Z3 would get rejections incrementally instead of all at once (inefficient solving).

**Solution: Support Both**

Scriptlet authors write **once**:

.. code-block:: python

   def init():
       juju.observe("validate", on_validate)      # For bundle validation
       juju.observe("deploy", on_deploy)          # For incremental Juju
       juju.observe("integrate", on_integrate)
       juju.observe("config", on_config)
   
   def on_validate(event):
       # Complete bundle validation - all relations visible
       if 'db' not in event.relations:
           event.reject('required', 'db')
   
   def on_deploy(event):
       # Incremental Juju validation - may have no relations yet
       # Same code, but context differs
       if 'db' not in event.relations:
           event.reject('required', 'db')
   
   def on_integrate(event):
       if event.endpoint == 'database':
           if len(event.relations['database']) > 5:
               event.reject('limit', 'database:5')
   
   def on_config(event):
       if event.config.get('max_connections', 0) > 1000:
           event.reject('max_connections', 'exceeds limit')

Hosts call **different events** depending on context:

- **Bundle Builder**: Fires only ``on_validate`` with complete bundle state. Z3 gets all rejections at once, solves efficiently.
- **Real Juju**: Fires ``on_deploy``, ``on_integrate``, ``on_config`` as operations occur. Users get validation feedback immediately after each operation.

**Benefits:**

1. **Single source of truth**: Charm authors write validation logic once
2. **Efficient bundle validation**: Complete state upfront, all rejections together
3. **Better UX for incremental Juju**: Validation fails early, before wasting resources
4. **Future-proof**: Juju can adopt this model directly
5. **Backward compatible**: Scriptlets can implement just one or both sets of events

Event Context
-------------

**on_validate (Bundle Validation):**
   - ``event.relations``: Complete bundle topology with all applications and integrations
   - ``event.config``: Application configuration
   - ``event.reject(field, reason)``: Signals constraint violation

**on_deploy (Incremental Juju):**
   - ``event.relations``: Relations visible at deployment time (initially empty, or pre-existing if redeploying)
   - ``event.config``: Application configuration
   - ``event.reject(field, reason)``: Signals validation failure

**on_integrate (Incremental Juju):**
   - ``event.endpoint``: Endpoint being integrated
   - ``event.relations``: Relations including the one being added
   - ``event.remote_app``: Name of remote application
   - ``event.reject(field, reason)``: Signals integration failure

**on_config (Incremental Juju):**
   - ``event.config``: New configuration values
   - ``event.delta``: Changes since last config (optional)
   - ``event.reject(field, reason)``: Signals configuration failure

Usage Examples
--------------

**Bundle Builder:**

.. code-block:: python

   # Instantiate harness with bundle topology
   harness = Harness(charm_class, ...)
   
   # Add all relations from bundle
   for relation in bundle.relations:
       harness.add_relation(...)
   
   # Fire ONLY on_validate with complete state
   harness.fire_event("validate", event)
   
   # Collect all rejections
   rejections = event.rejections  # All constraints at once
   
   # Pass to Z3 as a batch
   z3_constraints.add(rejections)

**Real Juju Deployment:**

.. code-block:: bash

   juju deploy wordpress-k8s
   # Fires on_deploy event
   # Scriptlet checks if 'db' in relations (NO → rejects)
   # Juju prints warning: "database integration required"
   # Operation proceeds (or fails, depending on strictness)
   
   juju integrate wordpress-k8s mysql-k8s
   # Fires on_integrate event with endpoint="db"
   # Scriptlet validates mutual exclusion, limits, etc.
   # Juju reports error if rejected (operation fails immediately)
   
   juju config wordpress setting=value
   # Fires on_config event
   # Scriptlet validates config constraints
   # Juju reports error if rejected

This design ensures:

- Same scriptlet works for both bundle validation and incremental Juju
- Bundle validation is fast (complete state, batch rejections)
- Incremental Juju has good UX (fail early, clear messages)
- Charm authors write once, don't duplicate validation logic

Event Structure Specification
=============================

Scriptlet events provide access to deployment state through a simple event object interface:

**Event Attributes**

All events provide:
  - ``event.relations``: Dictionary mapping endpoint names to lists of related applications
  - ``event.config``: Application configuration dictionary
  - ``event.reject(field, reason)``: Method to signal validation failure

Additional attributes for specific events:
  - ``on_integrate``: ``event.endpoint`` (name of endpoint being integrated), ``event.remote_app`` (remote application name)
  - ``on_config``: ``event.delta`` (optional, shows configuration changes)

**Event Context by Type**

The content of ``event.relations`` differs by event type:

- ``on_validate``: Complete bundle topology with all proposed relations
- ``on_deploy``: Relations visible at deployment time (typically empty for new deployments)
- ``on_integrate``: Relations including the one currently being added
- ``on_config``: Current relations (unchanged by config operation)

For implementation details and examples, see the Event Types and Design Decisions section above.

References
==========

This document synthesizes concepts from:

- **JU034 - Scriptlets**: High-level specification of the scriptlet mechanism
- **JU055 - Scriptlet Interface**: Detailed interface definition and context object structure
- **charm-deployment-constraints.rst**: Complete taxonomy of constraint types that scriptlets must express

Summary
=======

**What scriptlets are**: A general-purpose mechanism for sandboxed, versioned, executable validation logic.

**How we use them**: Per-charm scriptlets encode all deployment constraints for that charm, serving as the single source of truth for both bundle building (with Z3 constraint solving) and runtime validation (in Juju). This approach overcomes the limitations of static YAML and supports the full range of real-world deployment requirements.
