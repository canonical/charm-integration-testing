# Specification

| Index | OP083 |  |  |
| :---- | :---- | :---- | :---- |
| Title | Relation Interface Design |  |  |
| **[Type](https://docs.google.com/document/d/1lStJjBGW7lyojgBhxGLUNnliUocYWjAZ1VEbbVduX54/edit?usp=sharing)** | **Author(s)** | **[Status](https://docs.google.com/document/d/1lStJjBGW7lyojgBhxGLUNnliUocYWjAZ1VEbbVduX54/edit?usp=sharing)** | **Created** |
| Standard | [Dima Tisnek](mailto:dima.tisnek@canonical.com) | Drafting | 19 Dec 2025 |
|  | **Reviewer(s)** | **Status** | **Date** |
|  | Person | Pending Review | Date |

# Abstract

This document specifies details of the controlled process for introducing breaking changes to Juju relation interfaces: databag schema expectations, requirement to the charm library and supporting practices: charm library testing and an end-to-end example of an interface upgrade path.

# Rationale

Historically different charming teams and different charmed products provided different interoperability guarantees, both in terms of calendar time period and technical implementation. As a company we need a consistent, repeatable approach to manage interface evolution. From a customer perspective, a set of charms available today should just work together, just like packages in an Ubuntu LTS release, while breaking changes must be deliberate, introduced at defined points in time and communicated clearly, so that the experience is similar to an upgrade to the next Ubuntu LTS release.

**Forward compatibility and interface evolution**  
Prepare your interface for eventually being extended, in other words, spend time now not to get yourself into a corner later.

**Backwards compatibility and interface upgrades**  
When upgrade of one charm causes the interface to be upgraded, the TBD TBD

**Complicating factors**  
Applications in a large deployment may form a densely connected relation network. Thus, a single broken interface contract may have a significant impact on a running system.

Today, some charms provide very long LTS guarantees, while others are developed quickly. Additionally, Juju users outside Canonical don’t have a direct line to our engineers to find out the nuance of individual products and their interfaces. This means that a complex deployment cannot be safely upgraded: a Juju user doesn’t know which component is safe to upgrade first.

# Specification

## Databag schema

Adopt the following practices for forwards compatibility.

### **No mandatory fields**

Top-level fields must be optional.

Likewise most sub-fields must be optional.

`url: str | None = None`

`role:  Role | None = None`  
  `subject: str | None = None`  
  `session: str | None = None`

The exceptions are cases with clear semantics where the object containing the sub-field is discarded. See [Semantic Grouping](#semantic-grouping).

In some cases, a default value may be used instead of marking the field optional:

`protocol: Literal[“http”, “https”] = “https”`  
`temperature: float = 0.0`  
`priority: int = 100`  
`sans_dns: frozenset[str] = frozenset()`

Analysis of existing libraries suggests that default values are rare, and that they typically correspond to either enumerations or numeric quantities.

\[TBD\] add samples to the testing section

[Full rationale](#no-mandatory-fields)

### **No field reuse**

Once a field has been removed from the interface, another field with the very same name can never be added.

The exception is reverting removal of a field, where the field is brought back with the exact same type and semantics.

One method to ensure that field is not reused is to leave a comment in the interface specification

`class SomeData(...):`  
    `name: str | None = None`  
    `# surname: str | None = None  # removed in v1.2.3`

The other is to add a test vector in the charm library unit test dataset:  
TODO: move code ex below to the testing section

`V123_DATABAG = {“name”: “aa”, “surname”: “bb”}`

`def test_removed_fields():`  
    `assert parse(V123_DATABAG) == {“name”: “aa”}`

[Full rationale](#no-field-reuse)

### **Watch out for…**

No order in arrays: Essentially, treat JSON arrays as Python sets.

Arrays of primitive types are problematic, as individual elements cannot be extended.

Thus, arrays are arrays of objects, thus a sort property can be included into the object. Thus please treat arrays as sets.

At the databag level, arrays (JSON) should only be used to represent sets:

- the order of elements should not be strictly relied on  
- the index of particular element should not be relied on

- object ids embedded in the elements of the array:  
  `[{id: 42, name: “foo”, ...}, {id: 1, name: “bar”, ...}]`  
- ordering property embedded in the elements of the array:  
  `[{priority: 1, url: “...”}, {priority: 3, url: “...”}]`

Watch out for: if some unique identifier naturally maps to a string, you might be tempted to use an object instead:

- `{“42”: {name: “foo”, ...}, “1”: {name: “bar”, ...}}`

\[TBD\] Cert chain example…  
`[“A signed B”, “B signed C”, “C signed D”]`  
could be extended as:  
`[“A1 signed B”, “A2 cross-signed B”, “B signed C”, ...]`

**Potential issue: how the semantics of the key can be changed in a future version of the interface?**

TBD: recommend against any-key, value value type.

Debate: do we want fixed keys to different value types `{a: A, b: B}` or do we actually want any key to fixed value type `{x: A, yy: A, bbb: A}`?

### **Semantic grouping** {#semantic-grouping}

\[TBD\] is this good practice or a compatibility thing?

The databag content should be structured to reflect the meaning of data.

For example,  instead of:  
`{host, port, base_url, path}` 

consider:  
`{`  
  `direct: {host, port},`   
  `upstream: {base_url, path}`  
`}` 

This allows future version of the schema to express e.g.:  
`{direct: null, upstream: {base_url, path}}`

or   
`{direct: {host: str|None, port:...}, upstream: null}`

`direct.host: str`  
`direct.port: str`  
`upstream.base_url: str`  
`upstream.path: str`  
`# namespaced`  
`# nulled-outed as a group`  
`# a proper type can be applied`  
`# block can be passed somewhere else`

Which in turn allows predictable behaviour in the current version of the charm library, where blocks can be replaced with nulls.

For example, if the expected data is `{host: “foo.org”, port: 42}` the charm library may decide that host doesn’t make sense without a port, and if the remote sends something like `{host: “foo.org”, port: null, ports: [1,2,3]}`, the library can represent this to the charm as `{direct: null, ...}`.

This in turn allows future schema evolution with the knowledge of how older charms would interpret the data.

[Rationale](#semantic-grouping-1)

### **Secret content schema**

When a secret id or a URI is passed over the relation, the format for the secret content becomes part of the interface. Same rules apply to the keys and encodings of the values in the secret content.

* **no mandatory fields**  
* **no field reuse**  
* no order in arrays (if content values are encoded as JSON)  
* semantic grouping (if the secret content is complex enough)  
* **allowed URL or URI components**

The secret schema should be “placed” together with the interface schema.

TBD: cases against: we advocate for revealing the secret content as close to the workload as possible… how to mesh that with having the charm library specify the type? A handle class (type/parsing code \+ secret id)? Or some helper function to decode the secret?  
Pietro’s idea: lazify it, SomeObjt.get\_secret() → CharmLibDataclass object or exception  
but what about `.is_ready()`?

[Rationale](#secret-content-schema)

### **URLs and URIs**

While these are opaque formats, it makes all the sense in the world to use them when they map well to the workload and the mental model for the developers. For example, [The Twelve-Factor App](https://12factor.net/backing-services) recommends expressing attached resources via URLs or locator strings.

To do: it’s about **consistency**, can’t have two charms using `endpoint: URL` where one charm passes k8s service name and port as a base URL, and another a full public web URL with auth and query, as those two are not interchangeable. So we’re asking for **precision**.

At the same time, declaring something as “a URL” is insufficient. The schema and the charm library must make it clear:

* what the purpose of the URL is  
* what kind of URL it is: base URL, endpoint, full URL, an opaque value, or a workload-specific URI  
* what components of the URL are allowed: scheme, userinfo, host, port, path, query, fragment  
* what values are allowed: for example scheme must be HTTPS, host must be a hostname

`endpoint: yarl.URL | None = None`

`@pydantic.field_validator(“endpoint”, mode=”before”)`  
`@classmethod`  
`def _validate(cls, v: typing.Any) -> yarl.URL:`  
    `if v is None: return None`  
    `url = yarl.URL(v)`  
    `if url.scheme not in {“http”, “https”}: raise ValueError(“...”)`  
    `...`  
    `if url.fragment: raise ...`   
    `return url`

The restrictions must be validated in the unit tests that accompany the charm library.

[Rationale](#urls-and-uris)

## Charm library

Conventions, because an interesting charm uses several charm libraries, we don’t know in advance which relations are important.

### **Handle bad remote data**

Initialising the charm library object, and superficial API access (`.is_ready()`, detailed status) must not raise exceptions due to relation databag contents. Most importantly parsing the remote databag content must not lead to a charm-level exception / unit going into the error state.

* charm object initialisation should not raise  
* charm object `.is_ready()` should not raise

\[Why?\] because that allows `__init__()` to succeed and the rest will be done in the handler.  
\[TBD\]: separate reasons for providers and simple requirers.

Exceptions can and should be used to report incorrect initialization (e.g. wrong relation name), hook command failures (e.g. Ops exceptions) and similar circumstances.

\[Q?\] should there be a summary of the rationale here?  
Why? so that the interface can be upgraded.  
It’s about **controlled breakage**.  
A juju user can integrate any two applications whose charms advertise the same interface, and we don’t want those applications to go into error state(s).  
\[TBD\]: pull this up.

[Rationale](#handle-bad-remote-data)

### **Provide `.is_ready`**

The charm library should provide an `.is_ready()` method or property that allows the charm code to quickly evaluate the state of the endpoint. The value should be `False` in these cases:

* the relevant databag is empty, when appropriate  
* the relevant databag could not be parsed  
* the content of the databag is evaluated and determined “not ready” by some semantic rule

Note that this method/property doesn’t provide additional information about what’s wrong with the relation. See Advances status below for that.

[Rationale](#provide-.is_ready)

### **Advanced status**

Given that `.is_ready()` doesn’t provide the detailed status, and normal operation doesn’t raise extensions, consider providing some other method that evaluates the status of this endpoint as a whole, or relation per id (for each remote application).

For example, the Data team has the Advanced Status concept and the Rich Status API. The charm library that wraps an interface should provide some mechanism to explain why the relation “is not ready” that the charm authors can plug into that or similar API.

\[TBD\] five whys, in a complex deployment, being able to pinpoint what to look at first is gold.  
\[TBD\] a method that returns a string? structured data? a method to call to get an exception?  
for the exception case, most charms would stringify the exception and stuff that into the blocked status.  
\[TBD\] dig into advanced status API: multiple errors vs. most important error.

Example \[TBD find a real example from charming\]:  
`Blocked(ingress not ready, because FDQN is missing, because remote app data foo.bar.fqdn is empty)`  
`vs.`  
`Blocked(ingress not ready, because FDQN is missing)`  
`semantically: still waiting for the remote`  
`vs.`  
`Blocked(ingress not ready, because FDQN is bad: ValueError(“”))`  
`…`  
`raw pydantic error`  
`ValidationError: 1 validation error for IngressUnitModel`  
`upstream_fqdn`  
  `Input should be a valid string [type=string_type, input_value=42, input_type=int]`  
    `For further information visit https://errors.pydantic.dev/2.12/v/string_type`

`vs FastAPI`

{  
    "detail": \[  
        {  
            "loc": \[  
                "path",  
                "item\_id"  
            \],  
            "msg": "value is not a valid integer",  
            "type": "type\_error.integer"  
        }  
    \]  
}  
\[TBD\] [James Garner](mailto:james.garner@canonical.com)’s idea: stamp error with the indicated version if available.  
two things that we want:

- if remote is crazy, do expose why parsing failed  
  - but don’t have library send the unit into the error state  
  - charm may choose to (in a handler), that’s fine  
- if remote is sane, parse the databag, fields may all be nulls  
  then, surface what logical validation fails  
  - should this be done by the library  
  - or by the charm?

\[TBD\] charm library should store paring errors into the charm lib object

[Rationale](#advanced-status)

## Interface upgrades

Each **product** needs to be define rules:

- what can and cannot be upgraded  
- what track to upgrade too (one just vs many steps)  
- which side of the relation needs to go first

TBD suggestion, not a rule;  
TBD each charm team decides on their own product cadence

Upgrade example (TBD public interfaces, not private team interfaces):  
\[TBD\] these are product versions, not bases.

Sometimes breaking the interface when switching the track is OK.  
Field removal ought to be done in connection to switching tracks.

Intermediate step example:  
\[TBD\] northern star

* Year 2024 product A publishes  
  * `foo_url: str|None`  
* Year 2026 product A publishes (with a shim)  
  (Or 2024-track latest release?)  
  * `foo_url: str|None,`  
  * `foo_url_set: set[str]|None`  
* Year 2028 product A publishes  
  * `foo_url_set: set[str]|None`

## Testing

TBD we recommend including a forward-compatibility test and test vectors with the library. Specifically:

* a test that library parses empty and malformed data in a defined way  
* a test the include previously removed fields  
* a test for fields with changed definitions

In practice, it’s probably reasonable to keep the test vectors from the …

Tests for secret content that interface may reference.

Should I have examples here:

- no mandatory fields  
- no field reuse  
- no order in arrays  
- semantic grouping: if a subgroup is nulled when it cannot be parsed  
- secrets: actual example  
- URLs/URIs: examples of good and bad values; good coverage over URL components  
- —  
- no exceptions  
- is\_ready  
- advanced status  
- —  
- upgrades

# Further Information

### **Previous work and references**

[Analysis of versions of popular interfaces](https://docs.google.com/document/d/1YwriXR3eO_7PbdDJmLhCc8mERNKah46pQSj4jDe_Zqo/edit?tab=t.0)  
TBD summary  
[OB068 - Charm interface versioning](https://docs.google.com/document/d/1cUj0_-6CR_L_9R2zrm5gBW0Wqpnuhobr5phTcSAaPos/edit?tab=t.0) (rejected earlier effort)  
TBD short story  
[DA147 - UX of Statuses](https://docs.google.com/document/d/1SV11ct-flQkc5BOYOeXgmPeglL8bVs-mDVkGjG20K48/edit?tab=t.0) and [DA161- Implementation of Advanced Statuses](https://docs.google.com/document/d/1Yg7w7N-S1STbluk3SttZCQx1waZW_e_yOuKWeyahy20/edit?tab=t.0) (detailed status)  
TBD

TBD: add a note that this spec may get iterated upon after publication.

# Spec History and Changelog

Please be thorough when recording changes and progress with the spec itself and the work resulting from it. Record every meeting, attendees and conclusions from the meeting.

| Author(s) | Status | Date | Comment |
| :---- | :---- | :---- | :---- |
| [Dima Tisnek](mailto:dima.tisnek@canonical.com) | Braindump | 19 Dec 2025 | Brain dump |
| [Dima Tisnek](mailto:dima.tisnek@canonical.com) | Drafting | 6 Jan 2026 | Update this page from descriptive (informational: this is how things are / this is what’s hard) to prescriptive (standard: charmers should do this);   |
| [Dima Tisnek](mailto:dima.tisnek@canonical.com) | Drafting | 21 Jan 2026 | Split the document into two tabs: Specification and Extended Rationale. Major cleanup. |
| Person | Approved | Date |  |
|  |  |  |  |

# Extended Rationale

# Extended rationale

\[TBD\]: why do we prefer this over a simple databag version?

- not 0/1  
- data-driven  
- example: data interfaces

Be upfront what problems I’m not solving

- ??? 

Then what problems am I solving?

- same cadence  
- break stuff in the same way  
- how to detect the degraded mode

## Databag Schema

### **No mandatory fields** {#no-mandatory-fields}

tbd  
note: hairy interfaces  
note: partial use

### **No field reuse** {#no-field-reuse}

Although a field has been removed from the current version of the interface definition and charm library that wraps the interface, there’s no practical way to determine if there is still an application deployed that’s running charm code that was built with the older version of this charm library, or vendored a charm library or implemented the older definition of the interface without a library.

Therefore reusing a databag field brings uncertainty and a potential for incompatibility in production.

We’d rather not require charm libraries to perform extensive testing against obsolete versions of the interface and libraries. Instead we require that removed fields are never reused.

Other systems that follow this rule: [Protobuf](https://protobuf.dev/best-practices/dos-donts/#reuse-number) [Thrift](https://diwakergupta.github.io/thrift-missing-guide/#_versioning_compatibility)

### **Watch out for…**

1. No order in arrays  
2. Any-key fixed value type objects

tbd  
show what breaks if we didn’t do this

Counter-example: what if we have `dns: [str]` with the implication that it’s a list of DNS servers to try in order.

Counter-counter-example: what happens when we stuff both IPv4 and IPv6 addresses into the array? The recipient may only have a v4 or v6 address, and would filter the list to a v4-list or v6-list respectively. What does it mean then that it would use the DNS servers (index) `0,2,3,7,8` in one case and `1,4,5,6,9` in the other case?  
Regardless, wouldn’t it be better to round-robin those servers anyway (stateful) or use a random server (stateless)?

Counter-example: what if we have `servers: [str]` which are the database server endpoints, with the semantics that the first is the primary and the rest are replicas?

Counter-counter-example: what if the recipient has to filter those, e.g. by server name length or subnet or which domain names can be resolved. It could end up with a list like `[server2, server3]` and the pole position (primary) bit is lost? We’re forcing the recipient to convert the `[str]` list into something like `{primary: true, address: str}, {primary: false, address: str}, ...}`. Let’s express that directly in schema instead.

\[TBD\] writer still needs to emit elements in a stable order

\[TBD\] a charmer may ask “why don’t you like lists?”.  
\[TBD\] stable order when publishing data (to avoid spurious wakeups).

\[TBD\] example  
\[TBD\] rationale that includes what breaks if you don’t do this.

\[TBD\] What to do about it:

### **Semantic grouping** {#semantic-grouping-1}

tbd  
good practice in general; slightly stronger typing

### **Secret content schema** {#secret-content-schema}

tbd  
TBD  
\[TBD\]

- secret(username+password)   
- username \+ secret(password)  
- secret id? uri? (probably URI)  
- def bad str( `secret://<secret-id>/#<secret-field> )`

### **URLs and URIs** {#urls-and-uris}

**Different kinds of URLs**

Below are a few examples of semantically different data represented by URLs. The interface definition and the charm library must make it clear what semantics are expected when receiving a given field, in other words what kind of URL this is:

Base URL `http://pet.shop/v1`  
request `GET http://pet.shop/v1/animals/123`

Endpoint URL: `https://ap.ssso.hdems.com/authorize`  
redirect to `Location: https://ap.ssso.hdems.com/authorize?client_id=123&...`

Full URL `https://ap.ssso.hdems.com/oauth/userinfo`  
request `GET https://ap.ssso.hdems.com/oauth/userinfo` with `Authorization: xx`

An opaque URL `issuer: https://ap.ssso.hdems.com`  
id token claims `iss: https://ap.ssso.hdems.com, sub: N42, name: Bozo`

**URIs that are not URLs**

MongoDB connection string is allowed to include multiple hosts:  
`mongodb://hsot1:27017,host2:27017,host3:27017/mydb?replicaSet=rs0`

## Charm library

### **Handle bad remote data** {#handle-bad-remote-data}

A complex charm may include a dozen libraries and quite a few relations. The charm library author doesn’t have the knowledge to determine whether their charm library is critical for any given charm, important, or optional. Additionally, `ops` requires charm `__init__` to complete to observe any events.

Thus charm library object initialisation cannot raise exceptions under normal circumstances.

Additionally, since `.is_ready` is to be provided, there’s already a mechanism to report errors to the calling charm.

\> Exceptions can and should be used to report incorrect initialization (e.g. wrong relation name), hook command failures (e.g. Ops exceptions) and similar circumstances.

- initialization because every hook is guaranteed to fail / smth like static analysis / integration tests would fail and it’s best to catch these early  
- hook command failures, because those ought to be transient

### **Provide .is\_ready** {#provide-.is_ready}

TBD  
Directly useful to reconciler charms.  
An internal method/property for delta charms (was ready, now not ready —\> FooDisconnected event).

providers: is\_ready method, because it manages resources  
simple requirers: is\_ready property (either a limit: 1 relation or requirer aggregates data from multiple relations).

Examples go here:

- method  
- property

### **Advanced status** {#advanced-status}

\[TBD should be very small/simple\]  
TBD rationale: multiple relations in a consumer charms; multiple connected apps in a producer charm.  
\[TBD\] add show-config action thing  
\[TBD up to the charm to decide how to determine and expose “degraded” status\]

## Interface upgrades

## Testing

# Stash

~~Charms, as engineering projects and artefacts have long and differing life cycles.~~

~~: a relation may be established between a rather old version of charm A and a new version of charm B, and should still work. Likewise, either application can be updated at any time, and should continue to work.~~

## ~~Anti-patterns~~

~~\[TBD\] other anti-patterns?~~  
~~maybe remove from the standard spec: could go into the informational spec.~~

### **~~Using local databag to store state~~**

~~\[TBD\] write up why this was made / convenient~~

~~Using the local application databag (in a non-peer relation) to store state.~~

~~data interfaces example:~~

* ~~remote sends `{“port”: 42}`~~  
* ~~local stores `{“data”: {“port”: 42}}` in the local app databag~~

~~next time the remote updates the relation content, the local side computes:~~  
~~`diff(rel[remote], rel[local][“data”])`~~

~~effectively checking `remote.port (42) == local.data.port (42)`~~  
~~if the values are different, then an event is triggered “port changed”.~~

~~\[TBD\] write this up properly.~~

### **~~Combining disparate data in one relation~~**

~~\[TBD\] write up why this was made / convenient~~

~~ingress example: per-app and per-unit functionality on the same interface / in the same relation~~

### **~~Using opaque data formats~~**

~~\[TBD\] write up why this was made / convenient~~

~~example: serialised JSON (string blob) of Grafana alert rules~~

~~TBD: think about potential Grafana 9 to 12 transition.~~

### **~~Generic interfaces~~**

~~Overly generic interfaces, like “HTTP endpoint” (just a URL).~~

- ~~doesn’t say what kind of protocol~~  
- ~~what should it be used for?~~  
- …

### **\[TBD\]**

scheme/host/port/path vs URL/URI

- `https://user:password@host.com:5443/host?q=blah#fragment`  
- `^^^^^   ^^^^ ^^^^^^^^ ^^^^^^^^ ^^^^ ^^^^ ^^^^^^ ^^^^^^^^`  
- scheme: if HTTPS, we need a CA list (unless it’s a globally reachable URL)  
- auth: security / secrets  
- publicly resolved host, k8s service name, or ip address  
- port: only applicable to some host kinds?  
- path: is this a base URL or a full URL? (workloads speaking v1 vs v2 API)  
- query: if it’s there, it’s more of an endpoint? without maybe a base url?  
- fragment: browsers don’t even send that across, only for end-user facing UI?

Relation is used to establish the connection between two apps.  
This means that…. application imposes requirements on the format

`postgres://postgres:123456@127.0.0.1:5432/dbname`  
We do love URLs, URLs are amazing.. BUT\! you gotta write up what’s allowed, add the code to parse it(?), and add tests.  
For authentication/authorization, describe how secret content is injected.

TBD: URL-like things, e.g. MongoDB connection string

### **The case against delta charm libraries** {#the-case-against-delta-charm-libraries}

TODO: write this up properly

Sometimes engineers feel that Juju pushes charm authors towards delta charms.  
There is a distinction to be made though:

* Juju wants **targeted** processing in charms (specific relation)  
* but **not delta** processing (can’t get old state from Juju)

Additionally, if the delta functionality is encapsulated in a charm library (e.g. `data_interfaces`), the run-time cost is significant

One side of the relation receives “requests” as e.g. `rel[remote_app][“topic”]`  and maintains the old state in the same relation as `rel[local_app][“data”][“topic”]`, which means that any logical data change wakes up every unit in the relation: first on this side, then on the other side of the relation. 

TODO: a note about leadership changes and local storage  
TODO: a note about the charm library doing this in abstract

This leads to significant time for the model to settle, when the model is large.

There are roughly two options:

1. don’t compute the “delta” in the charm library, rather expose the current values to the charm, and have the charm compute the “delta” vs. the charm workload.  
2. have the charmers to write only holistic/reconciler charms, and push the charmers to update the existing charms to the holistic/reconciler paradigm, at least wrt. the processing of this relation

- no mand…  
- no reuse…  
- no order…  
- –  
- xxx

Try having multiple tabs\!

Postgres 14/

- relation: postgres\_14

Postgres 16/

- relation: postgres\_16 (interface 16\)  
- relation: postgres\_14 (interface 14\)

3 unit of 14 running.

juju refresh … 

2 units of 14  
1 unit of 16

juju integrate postgres:postgres\_16 some\_app

juju disintegrate postgres:postgres\_16 some\_app

If interfaces are effectively fixed, then \[this and that is not possible\]

If interfaces are always in flux, then \[tbd tbd\]

As the previous effort (version negotiation) was rejected, Charm Tech analysed evolution of four popular interfaces, and discovered that:

- most are app-to-app interfaces, and the heterogenous unit concern doesn’t arise  
- most changes were reasonably backwards compatible  
- a few changes were break-the-world kind

Thus, it is quite possible that with a little more care in early interface design, the need for complex interface versioning is obviated.

\[1\] No more delta charm libs please → internal link [https://docs.google.com/document/d/1Zub28RVLE8NFpdITtTdVS9sLwbeiUvmf2u40\_rjzX\_E/edit?tab=t.0\#heading=h.mgq35s1k0gff](#the-case-against-delta-charm-libraries)

\[2\] Charm library must be able to process bad databags, exposing those to the charm as “not ready” or “blank”, with possible extension for Data Platform-style extended charm status. this path should not log, as that would result in redundant logs on every Juju event. Charm library must include tests that validate this code path.

\[3\] Each field should (or must) have a default. This gives a clear, testable way to remove any field down the line. Forwards compatibility.

\[4\] new fields should have the default that’s logically equivalent to “new feature is not in use”. Standard backwards compatibility.

\[5\] changing type of fields: should be banned

- making fields more or less optional  
- expanding/contracting fields (int \<-\> float)  
- changing a field entirely (array \<-\> str), bad, can only be done if charm lib treats bad data as blank, and only in some case, so basically bad  
- basically, create a new field for a new type, and either remove the old one or push compatible representation into the old one.

TODO: special note on subfields  
TODO: special note on arrays (much harder than objects, where each key has or can have a default) – essentially arrays are sets, index cannot be used, ordering is probably not meaningful.  
TODO: special note on strings with custom format (CSV, etc)

- not sure what I meant  
- CA chains (pem-formatted text, where the order of blocks matters)

TODO: out of band communication

- relation is setting up the communication channel. It’s not the communication data channel. Corollary: a database.  
- db example  
  - users (db charm tracks those)  
  - databases (db charm tracks those)  
  - db schema  
  - data  
- cut-off: 1\. custom, workload-specific format; 2\. data is large; 3\. data changes often

TODO: code examples

## Other systems

### **Protobuf  v3**

All fields are optional in Protobuf 3, while they could be “required” or “optional” in Protobuf 2\. The [published rationale](https://protobuf.dev/best-practices/dos-donts/#add-required), [comment](https://github.com/protocolbuffers/protobuf/issues/2497#issuecomment-267422550), and [third-party analysis](https://capnproto.org/faq.html#how-do-i-make-a-field-required-like-in-protocol-buffers):

*“Required fields are considered harmful by so many \[that\] they were removed from proto3 completely. Make all fields optional or repeated. You never know how long a message type is going to last and whether someone will be forced to fill in your required field with an empty string or zero in four years when it’s no longer logically required but the proto still says it is.”*

### **Apache Avro**

Apache Avro recommends two schemata for their equivalent of a databag, a Writer’s schema and a Reader’s schema:  
[https://avro.apache.org/docs/1.11.1/specification/\#aliases](https://avro.apache.org/docs/1.11.1/specification/#aliases)  
more clearly contrasted here:  
[https://ambitious.systems/avro-writers-vs-readers-schema](https://ambitious.systems/avro-writers-vs-readers-schema)

In a simple case, Reader’s schema is the Writer’s schema. There should be at least something in common between the two. These schemata can evolve independently.  
\[TBD\]

maybe: HTTP/JSON API evolution

### **Array set rationale**

The requirement to treat JSON arrays as sets arises from the following observation:

Suppose that the content is heterogeneous. The receiver is able to process some, but not all elements.  
Sender: `[a, b, c]`  
Receiver: `[a, ???, c]`  
The receiver can either: reject the entire array or filter out the offending elements.  
In the latter case, the receiver passes `[a, c]` to some higher-level logic.  
Note that the array index for `c` is different.

If the order of the elements is important, \[tbd\]