# DA147 UX of Statuses

| Index | DA147 |
| :---: | :---- |
| **Title** | UX of Statuses |
| **[Status](https://docs.google.com/document/d/1lStJjBGW7lyojgBhxGLUNnliUocYWjAZ1VEbbVduX54/edit?usp=sharing)** | Approved |
| **Authors** | [Mia Altieri](mailto:mia.altieri@canonical.com) |
| **[Type](https://docs.google.com/document/d/1lStJjBGW7lyojgBhxGLUNnliUocYWjAZ1VEbbVduX54/edit?usp=sharing)** | Standard |
| **Created** | 2024-12-18 |

# Roles

| Owner |  |
| :---: | :---- |
| **Writers** | [Mia Altieri](mailto:mia.altieri@canonical.com) |
| **Decision Maker** | [Mehdi Bendriss](mailto:mehdi.bendriss@canonical.com) |
| **Reviewers** | [Mykola Marzhan](mailto:mykola.marzhan@canonical.com) |

# Abstract

We outline two possible UXs for statuses, specifically for what a user should expect to see during specific events/hooks. We outline which types of statuses take priority, outline when statuses are computed, and when statuses are shown. We compare two UXs which have two separate approaches for showing statues, i.e. single vs multiple statuses.

# Rationale

We plan to update how we show, compute, and store statuses. Before an implementation can be decided a UX must be agreed on. It is needed to update our current handling of statuses, since most charms set statuses throughout the code, resulting in: statuses getting overwritten, technical debt, flickering statuses, and lost status state. All of the above lead to a poor user experience, leading to a need for a unified UX of statuses. We aim to outline our own UX, rather than involve the UX team directly  as we haven't discussed prior design related decisions with them in the past. We should first build that relationship and plan how we want to work together, before getting them involved in this spec review.

# Overview

In this spec we outline two approaches, with the aim to identify the correct approach to UX for statuses:

- Single Status Approach  
- Multiple Status Approach

In the future, Juju will support displaying multiple statuses. Until then a unit and an application can only display one status. The Single Status Approach shows one *prioritised* status in the message area of a status, the Multiple Status Approach shows multiple statuses in the message area of a status. i.e. 

- **Single Status Approach** \- 

```
BlockedStatus("This is the most important status")
```

- Multiple Status Approach \- 

```
BlockedStatus("C3-status. Run `status-detail`: 1 action required; 4 additional statuses")
```

For the most part the approaches in the UX will be the same for the Single Status Approach and Multiple Status Approach. Where the two approaches differ, the differences are outlined and indicated by the Δ symbol.

# App Status

In this document we do not cover app status, instead we consider the same approach for app status as the unit status. 

## Definitions 

- **Component:** a part of the code dedicated to its own feature (i.e. TLS, backups, charm fundamentals, etc)   
- **Running status:** status that indicates there is a running a **long** operation (i.e. enabling tls, or running a backup action for some databases, upgrade)  
- **Blocking running status:** a type of running status that blocks other events from executing (i.e. the draining of a shard prevents other events from running)   
- **Async running status:** a type of running status that runs across hooks.   
- **Deferring status:** status that indicates the charm is waiting for something to happen in a deferred event  
- **Component status:** A status for a component, ie all other statuses (note, a deferring status falls under the component status category)  
- **Sub-state status:** A component can have multiple valid status, i.e. A TLS component can have one certificate expiring and another certificate rotating, in this case these would be considered as sub-state statuses.     
- **Up to date status:** a status that has been recently computed by considering all component statuses. (recently \= during the executing event)   
- **Component relevancy:**  how the status of one component is prioritized over another. (expanded on later)  
- **Status type Relevancy:**  how relevant a status is when compared to other feasible statuses, based on status type.  (expanded on later)   
- **Relevant status**: how relevant a status is compared to another status. (explicitly defined later)

# Specification

## What status should be shown *during* an event?

The only statuses that should be set *during* an event are running statuses. 

Running statuses occur in:

- **Lifecycle hooks**  
  - These statuses are shown if there is no other higher-priority status (According to **How To Compute Status Relevancy** section)  
- **Juju actions**   
  - These take priority over any other status (including blocked) and will be the first to be shown.


Regardless of which running status the following is always true:

- Running statuses are always logged so the user can check logs for long running operations

Examples for motivation:

1\. When enabling TLS we must restart the daemon. On a heavily loaded database this can take minutes, so by setting a status in the event ("Enabling TLS"), a user does not have to wonder "why is this hook still executing after several minutes? 

2\. When removing a replica/shard in a "storage\_dettached" event. That operation can take a long time and that event can only occur once, making the status essential for the user.

In both of these, without a running status, the user will be left to wonder why an event appears to be "stuck" in executing, when in fact the event is running a long running operation. This information is very helpful for the user

### Δ Single Status vs Multiple Status

In the **single status** approach the running status will be the *only* status shown. i.e.  
	  
	

```
MaintenanceStatus("Long Running Operation")
```

In the **multiple status** approach the running status will be shown at the *beginning* of the list of statuses,  i.e.

```
MaintenanceStatus("Long Running Operation. Run `status-detail`: 1 action required; 3 additional statuses")
```

## What status should be shown *after* a hook?

A hook can trigger multiple events (i.e. deferred or manually emitted events), a status that is set at the end of an event can immediately get overwritten by a deferred event, leading to a flickering status and a poor UX.  Hence statuses should be computed and shown after a hook. Users can see previous statuses by viewing logs or by using the command: **juju show-status-log** 

The status that is shown after a hook, depends on the calling event.

### Update Status

The most *relevant* and  *status*(es)Δ for the components. This saves long status computations for update-status events.    

### All other events

The most *relevant* status(es)Δ according to the stored states of the components *(the stored state of the components is perpetually updated during the lifecycle of the charm)* 

Δ \- more than one status is shown in the case of **Multiple Status** approach, otherwise one status is shown in the **Single Status** approach

## How to compute status relevancy?

In a previous iteration of this spec we considered computing relevancy based on the component relevancy first and then the status relevancy. But after much feedback requesting to go by status type, the component based approach was deprecated. This approach, along with feedback \+ comments can be seen on the document tab titled: “[\[**Deprecated Approach\]** Component based relevancy.]()”

Similar to the approach for [charmed Kubeflow](https://github.com/canonical/charmed-kubeflow-chisme/blob/3f13ba3f6f4daaf584d0b757eeb3014f0ae91714/src/charmed_kubeflow_chisme/status_handling/multistatus.py#L22-L27) we will order status relevancy by the status type. As is done in [the operator framework](https://github.com/canonical/operator/blob/d180ad2b6610fcabde798b31cc1417290dc80369/ops/charm.py#L1117-L1120).  i.e.:

Error \> Blocked \> Maintenance \> Waiting \> Active  \> Unknown 

In the case that many components have the same status type, the component with a higher relevancy is shown first. Component relevance is determined by the developer. An example would be: 

upgrade component \> charm-config component \> db-daemon component \> tls component \> etc

i.e:

C1 \> C2 \> C3 \> … \> Cn 

### Single Status Relevancy

In Single Status relevancy we show the first status on top of the stack according to the prioritization of status type and component type

Example 1:   
The following component statuses:  
C1 \- None  
C2 \- Maintenance(C2-status), Waiting(C2-status)  
C3 \- Blocked(C3-status)  
C4 \- Blocked(C4-status)  
C5 \- Maintenance(C5-status)

Note Cx-status \- is a complete status message for the user, i.e. “Missing relation with X” or “Primary shard”, “TLS expiring” ,etc.

Would result in a status priority of:  
Blocked(C3-status),  Blocked(C4-status), Maintenance(C2-status), Maintenance(C5-status), Waiting(C2-status)

Would result in a shown status of:  
Blocked(C3-status)

### Multiple Status Relevancy

In multiple status relevancy, we use the same ordering from single status, but show more than one status at a time. How we show the multiple statuses is still up for debate and we provide three options.  
 

Example 1:   
The following component statuses:  
C1 \- None  
C2 \- Maintenance(C2-status), Waiting(C2-status)  
C3 \- Blocked(C3-status)  
C4 \- Blocked(C4-status)  
C5 \- Maintenance(C5-status)

Note Cx-status \- is a complete status message for the user, i.e. “Missing relation with X” or “Primary shard”, “TLS expiring” ,etc.

Would result in a status priority of:  
Blocked(C3-status),  Blocked(C4-status), Maintenance(C2-status), Maintenance(C5-status), Waiting(C2-status)

Would result in a shown status of:  
BlockedStatus("C3-status. Run \`status-detail\`: 1 action required; 4 additional statuses")

Other options for showing status were considered, but engineers preferred the above status formatting.

Separating the Status from Action

In our current approach to statuses we have limited characters to express the state and required actions by the user. In rich statuses we can outline these directly, so in the new approach for status UX we split the status and the required action. i.e.   
	

```
Refreshing. Check units >=11 are healthy & run `resume-refresh` on unit 10. To rollback, `juju refresh --revision 10007`
```

The status is:  
	

```
Refreshing.
```

The action is:  
	

```
Check units >=11 are healthy & run `resume-refresh` on unit 10. To rollback, `juju refresh --revision 10007`
```

## Logs

After some discussion with Jon, we decided to move the viewing of rich status viewing to the action `status-detail`. This prevents the statuses of the charm components from spamming the logs.

We will still output some status information to the logs, but in a compressed json output, in order to save log space but keep information visible.

Running statuses will be shown in the logs as soon as they are set and after each event we do a json dump of all logs aggregated by status type to be of the format:  
 

```
{
Blocked: [[<component-name>, <status>, <action>, <reason>], ...], 
Maintence: [[<component-name>, <status>, <action>, <reason>], ...], 
...
}
```

## Action: status-detail

As suggested by [Mohamed Nsiri](mailto:mohamed.nsiri@canonical.com) it would be nice to have a standard action to display all the status with rich details. 

The action `status-detail` would return the status(es) of all components. The action would take an optional argument `recompute` which when set to true would fully recompute all statuses, otherwise it would return the stored statuses. By default `recompute=False`. 

The output of this action would look like:

	

```
<Stored | Recomputed> statuses:

App:
+------------+----------------+---------+---------+--------+
| Status     | Component Name | Message | Action  | Reason |
+------------+----------------+---------+---------+--------+
| Blocked    | <component 1>  | <...>   | <...>   | <...>  |
| Blocked    | <component 2>  | <...>   | <...>   | <...>  |
| Maintence  | <component 3>  | <...>   | <...>   | <...>  |
| Waiting    | <component 1>  | <...>   | <...>   | <...>  |
+------------+----------------+---------+---------+--------+


Unit:
+------------+----------------+---------+---------+--------+
| Status     | Component Name | Message | Action  | Reason |
+------------+----------------+---------+---------+--------+
| Blocked    | <component 1>  | <...>   | <...>   | <...>  |
| Blocked    | <component 2>  | <...>   | <...>   | <...>  |
| Maintenance| <component 3>  | <...>   | <...>   | <...>  |
| Waiting    | <component 1>  | <...>   | <...>   | <...>  |
| Active     | <component 4>  | <...>   | <...>   | <...>  |
+------------+----------------+---------+---------+--------+


*Note:* only recomputed statuses for <unit name> to recompute for other units run action on desired unit. To recompute app, run on leader.

```

### Action output as json:

Users of our charms may want to perform some logic (or compute something) as the result of the status of the charm. For that it is likely that they would want some output that is easily parsable; i.e. json. While juju does provide a way in actions to output the result as a json, it simply outputs the results of the output as a json i.e.  
	

```
{"<app-name>/<unit-id>":{"id":"2","results":{"<json-status-field-name>":"<result-including-app-and-unit-status>","return-code":0,"secret-id":"<app-name>.app"},"status":"completed","timing":{"completed":"2025-02-25 10:30:17 +0000 UTC","enqueued":"2025-02-25 10:30:15 +0000 UTC","started":"2025-02-25 10:30:15 +0000 UTC"},"unit":"<app-name>/<unit-id>"}}
```

To enable users to receive the result as a json, we must return another field called `json-output`. Which would be a json-ified version of the table above. 

# Exception: critical statuses

In some cases where there is a critical status i.e. upgrade related statuses. We permit the status to *overwrite* *the* *entire* status and use the full 120 characters available. While this breaks the UX it provides necessary functionality for critical statues. 

What makes a status critical is that it is both:

- The very first action a user should take  
- The user might need to take this action immediately 

If you believe you have a critical status that should override the functionality outlined in this spec, please include the request as a PR requesting the review of the concerned / corresponding Data Platform Engineering Manager(s), as the goal is to unify the UX of all Data Platform charms.. 

# Cons of Single Status and Multiple Status approach

**Single Status**

- Obstructs pertinent statuses from user (this is significant)  
- Difficulties with status prioritization implementation  
- Testing of statuses might be difficult  
- Possibility for flickering statuses

**Multiple Status**

- Conflicting status message vs status type could be confusing to reader  
- All docs will need to be updated  
- Testing of statuses might be difficult

# Conclusion

After considering both approaches to statuses, it is believed that the better approach is the Multiple Status approach. This is due to the fact that single statuses obstruct pertinent statuses from the user, which hinders our ability to provide a rich+transparent state of the application.

It is also useful for us to show multiple statuses since it will ease our transition once Juju enables the ability to show multiple statuses. 

An example of how single statuses obstruct pertinent statuses from the user can be the following. Consider a charm which needs several operations done by the user:

- Blocked \- TLS needs to be enabled  
- Blocked \- Backup needs to be configured

When a user first deploys a charm, there might be several relations needed or configurations needed so it is not unlikely for this to occur. 

If we only show one (and not the other) we “hide” the full state from the user. The user might be surprised that after resolving TLS, the backup needs to be configured. It might lead them to wonder “how many things will I have to resolve before the charm goes into active state.” By showing all statuses the user can get a realistic view of the state of the charm.

With multiple statuses a user can also “breathe easy” knowing that they can see with certainty that a component is healthy. I.e.

- Blocked \- s3-credentials are incorrect  
- Blocked \- config-server has TLS enabled but shard does not  
- Blocked \- shard does not support client interface    
- Active \- Primary

In this case the user knows with certainty that full view of the charm. In single status they might think “all they need to do” to make the charm healthy is to provide correct credentials for s3, but here they get the full picture.

# Out of Scope

The following is out of scope and will be defined in a separate spec once the UX is agreed upon:

1. Computation of statuses  
2. Storing of component state  
3. Updating/Wiping/Adding component status  
4. Architecture  
5. Methods for collecting status

# \[Optional read\] Multi-event example

# \[Optional read\] Multi-event example

Below is an example of how statuses are prioritized over the timespan of several events/hooks. The example aims to solidify some of the definitions and concepts from this spec. It presents *no new information or concepts.*

Lets consider a charm with two components (note: real world charms will have many components)   
CT  \- TLS component  
CB \- Backup component 

Where:  
CT \> CB

| Event | Status of Component (before event) | Status of Components (after event) | Status Shown \- Single Status | Status Shown \- Multiple Statuses | Explanation |
| :---- | :---- | :---- | :---- | :---- | :---- |
| tls-event1  (long running block)  | CT  \- Active(CT-status) CB \- Maintenance(CB-status) | CT \- Active(CT-status) CB \- Maintenance(CB-status) | Maintenance(Enabling TLS)  | Maintenance(Maintenance-Enabling TLS, Maintenance-CB-status) | tls-event1 begins with a long running block. In long running blocks the running status is shown. (See Definition **Running status**) |
| tls-event1 (after long running block) | CT \- Active(CT-status) CB \- Maintenance(CB-status) | CT \- Waiting(CT-status) CB \- Maintenance(CB-status) | Maintenance(CB-status)  | Maintenance(Maintenance-CB-status, Waiting-CT-status) | In this scenario, the tls-event1 deferred.  Resulting in a Deferred status for component CT  Single Status \+ Multiple Status: We order based on their status-type relevancy (See Definition **Deferred status** \+ **How to compute status relevancy?**)  |
| backup-event1 | CT \- Waiting(CT-status) CB \- Maintenance(CB-status) | CT \- Waiting(CT-status) CB \- Blocked(CB-status) | No update | No update | In this case the backup event resulted in a blocked status for CB (likely because of issues in TLS)  Note: no status is updated, since the deferred event below (tls-event1)  executes in the *same* hook. To prevent flickering statuses across hooks we only set statuses at the end of hooks (See **What status should be shown after a hook?**) |
| tls-event1 (deferred event) | CT \- Waiting(CT-status) CB \- Blocked(CB-status) | CT \- Active(CT-status) CB \- Blocked(CB-status) | Blocked(CB-status)  | Blocked(Blocked-CB-status, Active-CT-status) | In the deferred event, TLS resolved its issues. Single Status \+ Multiple Status: We order based on their status-type relevancy  |
| update-status | CT \- Active(CT-status) CB \- Blocked(CB-status)  | CT \- Waiting(CT-status) CB \- Blocked(CB-status)  | Blocked(CB-status) | Blocked(Blocked-CB-status,Waiting-CT-status) | This event recalculates statuses for  CT \+ CB to the fullest extent. In this scenario, the recalculation of CT resulted in a Waiting status. (See **What status should be shown after a hook?** \+ **How to compute status relevancy?**)   |
| backup-event2 | CT \- Waiting(CT-status) CB \- Blocked(CB-status)  | CT \- Waiting(CT-status) CB \- Blocked(CB-status\-critical)  | Blocked(CB-status\-critical)  | Blocked(Blocked-CB-status\-critical, Waiting-CT-status) | Now a new backup-event has been run, resulting in a critically blocked state for backups.  (See **Δ Single Status Exception to Relevancy**)  |
| action-event1 | CT \- Waiting(CT-status) CB \- Blocked(CB-status\-critical))  | CT \- Active() CB \- Active()  | Active() | Active() | In this case the user ran an action which resolved both components. In this case there is no status to show  |
| action-event2 | CT \- Active() CB \- Active()  | CT \- Active() CB \- Active(CB-status)  | Active(CB-status) | Active(Active-CB-status) | In this case the user ran an action which resulted in an active status with pertinent information for CB \- so that status is shown.   |

# \[Deprecated Approach\] Component based relevancy

Relevancy takes a very different meaning when we can show *only* one status versus *many* statuses. So we will define these separately for each approach. But first we introduce two measurements for relevancy:

### Component Relevancy 

Component relevance is determined by the developer. An example would be: 

upgrade component \> charm-config component \> db-daemon component \> tls component \> etc

i.e:

C1 \> C2 \> C3 \> … \> Cn 

### Status type Relevancy 

When comparing statuses regardless of component, the priority is determined on the type of the status. As is done in [the operator framework](https://github.com/canonical/operator/blob/d180ad2b6610fcabde798b31cc1417290dc80369/ops/charm.py#L1117-L1120).  ie:

Blocked \> Maintenance \> Waiting \> Active 

### Δ Single Status Relevancy

For the single status, the relevance of a status is *first* computed by component relevancy. The status from an earlier component takes priority over the statuses from later components. Since a component can have *multiple* statuses, the status displayed is based on the status type relevancy.

#### Exception to Single Status Relevancy

In the case that a component Cn  has a status which is *critical* and takes precedence over all the components before it. That status is shown. In the rare case that Cn has a critical status and Cm also does (m \< n), the one from Cm is shown.

Example 1: 

The following component statuses:

C1 \- None  
C2 \- Maintenance(C2-status), Waiting(C2-status)  
C3 \- Blocked(C3-status)  
C4 \- Waiting(C4-status)

Would result in:

Maintenance(C2-status)

### Δ Multiple Status Relevancy

Since we can show more than one status at a time we use the status type to order the statuses that are shown. In the case that more than one component has the same status-type, the more relevant component is shown first. 

Example 1: 

The following component statuses:

C1 \- None  
C2 \- Maintenance(C2-status)    
C3 \- Blocked(C3-status)  
C4 \- Waiting(C4-status)  
C5 \- Maintenance(C5-status)

Note Cx-status \- is a complete status message for the user, i.e. “Missing relation with X” or “Primary shard”, “TLS expiring” ,etc.

Would result in:

BlockedStatus(Blocked-C3-status, Maintenance-C2-status, Maintenance-C5-status, Waiting-C4-status)

In the case that the contents of the status would exceed the maximum length of 120 chars, the following status would be displayed:

BlockedStatus(Blocked-C3-status, Maintenance-C2-status, Waiting-C4-status, see logs for all statuses)

