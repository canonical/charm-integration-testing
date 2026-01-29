

| Index | JU055 |
| :---: | :---- |
| **Title** | Scriptlet Interface |
| **[Status](https://docs.google.com/document/d/1lStJjBGW7lyojgBhxGLUNnliUocYWjAZ1VEbbVduX54/edit?usp=sharing)** | Rejected |
| **Authors** | [Ed Jones](mailto:ed.jones@canonical.com)[Marco Manino](mailto:marco.manino@canonical.com) |
| **[Type](https://docs.google.com/document/d/1lStJjBGW7lyojgBhxGLUNnliUocYWjAZ1VEbbVduX54/edit?usp=sharing)** | Informational |
| **Created** | 2022-09-27 |

**This document provides a more organised summary of the decisions made during the QEII Scriptlets Mini-Sprint in London (Sept 22). These decisions were not reviewed for implementation, but many of the concepts seen in this document will be taken, put into smaller specs and discussed in more depth there.**

# **Abstract**

Scriptlets will be useful to configure our products. This spec presents a framework to standardise the scriptlet experience with respect to products and scriptlet-authors.

# **Rationale**

The expression of config is a common task which requires a significant amount of effort on the part of the user. The original approach of a config file stating declaratively has two major weaknesses, it cannot ergonomically express:

* Conditional configuration  
* Repetition or near-repetition

Naturally, this has led to the use of preprocessors, or the embedding of small, unstandardised languages within the schema, which swamps previously-minimalist formats with new abstractions until they attain Turing-completeness and become a complete mess.

Scriptlets promise a new, more flexible approach to express both configuration and behaviour across a large proportion of the company’s portfolio in a simple, unified way. Instead of taking a config format and morphing it into a programming language, they take the opposite approach, starting with a constrained, deterministic, python dialect and using it to express configuration. Rather than being an after-thought, Turing-completeness is included from the start.

The flexibility of scriptlets must be guided, as otherwise there will be a divergence in approaches, scriptlets as a whole will become messy and hence their potential will be squandered.  Therefore, this spec presents:

1. A format which should be used by scriptlets  
2. A Go library to facilitate their embedding in company products

# **Specification**

A scriptlet is a small section of code written to solve a particular configuration task. In essence, a scriptlet is a special case of an extension—using a small set of exposed data, it makes decisions in an insulated environment. These decisions provide a hint about how the host application should proceed. As with traditional configuration, this hint may be used or ignored at the host’s discretion.

To form a consistent scriptlet experience, we must consider two points of view:

1. Scriptlet author—one who uses one of our products, wishes to configure its behaviour, greatly appreciates simplicity.  
2. Platform developer—one who works on one of our products, wishes to expose core functionality in a limited way, understands how and where data is stored in their system.

This spec presents conventions and a library to govern the experience of both parties. We present first the outward-facing, scriptlet-author perspective and then the inward-facing developer perspective.

## **Scriptlet organisation**

Here we discuss the event-handlers, data flow, code sharing and logging from within scriptlets.

### **Scriptlet patterns**

Scriptlets are written in Starlark, a language very similar to Python. To help ensure that new scriptlet authors have an easy time, Scriptlet APIs should be designed to fit well with Python patterns and idioms.

In its most basic form, we expect a scriptlet to look something like this:

```py
def init():
# observers can only be registered within the init function
juju.observe("config_change", on_config_change)

def on_config_change(event):
cfg = event.config
if 'avg_latency_warn' in cfg and 'avg_latency_crit' in cfg:
if cfg['avg_lateny_warn'] >= cfg['avg_latency_crit']:
event.reject(
'avg_latency_warn',
'must be less than avg_latency_crit'
)
def on_install(event):
stack = juju.app.stack
pg = stack.add("postgresql", trust=True, channel="15.0/stable")
pgb = stack.add(name="pgbouncer", config={"foo": juju.config["foo"]})

juju.integrate(pg, pgb)	
juju.integrate(pg.endpoints["db-proxy"], pgb.endpoints["db-proxy"])
juju.integrate(pg.endpoints["db.shared-db"], pgb.endpoints["foo"])
```

	  
The scriptlet will provide responses to a set of events it observes. It will use an *application object* to perform any access to the state of the underlying system and to provide its hints (configuration). In the above example, this is called `juju`.

The execution of a scriptlet will happen is partitioned into three phases:

* Script setup (parse, compile, run global scopes)  
* Calling the `init` functions  
* Handling events

The application object has the same set of fields and methods defined during all phases. We say that a method is *unavailable* if calling it causes a ‘method not available’ error when called (regardless of arguments).

During the first phase, the global state of the script should be cached. All methods on the application object (and its children) are unavailable.

During the `init` phase, the `init` function is called. All methods on the application object (and its children) are unavailable, except for the `observe` method. This method registers the handler for an event and has the form `foo.observe(“bar”, on_bar)`.

During the event handling phase, the `observe` method is unavailable.

By convention, events are handled by `on_*` functions like above. When these are called, they are:

* given data about the current event through their `event` parameter.  
* allowed access to useful data and functions through the application object.

*Note: we need to consider loading: what happens if we have:*  
*base:.star*

```py
def init():
juju.observe('foo', on_foo, default=True)
# is_default
# overrideable
```

*other.star:*

```py
load('base', default_on_foo='on_foo')
def init():
	observe('foo', ...)

def on_foo(event):
    ...
    default_on_foo(event)
```

### **Scriptlet storage**

During development, scriptlet code should be stored in a directory named `scriptlets/`, in the project root, or beside the relevant `src/`.

TODO:

* split into two sections  
* no symlinks, no flags, no directories  
* the only loadable files are regular files  
* not perfect (e.g. can use FUSE to imitate a regular file)  
* remove references to symlinks and hardlinks  
* add section on implementation vs expected development environment (how represented once loaded vs. what the user sees)

### **Load constraints**

The files from which a scriptlet may load values should be constrained to make the scriptlet feel small, lightweight and confined.

A load statement will attempt to load a starlark source file from a path relative to the current file, for example `load("dir/file.star", ...)`. will load `./dir/file.star`, note that the file extension must be present and must be ‘`.star`’. Paths containing invalid UTF-8 will be rejected.

A `load` statement will be successful if and only if the file of Starlark code requested satisfies:

*  at least one of:  
*   

1. it is in the same directory as the file which requested it  
2. it is in a subdirectory of the above  
3. it is accessed via a sequence of symlinks/hardlinks, all of which satisfy 1 and 2   

* and: its inode must have a refcount of at most 1\.

These constraints and the fact that scriptlets are stored in a separate directory imply that scriptlet and application code should not mingle.

### **Event conventions**

When `on_foo` is called, it is passed some `event` object which encapsulates event-specific data and responses.

Access to data should follow Starlark/Python idioms, for example, in a config-changed event, the new configuration should be accessed as if it is a first-class object, for example: `event.config[‘foo’][‘bar’] = ‘baz’`.

Method usage is encouraged where necessary, however names should be kept consistent. In particular, all events should have a `reject` method which takes either:

* A single string—`event.reject('too many cooks')`  
* Two strings—`event.reject('broth', 'spoiled by broth-to-cook ratio')`  
* A string and a list`—`event.reject('broth', \['too many cooks', 'mouldy potatoes'\])

```py
def on_config_changed(event):
	delta = event.delta
	if 'foo' in delta:
		foo = delta['foo']
		if foo.len() > 50:
			event.reject('foo too long: got %d chars' % foo.len())

def on_exit(event):
	foo = juju.config['foo']
	if foo.len() > 50:
		event.reject('ruh-roh: config was wrong all along lmao')

def helper(event):
	foo = juju.config['foo']
	if foo.len() > 50:
		event.reject('ruh-roh: config was wrong all along lmao')

def on_new_node(event):
	loc = event.location
	if loc.region == 'eu':
		juju.place(juju.apps['postgres'])
	if loc.region == 'us':
		1 / 0
		event.reject('cowboy country not supported')

def on_infallible(event):
	if invalid(event):
		print('this event is invalid')

def on_foo(event):
	if event == None:
		fail('unreachable')
```

*convention:*

* *the fail builtin is for invalid scriptlet code (e.g. equivalent to `panic(‘unreachable’)`*  
* *the event.reject method is for invalid semantics (e.g. the new config makes no sense)*  
* *all events are rejectable.*

### **Logging conventions**

A scriptlet will have a simplified logging interface—as they are intended to be lightweight, it would be unbefitting to use an expressive heavy-weight scheme appropriate within the wider host application.

Three builtins are provided in the global environment to handle logging. If the wider system for example uses log levels: *critical, error, warning, info, debug* and *trace,* the builtins will map as follows: 

* `print`—outputs *info*  
* `debug`—outputs *debug*  
* `fail`—outputs *error,* and stops execution


The behaviour of these functions should be integrated into the host application so that logs go to the right place and the error from `fail` flags the respective scope as being in an error state.

## **General organisation**

To standardise the development experience, we present a go-library called `scriptlets`. This library should:

* be generally applicable wherever scriptlets are deemed useful  
* facilitate common behaviours associated with scriptlets  
* hide Starlark details where reasonably possible

There are five areas to discuss here:

* Event objects—encapsulate event-specific data for event-handlers  
* Modules—abstract a Starlark source file  
* Module groups—source-related collections of Starlark modules  
* Script engine—entry-point, caches modules and unexecuted Starlark  
* Script backend—interface to host’s module storage and domain-specific data

### **Events**

The sole argument of the event-handlers is a value which represents the event.   
Event-handlers will be passed an `Event` object, which contains a *name* and a map of *attributes* for access by Starlark. `Event` will implement `starlark.Value`.

The name of the event matches the `foo.observe('bar', on_bar)` calls seen above. The value of this field must be unique.

The attributes of an event should follow the conventions outlined previously, however, it is up to the library-user to adhere to these.

### **Modules**

A module corresponds to a single file of Starlark code, and is a cache for use by `load` statements. It stores:

* Its name  
* a pointer to the one module group to which it belongs  
* a cache of the module-global environment it defines when executed

As the module is loaded lazily, the environment cache will initially be `nil`. This cache may be shared between many threads

### **Module Groups**

For their own convenience, the scriptlet author may wish to separate out their scriptlet code into separate starlark files. As such, when considering events and handlers, we should consider all of a user’s declarations simultaneously, leading us to the concept of *module groups.* These just abstract all of the starlark code in a user’s `scriptlets/` directory.

A module group stores:

* Its ID (which has `any` type)  
* The starlark modules in the group  
* A map of event-names to lists of handler objects  
* A map of local values unseen by Starlark but available to builtins

The module group should have methods:

* `SetLocal(key string, value any)`—declares the local values for builtins  
* `ObserveEvent(eventName string, callable starlark.Value) error`—declares that `callable` should be called when the event with the given name occurs. Appends to a list of event-handlers associated with this event. This method will error if it is called after the scriptlet’s initialisation phase. This method will also error if an observer `eventName` has already been registered.  
* `HandleEvent(event *Event, threadLocals map[string]any) error`—for a given event, calls the event handlers registered by `ObserveEvent` in the order they were given. Each handler is executed independently, in sequence, in its own `starlark.Thread`. Before each call, the  `threadLocals` are (re)copied into the thread. Calls to `HandleEvent` can be concurrent.

Module groups are loaded in lexicographical byte-order order from their source directory and its children. We do not need to concern ourselves with ordering constraints as Starlark will handle these for us.

### **Script engine**

The scriptlet engine governs the application wide use of scriptlets, it stores known module groups and a cache of parsed but un-executed starlark programs.

Module groups are stored in a map, indexed by some ID. New module groups are added into this map with the `AddModuleGroup` method, which takes an ID and returns a new, empty `*ModuleGroup` now stored in the map. Existing module groups are fetched by the `ModuleGroup` method.

The global cache of parsed starlark code is stored in a map indexed by a digest of the code. This avoids potentially costly calls to underlying storage. Care should be taken to flush this when the starlark bytecode version changes.

### **Script backend**

At their heart, scriptlets are a user-friendly way of concepts in application-space, hence this scriptlet library needs a way to access this information. This is done through the ScriptBackend interface , which has methods:

* `Load(*Module) (content []byte, err error)`—returns the content of a given module if it is absent from the script engine’s cache. This can be used by Starlark `load` statements.  
* `Globals(*Module) StringDict`—constructs an environment for use when executing the given module. The returned value should contain an application object as described previously.  
* `InitThreadLocals(*Module) map[string]any`—constructs a map of local values, made available to builtins but not Starlark code.

# **Further Information**

The version of Starlark we are using currently comes from [our own fork](https://github.com/canonical/starlark), which we hope to have merged with upstream eventually.

# **Spec History and Changelog**

| Date | Status | Author(s) | Comment |
| :---- | :---- | :---- | :---- |
| 2022-09-27 | Initial spec | [Ed Jones](mailto:ed.jones@canonical.com) | A summary of conversations from the 2022-09 Scriptlets Mini-sprint |
| 2022-10-05 | Reviewed with Gustavo |  | Focused on Starlark side |

# **Appendix**

### **Code sketch**

The scriptlet implementation code may look something like the following.

`type Module struct {`  
    `name	string`  
    `group   *ModuleGroup`  
    `globals StringDict // if loaded, globals != nil`  
`}`

`func (m *Module) Name() string        { return m.name }`  
`func (m *Module) Group() *ModuleGroup { return m.group }`

`type ModuleGroup struct {`  
    `id   	any`  
    `modules  map[string]*Module`  
    `locals   map[string]any`  
    `handlers map[string][]starlark.Value`  
`}`

`func (g *ModuleGroup) ID() any { return g.id }`

`func (g *ModuleGroup) AddModule(name string) *Module {`  
    `module = &Module{name: name, group: g}`  
    `g.modules[name] = module`  
    `return module`  
`}`

`type Event struct {`  
    `Name  string`  
    `Attrs StringDict`  
`}`

`var _ starlark.Value = &Event{}`

`func (e *Event) Truth() starlark.Bool 	{ return starlark.True }`  
`func (e *Event) String() string       	{ return "..." }`  
`func (e *Event) Type() string         	{ return "Event" }`  
`func (e *Event) Freeze()              	{ /* ??? */ }`  
`func (e *Event) AttrNames() []string  	{ /* from e.Attrs, sorted */ }`

`func (e *Event) Attr(name string) (starlark.Value, error) {`  
    `return e.Attrs[name], nil`  
`}`  
`func (e *Event) Hash() (uint32, error)	{`  
    `return 0, fmt.Errorf("unhashable: %s", e.Type())`  
`}`

`func (g *ModuleGroup) ObserveEvent(eventName string, callable starlark.Value) error {`  
    `// error if callable is not starlark.Callable`  
    `g.handlers[eventName] = append(m.handlers[eventName], callable)`  
`}`

`func (g *ModuleGroup) HandleEvent(event *Event, threadLocals map[string]any) error {`  
    `for _, handler := range g.handlers[event.Name()] {`  
   	 `thread := &starlark.Thread{}`  
   	 `for key, value := range threadLocals {`  
   		 `thread.SetLocal(key, value)`  
   	 `}`  
   	 `args := starlark.Tuple{event.Value()}`  
   	 `starlark.Call(thread, handler, args, nil)`  
    `}`  
`}`

`type ScriptBackend interface {`  
    `Load(module *Module) (content []byte, err error)`  
    `Globals(module *Module) StringDict`  
    `InitThreadLocals(module *Module) map[string]any`  
`}`

`type ScriptEngine struct {`  
    `groups   map[any]*ModuleGroup`  
    `programs map[string]*starlark.Program // digest => program`  
`}`

`func (se *ScriptEngine) AddModuleGroup(id any) *ModuleGroup {`  
    `group := &ModuleGroup{}`  
    `se.groups[id] = group`  
    `return group`  
`}`

`func (se *ScriptEngine) ModuleGroup(id any) *ModuleGroup {`  
    `return se.groups[id]`  
`}`