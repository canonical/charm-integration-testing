

| Index | OP037 |
| :---- | :---- |
| **Title** | Alternative approach to stateless multi-status in ops |
| **[Status](https://docs.google.com/document/d/1lStJjBGW7lyojgBhxGLUNnliUocYWjAZ1VEbbVduX54/edit?usp=sharing)** | Approved |
| **Authors** | [Ben Hoyt](mailto:ben.hoyt@canonical.com) |
| **[Type](https://docs.google.com/document/d/1lStJjBGW7lyojgBhxGLUNnliUocYWjAZ1VEbbVduX54/edit?usp=sharing)** | Standard |
| **Created** | 2023-06-20 |

# **Abstract**

One approach to stateless multi-status handling was presented in [OP035](https://docs.google.com/document/d/1iJg4sfZW9vhCHXjKdBlv-va_sJx-jwN2WWTeQSKjb9M/edit) with `ops.StatusPrioritiser`. However, it was pointed out during spec review that the `get_status` callbacks are event handlers, and we already have an event system in the framework. In addition, `StatusPrioritiser` has to be manually `install()`ed, whereas the framework usually auto-installs things. This spec presents an alternative approach to stateless multi-status that uses `Framework.observe` with framework events.

**Update: this has been implemented in [https://github.com/canonical/operator/pull/954](https://github.com/canonical/operator/pull/954).**

# **Specification**

We propose adding a new `CollectStatusEvent` whose handlers add statuses for evaluation by a "status evaluator" at the end of every hook. These would be observable as `charm.on.collect_app_status` and `charm.on.collect_unit_status`, for setting application status and unit status, respectively.

```py
class CollectStatusEvent(EventBase):
    def add_status(self, status: model.StatusBase):
        """Add a status for evaluation."""

# Example handler
class MyCharm(ops.CharmBase):
    def __init__(self, *args):
        ...
        self.framework.observe(self.on.collect_app_status, self._on_collect_status)

    def _on_collect_status(self, event: ops.CollectStatusEvent):
        event.add_status(ops.BlockedStatus('config not set'))
```

After calling normal event handlers, the framework would run the status evaluator. If there were any `collect_app_status` or `collect_unit_status` observers, the framework would call each of them, and set the relevant status (app or unit) to the highest-priority status added.

The order of priorities is as follows, from highest to lowest priority (if there are multiple statuses with the same priority, the evaluator uses the first one added):

* error  
* blocked  
* waiting  
* maintenance  
* active  
* unknown

If an event handler set status manually using `app.status = x` or `unit.status = x`, those updates would go through immediately on set, but at the end of the hook the status evaluator would override that with its highest-priority status. Hooks could still set status manually to provide progressive status updates (for example, a long-running install hook).

The following shows a charm with two components, database and web app, that each observe `collect_unit_status` to provide the charm's overall status:

```py
class StatustestCharm(ops.CharmBase):
    def __init__(self, *args):
        super().__init__(*args)
        self.database = Database(self)
        self.webapp = Webapp(self)

class Database(ops.Object):
    """Database component."""

    def __init__(self, charm: ops.CharmBase):
        super().__init__(charm, "database")
        self.framework.observe(charm.on.config_changed, self._on_config_changed)

        # Note that you can have multiple collect_status observers even
        # within a single component, as shown here. Alternatively, we could
        # do both of these tests within a single handler.
        self.framework.observe(charm.on.collect_unit_status, self._on_collect_db_status)
        self.framework.observe(charm.on.collect_unit_status,
                               self._on_collect_config_status)

    def _on_collect_db_status(self, event: ops.CollectStatusEvent):
        if 'db' not in self.model.relations:
            event.add_status(ops.BlockedStatus('please integrate with database'))
            return
        event.add_status(ops.ActiveStatus())

    def _on_collect_config_status(self, event: ops.CollectStatusEvent):
        message = self._validate_config()
        if message is not None:
            event.add_status(ops.BlockedStatus(message))
            return
        event.add_status(ops.ActiveStatus())

    def _validate_config(self) -> typing.Optional[str]:
        """Validate charm config for the database component.

        Return an error message if the config is incorrect, None if it's valid.
        """
        if "database_mode" not in self.model.config:
            return '"database_mode" required'
        return None

    def _on_config_changed(self, event):
        if self._validate_config() is not None:
            return
        mode = self.model.config["database_mode"]
        logger.info("Using database mode %r", mode)

class Webapp(ops.Object):
    """Web app component."""

    def __init__(self, charm: ops.CharmBase):
        super().__init__(charm, "webapp")
        self.framework.observe(charm.on.config_changed, self._on_config_changed)
        self.framework.observe(charm.on.collect_unit_status, self._on_collect_status)

    def _on_collect_status(self, event: ops.CollectStatusEvent):
        message = self._validate_config()
        if message is not None:
            event.add_status(ops.BlockedStatus(message))
            return
        event.add_status(ops.ActiveStatus())

    # ... other methods similar to Database component ...
```

# **Optional tools**

**Note: this section of the spec wasn't agreed on – we'll start without exposing these and discuss later if necessary.**

Optionally, the framework could also expose the tools that build this up.

A `get_highest_priority` helper function (probably a class method on `StatusBase`) which, given a list of statuses, returns the highest-priority one. The implementation would be trivial:

```py
class StatusBase:
    _priorities = {
        "error": 5,
        "blocked": 4,
        "waiting": 3,
        "maintenance": 2,
        "active": 1,
    }

    @classmethod
    def get_highest_priority(cls, statuses: list[StatusBase]) -> StatusBase:
        return max(statuses, key=lambda s: cls._priorities.get(s.name, 0))
```

A new `finalise` event (a subclass of `LifecycleEvent`) that runs after calling normal event handlers, which the charmer could observe to do similar things after event code has run (whether successful or not). It could be used to manually do something similar to the provide-status events:

```py
class MyCharm(ops.CharmBase):
    def __init__(self, *args):
        super().__init__(*args)
        self.database = Database()
        self.webapp = Webapp()
        self.framework.observe(self.framework.finalise, self._on_finalise)

    def _on_finalise(self, event: ops.FinaliseEvent):
        statuses = [self.database.get_status(), self.webapp.get_status()]
        self.unit.status = ops.StatusBase.get_highest_priority(statuses)
```

# **Notes and discussion**

## **Alternative event names**

**Decision: we agreed on the name "collect\_status".**

Here's a list of alternative event names we discussed:

| `App.update_sub_status` | `Unit.update_sub_status` |  |
| :---- | :---- | :---- |
| `provide_app_status` | `provide_unit_status` | current proposal |
| **`app.on.collect_status`** | **`unit.on.collect_status`** | Note: unfortunately Unit and App can't inherit from Object so can't have .on (due to an impossible dependency where Framework needs a Model, so Model parts can't have a Framework instance). So we're going with **charm.on.collect\_app\_status** and **charm.on.collect\_unit\_status** |
| `app.on.finalise_status` | `unit.on.finalise_status` |  |
| `app.on.reconcile_status` | `unit.on.reconcile_status` |  |
| `app.on.report_status` | `unit.on.report_status` |  |
| `request_app_status` | `request_unit_status` |  |
| `app_status_vote` | `unit_status_vote` |  |
| `app_status` | `unit_status` | short and sweet, but maybe confusing |

We shouldn't use `update_status`, as that's already a Juju event triggered on an interval.

## **Issue: events don't return a value**

**Decision: the decision was that because these *are* events, let's treat them as normal events. Having to call `add_status()` and then `return` is just the same pattern people need to use for all events.**

Framework events don't return values, leading to the `event.add_status(x); return` pattern shown above. Provide-status handlers will tend to check a number of things and return early if they can provide a status, so I think this will be common. Even when writing the example code, I forgot one of the early returns, for example:

```py
    def _on_collect_status(self, event: ops.CollectStatusEvent):
        status = self._validate_config()
        if status is not None:
            event.add_status(status)
            return
        if 'db' not in model.relations:
            event.add_status(ops.BlockedStatus('please integrate with database'))
            # OOPS, forgot "return" here!
        event.add_status(ops.ActiveStatus())
```

We could allow these types of events to return values, and have the framework collate the return values. But that makes them different to existing framework events, none of which return a value.

We could mitigate this by having `event.add_status(x)` raise an exception if it was set a second time, which would catch the error above. But that prevents users from calling `add_status` multiple times if they *want* to add multiple statuses for evaluation.

Alternatively, we could provide a `status_handler` decorator that transformed a status-returning callback into one that calls `event.add_status()`. This would allow the handler to avoid taking the event parameter, and return a status rather than setting it and then returning. The above example would be simplified to this:

```py
    @ops.status_handler
    def _on_collect_status(self):
        status = self._validate_config()
        if status is not None:
            return status
        if 'db' not in model.relations:
            return ops.BlockedStatus('please integrate with database')
        return ops.ActiveStatus()
```

### **Issue: observers must be subclasses of `ops.Object`**

**Decision: similar to above – because these are regular events, they should have the same properties, including this requirement. In practice it's not a big deal, and you can observe multiple times on the same `ops.Object` subclass (as shown in the updated example).**

A handler (observer) must be a method on a class that inherits from `ops.Object`, which is required for serialising events. This is restrictive and annoying for provide-status events, which aren't and shouldn't be serialisable.

This is solvable if we make these `LifecycleEvent`s, and change events of that class to not be serialisable and allow any callable to be used as an event handler. The existing `LifecycleEvent`s, pre\_commit and commit, shouldn't be serialisable either. But again, this makes them different to existing Framework events, and will require special-casing `LifecycleEvent`s in the framework.

# **Spec History and Changelog**

| Date | Status | Author(s) | Comment |
| :---- | :---- | :---- | :---- |
| 2023-06-20 | Drafting | [Ben Hoyt](mailto:ben.hoyt@canonical.com) | Initial draft |
| 2023-06-21 | Drafting | [Ben Hoyt](mailto:ben.hoyt@canonical.com) | Updated spec based on 20 June review meeting and added decision notes. |

