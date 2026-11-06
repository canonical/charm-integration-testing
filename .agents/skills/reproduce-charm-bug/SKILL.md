---
name: reproduce-charm-bug
description: Reproduce and diagnose a reported Juju charm bug in a live deployment without changing charm source. Use when asked to investigate or reproduce a charm failure before implementing a fix.
---

# Task: reproduce a charm bug

## Goal

Recreate the reported behavior against the specified charm revision in an isolated
live Juju deployment, gather evidence that distinguishes causes, and report whether
the bug was reproduced. Do not modify charm source or present a hypothesis as a
confirmed cause.

## Steps

1. **Capture the exact report.** Identify the charm and repository, expected and
   observed behavior, operation that triggers it, revision/channel, Juju and
   substrate versions, and any CI, Test Observer, or log links. Ask only for
   details that cannot be discovered and materially affect the reproduction.

2. **Inspect before provisioning.** Read project and charm instructions, the
   relevant source and tests, and existing diagnostic artifacts. If this is a
   Test Observer failure, use the `run-charm-tests` skill to map its parameters
   and reproduce the exact scenario. Check existing controllers, models,
   workloads, and available resources before creating anything. Never reuse or
   destroy a model that was not created for this run.

3. **Choose the baseline.** Use the exact reported revision when available.
   Otherwise use the user's specified revision or the currently published charm,
   recording which one was tested. Do not substitute a locally modified charm
   when the question is whether the published charm reproduces the failure.
   Use the appropriate setup skill only if the required substrate is unavailable.

4. **Create an isolated reproduction.** Use a unique model/resource name and the
   smallest deployment that preserves the failure conditions. Record the exact
   deployment artifacts, commands, versions, and configuration. Do not modify
   unrelated host, controller, cluster, or production state.

5. **Validate the test preconditions.** Wait for Juju operations and hooks to
   settle. Confirm applications are active/idle where expected, workloads are
   ready, and required endpoints or relations are actually healthy. A bootstrap,
   deployment, or setup failure is not a reproduction of a later operation's
   bug. Verify the triggering action really ran.

6. **Observe the full outcome.** Capture relevant Juju status and debug logs,
   Kubernetes resources/events/logs when applicable, and the actual state before
   and after the triggering operation. For asynchronous deletion, inspect
   deletion timestamps and request/error evidence; do not infer a timing race
   merely because a resource existed shortly after a command returned. Use fresh
   canaries or timestamps when old successful state could mask failure.

7. **Classify and report.** State `REPRODUCED`, `NOT REPRODUCED`, or `BLOCKED`,
   name the phase reached, and separate observations from hypotheses. Include
   exact commands, revision, substrate, concise relevant evidence, and what
   remains uncertain. Do not propose an unverified root cause as fact and do not
   change the charm unless separately asked to fix it.

8. **Clean up only run-owned resources.** Inspect actual state before removing
   resources, especially after orchestration errors that may occur after mutation.
   Remove only the dedicated model and temporary resources created for this run.
   Verify cleanup; if teardown is stuck, diagnose it rather than deleting broad
   directories, containers, controllers, or shared workloads.

## Output

- Result and phase reached.
- Exact charm revision and environment tested.
- Reproduction command/scenario and observable evidence.
- What was not tested or remains uncertain.
- Cleanup status and retained evidence paths.
