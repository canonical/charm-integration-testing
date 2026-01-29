     

| Index | OP035 |
| :---- | :---- |
| **Title** | Stateless status prioritiser in Operator Framework |
| **[Status](https://docs.google.com/document/d/1lStJjBGW7lyojgBhxGLUNnliUocYWjAZ1VEbbVduX54/edit?usp=sharing)** | Rejected |
| **Authors** | [Ben Hoyt](mailto:ben.hoyt@canonical.com) |
| **[Type](https://docs.google.com/document/d/1lStJjBGW7lyojgBhxGLUNnliUocYWjAZ1VEbbVduX54/edit?usp=sharing)** | Standard |
| **Created** | 2023-06-13 |

# **Abstract**

**Update: this has been rejected in favour of [OP037](https://docs.google.com/document/d/1uQNgif0GG03TdnqT4UM9BxEshXuSvCmT9TdxOHoIUkI/edit).**

Charms often have multiple independent components that each have their own status, such as blocked or waiting. Only a single status can be reported to Juju, but if each component in the charm simplistically sets the application status, one component may set the overall status to active, when in fact the charm is still blocked by another component. Many charm teams have struggled with this and need a solution; some have solved it already using ad-hoc functions or their own charm libraries.

This spec proposes a stateless solution to this problem, an `ops.StatusPrioritiser` class, that tracks the status of multiple components and reports the highest-priority status to Juju.

It is currently out of scope to modify Juju to support multiple status components or rich status objects, but such changes to Juju may be considered in future.

For additional context, it's worth reading the introduction to the original OP034 spec, which proposed both this stateless version and a stateful approach. We split the stateless version into its own spec for easier discussion and approval.

# **Specification**

We propose adding a lightweight `ops.StatusPrioritiser` class to Operator Framework that tracks the highest-priority status among several named components. Apart from the `install` method, it's a "pure functional" class; the logic for actually updating the status is still done by the charm.

The order of priorities is as follows (priorities higher in this list take precedence):

* error  
* blocked  
* waiting  
* maintenance  
* active  
* unknown

The proposed API is as follows (the proof-of-concept [implementation](https://github.com/benhoyt/test-charms/pull/2/files) is very short too):

```py
class StatusPrioritiser:
    """Status prioritiser: track the highest-priority status among several components."""

    def add(self, component: str, get_status: typing.Callable[[], ops.StatusBase]):
        """Add a named status component.

        The highest/all methods will call get_status to get each component's status.

        Args:
            component: The name of the component (must be unique).
            get_status: A callable that returns the current status of this component.
        """

    def highest(self) -> ops.StatusBase:
        """Return highest-priority status with message prefixed with component name."""

    def all(self) -> list[tuple[str, ops.StatusBase]]:
        """Return list of (component_name, status) tuples for all components.

        The list is ordered highest-priority first. If there are two statuses
        with the same level, components added first come first.
        """

    def install(self, framework: ops.Framework,
                app_or_unit: typing.Union[ops.Application, ops.Unit]):
        """Install prioritiser: set status to highest() whenever framework exits.

        This will set the status after every hook execution, whether it's
        successful or not.

        Args:
            framework: The ops.Framework instance.
            app_or_unit: An Application instance to set application status (only
                valid on the leader unit), or a Unit instance to set unit status
                (valid for all units).
        """
```

To use the API in a charm, create a single `StatusPrioritiser`, add one or more components, and then call `prioritiser.highest()` to determine the overall status to set. The status can be set manually in the relevant event handlers, or automatically at the end of every hook (after the event handler has run) by calling `install`.

Below is a test charm that shows example usage of the prioritiser with two components. Each component only looks at charm config to determine status, but it could just as easily look at relation status, attributes of the workload, and so on. It's not required that the components be in their own classes, but they're shown like that here (each with a `get_status` method).

```py
class StatustestCharm(ops.CharmBase):
    """Status test charm."""

    def __init__(self, *args):
        super().__init__(*args)
        self.database = Database(self)
        self.webapp = Webapp(self)
        prioritiser = ops.StatusPrioritiser()
        prioritiser.add("database", self.database.get_status)
        prioritiser.add("webapp", self.webapp.get_status)
        prioritiser.install(self.framework, self.unit)

class Database(ops.Object):
    """Database component."""

    def __init__(self, charm):
        super().__init__(charm, "database")
        self.charm = charm
        charm.framework.observe(charm.on.config_changed, self._on_config_changed)

    def get_status(self) -> ops.StatusBase:
        """Return this component's status."""
        status = self._validate_config()
        return status if status is not None else ops.ActiveStatus()

    def _validate_config(self) -> typing.Optional[ops.StatusBase]:
        """Validate charm config for the database component.

        Return a status if the config is incorrect, None if it's valid.
        """
        if "database_mode" not in self.charm.model.config:
            return ops.BlockedStatus('"database_mode" required')
        return None

    def _on_config_changed(self, event):
        if self._validate_config() is not None:
            return
        mode = self.charm.model.config["database_mode"]
        logger.info("Using database mode %r", mode)

class Webapp(ops.Object):
    """Web app component."""

    def __init__(self, charm):
        super().__init__(charm, "webapp")
        self.charm = charm
        charm.framework.observe(charm.on.config_changed, self._on_config_changed)

    def get_status(self) -> ops.StatusBase:
        """Return this component's status."""
        status = self._validate_config()
        return status if status is not None else ops.ActiveStatus()

    def _validate_config(self) -> typing.Optional[ops.StatusBase]:
        """Validate charm config for the web app component.

        Return a status if the config is incorrect, None if it's valid.
        """
        if "webapp_port" not in self.charm.model.config:
            return ops.BlockedStatus('"webapp_port" required')
        return None

    def _on_config_changed(self, event):
        if self._validate_config() is not None:
            return
        port = self.charm.model.config["webapp_port"]
        logger.info("Using web app port %r", port)
```

# **Further Information**

## **Links and prior art**

* [OP033](https://docs.google.com/document/d/1Y5INjun-xXvSpRxkpDgvG1wHuuz9N67SlIMYf-5gZ20/edit#) \- the original multi-status spec which also proposed a stateful approach.  
  * See also the notes from teams at the bottom of OP033.  
* The mysql-router-k8s charm does something similar to this for [two components in a stateless way](https://github.com/canonical/mysql-router-k8s-operator/compare/d28fbe139e02a7924805a96c6f192cc0a42c2214...d319d72bee63621959f6226b1ef9526b6f863076) ([update](https://github.com/canonical/mysql-router-k8s-operator/commit/608c094273afdb7adb8ad3e9ef975ca4f8af4865)).  
* [Notes from rich status meetings](https://docs.google.com/document/d/1TPGlFA3GCsWln_-2NLgMnQ2Do0ShoxnDUO933tGBFu0/edit#heading=h.ge3uo3pwlp2q) at Prague sprints

# **Notes**

## **Notes from [Andrew Scribner](mailto:andrew.scribner@canonical.com)'s experience with StatusPrioritiser**

* I've been trying to write a base charm for the kubeflow team. it is largely an extension/refactor of sunbeam's base charm, but addresses a few blockers I hit when trying to use theirs.  What you might find interesting is that I pilfered your POC Prioritiser (thanks\!\!) and used it in the implementation.  So far, I'm loving it  
* I thought I'd dislike the stateless model when I first heard of it, but I'm now a convert.  It felt more natural than the stateful StatusPool that Sunbeam uses  
* If you want to take a look, there is:  
  * [spec](https://docs.google.com/document/d/1AOmp25BfyEWaPlSuS3U9UlF_Q5CKMTwHw8T84_KwDEM/edit)  
  * [poc implementation of the base charm](https://github.com/ca-scribner/functional-base-charm)  
  * [example charm rewrite using the base charm](https://github.com/canonical/kubeflow-profiles-operator/pull/122)  
* its all very much WIP

## **From 13 June 2023 spec review meeting**

`# TODO: consider removing component name from StatusPrioritiser altogether`

`# TODO: figure out a way to get "install" into constructor, as that's more`  
`#       similar to other ops patterns`  
`# self.framework.observe(framework.on.config_changed, self._on_config_changed)`  
`def __init__(self, framework, app_or_unit):`  
    `...`

`# Stateful syntax suggestion:`  
`# Ben's note: I don't think this is possible because self.unit.status is a property`  
`#             and hence a StatusBase value.`  
`self.unit.status['db'] = BlockedStatus('database is blocked')`

`# consider using our own event system instead of ad-hoc "event" callback in add()`  
`# - update_object_status`  
`self.framework.observe(framework.on.get_status, self._get_status_component1)`  
`self.framework.observe(framework.on.get_status, self._get_status_component2)`  
`self.framework.observe(framework.on.get_status, self._get_status_component3)`

# **Spec History and Changelog**

| Date | Status | Author(s) | Comment |
| :---- | :---- | :---- | :---- |
| 2023-06-13 | Drafting | [Ben Hoyt](mailto:ben.hoyt@canonical.com) | Separated this out from OP033 |
| 2023-07-19 | Rejected | [Ben Hoyt](mailto:ben.hoyt@canonical.com) | Marking this as rejected in favour of OP037 (collect-status) |

