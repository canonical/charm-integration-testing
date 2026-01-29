

| Index | JU034 |
| :---: | :---- |
| **Title** | Scriptlets |
| **[Status](https://docs.google.com/document/d/1lStJjBGW7lyojgBhxGLUNnliUocYWjAZ1VEbbVduX54/edit?usp=sharing)** | Obsolete |
| **Authors** | [John Meinel](mailto:john.meinel@canonical.com), [Arnaud Delobelle](mailto:arnaud.delobelle@canonical.com), [Caner Derici](mailto:caner.derici@canonical.com) |
| **[Type](https://docs.google.com/document/d/1lStJjBGW7lyojgBhxGLUNnliUocYWjAZ1VEbbVduX54/edit?usp=sharing)** | Standard |
| **Created** | 2021-04-30 |

[Background and motivation](#heading)

[Existing implementations/capabilities (if applicable)](#existing-implementations/capabilities-\(if-applicable\))

[Proposed solution and implementation](#proposed-solution-and-implementation)

[Database Changes](#database-changes)

[Cross service Dependencies](#cross-service-dependencies)

[Milestones](#milestones)

[Metrics & KPIs](#metrics-&-kpis)

[Security](#security)

[Out of scope/Non-goals](#out-of-scope/non-goals)

[Unresolved questions](#unresolved-questions)

[Is it Done?](#is-it-done?)

[How to use a spec / is it ready?](#how-to-use-a-spec-/-is-it-ready?)

[Spec History](#spec-history)

# 

#  {#heading}

# **Background and motivation**

*This is the explanation of the feature from a high level point of view. It should describe the motivation to have the feature from the point of view of stakeholders (snap publishers, collaborators, end users, sales people, etc).*  
*It is very likely that the feature is solving a given problem or need from an external client of our system, please explain that here, ideally using clear examples.* 

*User Story, PR/Blog draft, etc.*

What we want is 'YAML on Steroids'  
[Golua Safe Environment](https://docs.google.com/document/d/1GriGMd_F3_jCmt_f0hXNfHfy4wLBXF_qJGf_oFnMBaU/edit#heading=h.4dyz0q9jhiik)

# **Existing implementations/capabilities (if applicable)** {#existing-implementations/capabilities-(if-applicable)}

*Many of the features we are developing are improvements or extensions to current system that already offer some level of functionality for the feature.*  
*This section should explain what we have at the time of the writing and why we need to change/improve/extend it.*  
*In this section we could also explain some fine grain details like what services are involved, whether that needs changing or not, etc. Try to use a subsection for each service or key point to be highlighted.*

# **Proposed solution and implementation** {#proposed-solution-and-implementation}

*This section would describe the proposed solution/s, and the service/s affected. Be sure to add the rationale for the decision making of the plan, and* 🆕 ***please be sure to confirm with the stakeholders whether there are external deadlines affecting the delivery of this feature*** 🆕*.*

## **Database Changes** {#database-changes}

*Describe any substantive changes or additions to database schema.*

## **Cross service Dependencies** {#cross-service-dependencies}

*Detail new direct internal cross-service dependencies, e.g. snapdeltas pushing information to snaprevs.*

## **Milestones** {#milestones}

*Describe the milestones and how tasks are grouped for estimation and execution. Be sure to include blockers between milestones/tasks if there are any.*  
*Please add a very rough estimate for each milestone (likely to be in weeks), and include an explicit items for:*

* *Indicate if there are things that need further investigation.*  
* *Environment setup if this feature will not be part of an existing env.*  
* *Staging/production rollout time-slot allocation, including some buffer for the unexpected.*  
* 🆕 *Expectations around deadlines and delivery dates.*

*Create a trello card with checklist(s) of breakdown of work items and milestones to ease visual tracking.*

## **Metrics & KPIs** {#metrics-&-kpis}

*Have a dedicated subsection for listing the metrics that will be added in order to ensure the feature can be measured on both health and success (are users really using this? Is it making their life easier? etc)*

## **Security** {#security}

*Will this feature require new keys or secrets? Will it use existing keys or secrets? What are the implications of having the secrets leaked or compromised? Identification of all secrets needed by the application/feature and a description of the security implications and requirements for this feature. If there are no security implications please state so explicitly so it's known that some thought has been given to the topic.*

# **Out of scope/Non-goals** {#out-of-scope/non-goals}

*\[Choose your preferred spelling for the section title and add things here that are recognized as being peripheral to this project.\]*

# **Unresolved questions** {#unresolved-questions}

*If there are blockers about any feature definition, please list them explicitly in their own sub-section.*  
*If the issues/questions are better discussed in the Snap forum, please add here links to the relevant post/s.*

# **Is it Done?** {#is-it-done?}

☐ Spec finalized \- (*Sent to snapstore-crew ML for comment. Bring up at team meeting if needed*.)  
☐ If this spec is part of multi-phase work, all out-of-scope items have been published to a phase N+1 spec  
☐ User documentation written (including forum post if applicable)  
☐ Operational Playbook written (*if applicable*)  
☐ Feature flag removed  
☐ Metrics published on KPI dashboard

*\[Add other important but not urgent items above as they are discovered during the project.  Oh, and you may need this: ☑*  
*Lastly, please remove the "\[" and "\]" from the table below when you start adding real dates for real events.\]*

# **How to use a spec / is it ready?** {#how-to-use-a-spec-/-is-it-ready?}

| Thing | Done? ☐ ☑ (or at least read/understood) |
| :---- | :---- |
| Create spec early and evolve as our understanding of the problem and the solutions solidifies. |  |
| Specs are a living document of the project \- Please keep an eye on them since they can be updated as the project progresses. |  |
| Stakeholder needs (the sections about background/motivation and existing implementations) are very important, must be filled with care, with a descriptive outline of the solution, and should be aimed at a possibly non-Snap-Store-Team audience and avoid acronyms.  |  |
| using “suggest” mode is encouraged so changes are visible and can have explicit approval. However, don’t use “suggest” mode for massive edits. |   |
| Use a hangout if some issue is taking too long to discuss on the spec.  Summarize the result of the hangout on the spec itself. |   |
| The spec is ready to start working on, **only** when Tech leads (Natalia, William) have approved it. If ready for review, \+mention them in a spec comment and wait for final approval. If no action in a couple of days, poke on IRC. |  |
| Copy/track the milestones in the project’s Trello card as a checklist. |  |
| Wait until the spec is approved before starting work, and do not send any merge proposals before the spec is approved. |  |
| Specs should be open to anyone in the company, we do things “in the open”. The rare snap-store-only topic should be discussed elsewhere (gdoc, forum, mailing list) and then summarized in the spec. |  |
| Before the spec is approved, all open comments on it should be resolved to the commenter’s satisfaction. Trivial fixes or requests for concrete information can be resolved by the spec author. More involved or contentious comments should wait for the commenter’s confirmation before resolving (or be resolved by the commenter) It is fine for the author to poke commenters about unclosed comments. Comment threads should be summarized in spec’s text to reflect the conclusion, so it doesn’t get lost. |  |

## **Apr 19, 2022 | [Juju Scriptlets - Weekly](https://www.google.com/calendar/event?eid=NDdraW51ZG90dTRpYTkwM2t2azl0dWZ2aGRfMjAyMjA0MTlUMTQwMDAwWiBqb2huLm1laW5lbEBjYW5vbmljYWwuY29t)**

Attendees: [Gustavo Niemeyer at Canonical](mailto:gustavo.niemeyer@canonical.com) [Jon Seager](mailto:jon.seager@canonical.com) [Caner Derici](mailto:caner.derici@canonical.com) [Robert Carlsen](mailto:robert.carlsen@canonical.com) [John Meinel](mailto:john.meinel@canonical.com) [Arnaud Delobelle](mailto:arnaud.delobelle@canonical.com)

Notes

* May not want this to be an external library, vs something that we tightly control.  
* It is a core piece that is security critical on our end, and we want to have clear maintenance (we don't want it to just be a toy for the maintainer).  
* Does the charm ship with the lua driver, or is that only provided by Juju  
  * Gustavo \- we want it to be strictly provided by the agent  
  * The lua runtime itself is going to be provided by juju, but   
* Gustavo  
* If we got rid of all the logic around Context, and it was just at the Runtime level. So either you have a Runtime, or you don't, but you don't have some amount of lua code that \*could\* run with a context that was closed.  
* 

Action items

- [ ] 

## 

## **Feb 22, 2022 | [Juju Scriptlets - Weekly](https://www.google.com/calendar/event?eid=NXJlZ2x1ZnFzcXFudTBxcHVucGluZGprOGRfMjAyMjAyMjJUMTUwMDAwWiBqb2huLm1laW5lbEBjYW5vbmljYWwuY29t)**

Attendees: [Gustavo Niemeyer at Canonical](mailto:gustavo.niemeyer@canonical.com) [Arnaud Delobelle](mailto:arnaud.delobelle@canonical.com) [Jon Seager](mailto:jon.seager@canonical.com) [Caner Derici](mailto:caner.derici@canonical.com) [John Meinel](mailto:john.meinel@canonical.com) [Robert Carlsen](mailto:robert.carlsen@canonical.com)

Notes

* Finalizers \- concerns about whether we can find the edge cases around synchronization for teardown tests

Action items

- [ ] 

## 

## **Feb 8, 2022 | [Juju Scriptlets - Weekly](https://www.google.com/calendar/event?eid=NXJlZ2x1ZnFzcXFudTBxcHVucGluZGprOGRfMjAyMjAyMDhUMTUwMDAwWiBqb2huLm1laW5lbEBjYW5vbmljYWwuY29t)**

Attendees: [Gustavo Niemeyer at Canonical](mailto:gustavo.niemeyer@canonical.com) [Arnaud Delobelle](mailto:arnaud.delobelle@canonical.com) [Jon Seager](mailto:jon.seager@canonical.com) [Caner Derici](mailto:caner.derici@canonical.com) [John Meinel](mailto:john.meinel@canonical.com) [Robert Carlsen](mailto:robert.carlsen@canonical.com)

Notes

* Aligned with Lua 5.4 definition  
* Big effort was towards having support for   
* Updating Unicode support (including handling of otherwise 'illegal' code points)  
* VM Test suite under Windows  
  * Test suite passes under Windows, and also is part of the standard CI run for the project  
* Discussion about Finalizers  
  * Looking to support weakref and weakref.Pool  
  * Want to have finalizers that can be run even in a restricted context  
  * We know that we want to support cleaning up resources even if you hit context limits, but since finalizers are just 'more code', you could potentially have the bitcoin miner in the finalizer, so it should still be subject to context limits.  
* What is the lifetime of the object, wrt a context, is there an object that would survive across them  
* Finalizer 'which are logic' should not be run  
* Cleanup of that Context that we should do careful  
* When we 'close the context', malicious code could leave behind files because it overrode the finalizers for those objects.  
* It is possible to protect the Meta table for the File object  
* Debug module would be able to poke at things, but we can't allow Debug in a Safe execution environment anyway  
* We can make File objects have immutable meta tables  
* Talking about "what is the environment that you can have when trying to write a scriptlet"  
* The new logic around Error in go and how to do trace of errors  
  * We want to adapt to the standard Go pattern of how to do errors  
* Areas where we want to apply Scriptlets  
  * Schema Validation (eg, scriptlet to go with config.yaml)  
  * Templates (being able to import upstream yaml declarations and tune them for a deployment)  
  * Pre-deploy script  
* The Pre-Deploy script feels like one that we want to start with  
  * What is the environment that we want to be running in  
  * Feels like the best approach to have a focused view  
* NPM \- being able to drop into a lua REPL  
* Lua does provide an API for debugging, can get callbacks for when functions are called, etc.  
* We want to have a clear definition of what is available for those scriptlets to access  
* Similar to how we do for charms  
  * You want to "stash that I want to make a change", vs immediately make a change  
  * We want to define what the invariants are that we are promising for the scriptlet  
  * You want to feel like you are asking the scriptlet for an opinion  
    * Given this scenario, how would you change things  
    * Store the intention from the scriptlet, and then have Juju evaluate it  
    * And then Juju can make decisions about what actual changes happen  
  * Also makes it easier to test it. Here is my environment for you, tell me what you think about it.  
  * 

Action items

- [ ] 

## **Feb 1, 2022 | [Juju Scriptlets - Weekly](https://www.google.com/calendar/event?eid=NXJlZ2x1ZnFzcXFudTBxcHVucGluZGprOGRfMjAyMjAyMDFUMTUwMDAwWiBqb2huLm1laW5lbEBjYW5vbmljYWwuY29t)**

Attendees: [Gustavo Niemeyer at Canonical](mailto:gustavo.niemeyer@canonical.com) [Arnaud Delobelle](mailto:arnaud.delobelle@canonical.com) [Jon Seager](mailto:jon.seager@canonical.com) [Caner Derici](mailto:caner.derici@canonical.com) [John Meinel](mailto:john.meinel@canonical.com)

Notes

* [GoLua Safe Environment](https://docs.google.com/document/d/1GriGMd_F3_jCmt_f0hXNfHfy4wLBXF_qJGf_oFnMBaU/edit#heading=h.nvci0dv8lwfo)  
* 

Action items

- [ ] 

## **Jan 11, 2022 | [Juju Scriptlets - Weekly](https://www.google.com/calendar/event?eid=NXJlZ2x1ZnFzcXFudTBxcHVucGluZGprOGRfMjAyMjAxMTFUMTUwMDAwWiBqb2huLm1laW5lbEBjYW5vbmljYWwuY29t)**

Attendees: [Gustavo Niemeyer at Canonical](mailto:gustavo.niemeyer@canonical.com) [Arnaud Delobelle](mailto:arnaud.delobelle@canonical.com) [Jon Seager](mailto:jon.seager@canonical.com) [Caner Derici](mailto:caner.derici@canonical.com) [John Meinel](mailto:john.meinel@canonical.com)

Notes

* Sit down with what does the structure look like for people implementing a script  
* Want to follow the dispatch pattern that we did for Hooks  
* What are the event names and how do we have a pattern that logically it could be kept alive, even if practically it is restarted frequently  
* Do you have many scriptlets and then register them for what events they handle  
* The docs we create, are they associated with a scriptlet, or is the doc looked up by name from the scriptlet  
* Do we want scriptlets interacting with charm hooks  
  * Is there a pre/post pattern around each hook event?  
* Certainly we know we want the ability to have logic before 'install' or the charm is even provisioned to a machine  
* Try to find a design that allows for extensibility  
* In the case of CRDs, we can probably do it after the pod is set up  
* But for pod itself, the script needs to be run before the pod comes up  
  * "I would like to use a ConfigMap and inject those as Environment Variables"  
* Definition of the pod is a logical document, but not an obvious file that you would be processing  
* What happens if you 'bork' a scriptlet  
  * Eg. you set the port to 'foo'  
  * The scriptlet exits with an error  
* We should be a little bit conservative about what you can actually mutate in a scriptlet. We can always open that up later  
* Can we have a static document?  
  * If we actually only have snippets of the document at a time, it may be cleaner to run a scriptlet over a coalesced document instead  
* K8s community is well versed in "short window support"  
* 

Action items

- [ ] 

## 

## **Nov 16, 2021 | [Juju Scriptlets - Weekly](https://www.google.com/calendar/event?eid=NXJlZ2x1ZnFzcXFudTBxcHVucGluZGprOGRfMjAyMTExMTZUMTUwMDAwWiBqb2huLm1laW5lbEBjYW5vbmljYWwuY29t)**

Attendees: [Gustavo Niemeyer at Canonical](mailto:gustavo.niemeyer@canonical.com) [Arnaud Delobelle](mailto:arnaud.delobelle@canonical.com) [Jon Seager](mailto:jon.seager@canonical.com) [Caner Derici](mailto:caner.derici@canonical.com) [John Meinel](mailto:john.meinel@canonical.com)

Notes

* What do we want the interaction to look like.  
* Do we want to expose a function that you can ask for a function to be executed in a more restricted context.  
* Do we want to have soft and hard limits on memory/cycles/wall clock time?  
* Analogy with golang Context  
  * Some things are user space visible  
  * But quotas are meant to also be always active as a global  
* Could we use continuations as the way to store the context  
  * We can use the continuation to have the information about the cpu/memory complexity of the function being created  
* Quotas are listed as 'ticks from the system'  
* We are doing a predictive model of 'how much will any given evaluation take'.  
* We would have a design around 'ticks' of the lua cpu, and a quota associated with it.  
* Individual ticks will be variable in their complexity, but there should be an upper bound on it  
* How do we surface/manipulate the quota infrastructure in Lua?  
* Lua module is 'string'  
* Name of the module  
  * Quota \- feels like it would collide with a module that someone would like to build/use  
  * Runtime \-   
  * Lua \-   
* Examples  
  * `ctx = runtime.context()`  
    `ctx.memory = 10000`  
    `ctx.call(...)`  
    `ctx.run('/path/to/scriptlet.lua')`  
  * Does the variable that gets set auto-decrement? Would you set a limit or the current balance?  
  * `runtime.quota.memory`  
    `runtime.quota.cpu`  
    `rt = runtime.create({...})`

Action items

- [ ] quota.rcall(), hard to know what the arguments are, would be better to be a named structure rather than positional parameters  
- [ ] Quota \- as a package name feels like it will conflict  
- [ ] 

## 

## **Nov 9, 2021 | [Juju Scriptlets Kick-off/Approach](https://www.google.com/calendar/event?eid=MWxvZWFhc2QzMHBhZzEybDI2Z3FyNzRuMTYgam9obi5tZWluZWxAY2Fub25pY2FsLmNvbQ)**

Attendees: [Gustavo Niemeyer at Canonical](mailto:gustavo.niemeyer@canonical.com) [Arnaud Delobelle](mailto:arnaud.delobelle@canonical.com) [Jon Seager](mailto:jon.seager@canonical.com) [Caner Derici](mailto:caner.derici@canonical.com) [John Meinel](mailto:john.meinel@canonical.com)

Notes

* What we want is "YAML on Steroids"  
* Trying to create something that is reasonably unique  
* People expect that YAML is readable and 'has no side effects'  
* It won't eat CPU/Memory to process  
* Fast to parse, convenient to use  
* We should be able to have something similar, while still being an interpreted language  
* Want it to be as convenient as import yaml and parse a file  
* The script shouldn't have access to the filesystem/network. It should be a strictly confined context  
* The scriptlet can look at the data that was provided, and can provide an opinion on the content  
* More like a config language than an application  
* We have things like go-lua as a place to start from  
* If you want to have precise control over bytes/cpu that the script is using  
  * Could give a bit of an explicit budget for the script  
* We would want it to be something that Arnaud will investigate the problem and come back with 'what are the nice ways that things can be done'  
* If we have a quota for objects/time we can at least set boundaries on the utilization  
* If you limit the objects, but a given object can be specified as \`3e6\*'x'\` it doesn't help you much for providing bounds on memory consumption.  
  * It might be possible to mix the compute tokens with the memory tokens in a nice fashion (you can only allocate 1 'word' per compute token), etc.  
* When we start exposing the context to the lua sandbox, we'll utilize your expertise  
* In terms of language semantics, what would the concrete use cases  
* CPU Usage and Memory Usage are the pieces that we want to control  
* Do we envision these scripts as processing other documents, or being the primary definition of the document?  
  * Terraform has a language (HCL)  
  * Was an attempt at a replacement-for-yaml because YAML was poorly structured  
  * HCL becomes a language that allows you to call functions  
  * Or YAML \+ templating engine (YAML \+ variables)  
  * YAML with an external analysis  
  * All of this is on the table  
  * But all of it ends up a little bit nasty, and you end up with solutions that are working around the fundamental issue  
  * You end up with neither a language nor a document, because they are crossing the divides.  
* What if we have them as clearly separate ideas. You have a clear document, and a clear script operating on that document.  
  * Deterministic output of running the script on the document  
* Macro functionality  
  * Having a pre phase   
  * Racket's Macro expansion  
  * Akin to CPP, though CPP itself did a poor job of it  
* Is having the macros inside the same document a clean way to proceed.  
  * Jinja and various templating engines end up being not very nice to maintain  
* When you use Customize to build a YAML file for you, people don't want to understand the whole thing, what they want is to edit a snippet of it, inject a label, snip out a section.  
* Is it better to have a small scriptlet process a large file rather than embedding a template engine inside that file  
* You want to restrict complexity, but provide functionality, and they pull against each other.  
* Only tool that seems to avoid this is Basil ([https://pypi.org/project/basil/](https://pypi.org/project/basil/))  
  * Resembles python but has a lot of caveats  
  * In practice Basil does restrict a few things in the name of simplifying things, but the system can still be abused.  
  * You give up actually being python for not actually solving the problem  
  * Basil has been around for a medium term, not long in the term of language lifetimes, but open source for a while, and not very active with external engagement  
* Lua-  
  * What is in the latest spec that would potentially introduce compatibility issues  
  * Something between 3 and 4  
  * Maybe Finalizers for objects  
* Fine balance between expressibility and complexity  
* We are trying to find what the language looks like / what the syntax could be that would be stronger than a static declaration  
* All of the fundamental providers work with a "Document" that you use to define what you want.  
* We know from the community that they want to start with a 4MB document from upstream, but they want to tweak aspects of it.  
  * Examples, forcibly named applications and forcibly named namespaces  
* We are creating an extension system  
  * One of the lessons that we've learned is that variables are not a great way to convey data  
  * Instead of introspecting variables that are left behind, you have functions that are provided by the environment to interact  
  * Providing context  
    * It is better to have a system that you can ask questions  
    * I see something more as a bit of functions that you would call to get the context that you want to expand upon  
    * They can ask for additional details as the process is going forward  
* Let's go slowly and solidly  
  * Identify a pattern without having to finish everything first  
* We can look at some of the patterns for places like the Operator Framework for how the model would be represented to the scriptlet  
* We could restrict the computation that could be expressed via language design  
  * The user will interact with particular data structures and constructs  
  * If we want to have something restricted, that isn't then 'just lua'  
  *   
* 

Action items

- [ ] 

# **Spec History** {#spec-history}

| Date | Status | Author(s) | Comment |
| :---- | :---- | :---- | :---- |
| \[YYYY-MM-DD\] | Initial spec | \[Someone\] |  |
| \[YYYY-MM-DD\] | Spec sent for review | \[Someone\] | \[e.g Submitted for review to onlineservices ML\] |
| \[YYYY-MM-DD\] | Development started | \[Someone\] | \[Me, myself and I are working on this.\] |
| \[YYYY-MM-DD\] | Parked | \[Someone\] | \[Because of reasons\] |
| \[YYYY-MM-DD\] | Resumed | \[Someone\] | \[Moar reasons\] |
| \[YYYY-MM-DD\] | Development completed | \[Someone\] | \[Deployed in production, etc.\] |

