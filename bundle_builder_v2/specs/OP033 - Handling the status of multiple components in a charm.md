     

| Index | OP033  |
| :---- | :---- |
| **Title** | Handling the status of multiple components in a charm |
| **[Status](https://docs.google.com/document/d/1lStJjBGW7lyojgBhxGLUNnliUocYWjAZ1VEbbVduX54/edit?usp=sharing)** | Rejected |
| **Authors** | [Ben Hoyt](mailto:ben.hoyt@canonical.com) |
| **[Type](https://docs.google.com/document/d/1lStJjBGW7lyojgBhxGLUNnliUocYWjAZ1VEbbVduX54/edit?usp=sharing)** | Standard |
| **Created** | 2023-05-15 |

# **Abstract**

**Update: this has been rejected in favour of [OP037](https://docs.google.com/document/d/1uQNgif0GG03TdnqT4UM9BxEshXuSvCmT9TdxOHoIUkI/edit).**

Charms often have multiple independent components that each have their own status, such as blocked or waiting. Only a single status can be reported to Juju, but if each component in the charm simplistically sets the application status, one component may set the overall status to active, when in fact the charm is still blocked by another component. Many charm teams have struggled with this and need a solution; some have solved it already using ad-hoc functions or their own charm libraries.

This spec proposes two generally useful solutions and shipping them in a charm library or in `ops` itself. These classes would help charmers keep track of the status of multiple components and report a single status to Juju.

It is currently out of scope to modify Juju to support multiple status components or rich status objects, but such changes to Juju may be considered in future.

# **Specification**

## **Stateless vs stateful**

There are two main approaches for solving this:

1) A stateless approach, where each component registers a function that calculates that component's state, and the resulting, highest-priority status is calculated by calling each registered function.  
2) A stateful approach using stored state, where the resulting, highest-priority status is calculated from the individual stored status values. This is the approach taken by the [`StatusPool`](https://opendev.org/openstack/charm-ops-sunbeam/src/branch/main/ops_sunbeam/compound_status.py) class used by some OpenStack charms (and originally written by Pietro).

There are pros and cons of each approach: the stateless approach is simpler to reason about and avoids problems with the stored state getting out of sync, but the stateful approach can be simpler for charmers to write.

Some of the recent OpenStack charms use `StatusPool` (a stateful approach). Other charms, such as mysql-router-k8s, uses a [minimalist stateless approach](https://github.com/canonical/mysql-router-k8s-operator/compare/d28fbe139e02a7924805a96c6f192cc0a42c2214...d319d72bee63621959f6226b1ef9526b6f863076). At this point it's not clear which approach is better, so our plan is to create a small charm library, `multistatus`, that includes both approaches.

**We recommend using the stateless approach (`StatusPrioritiser`) for new charms, as it's simpler to reason about and avoids stored state, but existing charms which already use `StatusPool` or fit the stateful approach better can use that approach (`Group` and `Component`).**

Once we have more experience with each approach in real charms, we'd consider including that version in the `ops` framework itself.

## **Stateless API**

The stateless API proposal has been moved to its own spec, [OP035](https://docs.google.com/document/d/1iJg4sfZW9vhCHXjKdBlv-va_sJx-jwN2WWTeQSKjb9M/edit) (and the alternative proposal in [OP037](https://docs.google.com/document/d/1uQNgif0GG03TdnqT4UM9BxEshXuSvCmT9TdxOHoIUkI/edit)), for easier discussion and separate approval, with the goal of including it in the Operator Framework as `ops.StatusPrioritiser`.

## **Stateful API**

The stateful API adds `multistatus.Group` and `multistatus.Component` classes (it uses `multistatus.Prioritiser` in its [implementation](https://github.com/benhoyt/test-charms/pull/3/files) to find the highest-priority status). It uses `ops` stored state to save the current state of each component.

To use it, you create a group and then add components to the group. The API in the `multistatus` module is as follows:

```py
class Group:
    """A group of components, each of which has a status.

    Each component's status is saved to stored state when its status is set,
    and is loaded when the charm is initialized.

    Args:
        app_or_unit: An Application instance to set application status (only
            valid on the leader unit), or a Unit instance to set unit status
            (valid for all units).
    """

    def __init__(self, app_or_unit: Union[ops.Application, ops.Unit]):
        ...

class Component:
    """A single component in a status group.

    A component's status starts out as UnknownStatus().
    """

    def __init__(self, group: Group, name: str):
        ...

    @property
    def status(self) -> ops.StatusBase:
        """The component's status."""

    @status.setter
    def status(self, value: ops.StatusBase):
        """Set the component's status (and save the status group)."""
```

To use the API in a charm, create a single `Group`, add one or more `Component`s, and then set the status of the various components as required. Note that `Component.status` is a getter/setter property to match the style of the framework's existing `unit.status` and `app.status`.

Below is a test charm that shows example usage with two components.

```py
class StatustestCharm(ops.CharmBase):
    """Status test charm."""

    def __init__(self, *args):
        super().__init__(*args)
        status_group = multistatus.Group(self)
        self.database = Database(self, status_group)
        self.webapp = Webapp(self, status_group)

class Database(ops.Object):
    """Database component."""

    def __init__(self, charm, status_group):
        super().__init__(charm, "database")
        self.charm = charm
        self.component = multistatus.Component(status_group, "database")
        charm.framework.observe(charm.on.config_changed, self._on_config_changed)

        self._update_config()

    def _on_config_changed(self, event):
        self._update_config()

    def _update_config(self):
        if "database_mode" not in self.charm.model.config:
            self.component.status = ops.BlockedStatus('"database_mode" required')
            return
        mode = self.charm.model.config["database_mode"]
        logger.info("Using database mode %r", mode)
        self.component.status = ops.ActiveStatus(f"db mode {mode!r}")

class Webapp(ops.Object):
    """Web app component."""

    def __init__(self, charm, status_group):
        super().__init__(charm, "webapp")
        self.charm = charm
        self.component = multistatus.Component(status_group, "webapp")
        charm.framework.observe(charm.on.config_changed, self._on_config_changed)
        self._update_config()

    def _on_config_changed(self, event):
        self._update_config()

    def _update_config(self):
        if "webapp_port" not in self.charm.model.config:
            self.component.status = ops.BlockedStatus('"webapp_port" required')
            return
        port = self.charm.model.config["webapp_port"]
        logger.info("Using web app port %r", port)
        self.component.status = ops.ActiveStatus(f"web app port {port!r}")
```

# **Open questions**

* Should we be setting application status or unit status? Existing solutions use unit status, but in general charms should be setting overall application status where it's meaningful, in addition to unit status. The `Prioritiser` and `Group/Component` classes allow you to choose which status you're setting – so maybe it's okay to leave this in charmers' control.

# **Further Information**

## **Links and prior art**

* Pietro Pasotti's original [StatusPool class](https://github.com/PietroPasotti/compound-status)  
* Sunbeam's [simplified version of StatusPool](https://opendev.org/openstack/charm-ops-sunbeam/src/branch/main/ops_sunbeam/compound_status.py), used in their [base charm](https://opendev.org/openstack/charm-ops-sunbeam/src/commit/6a7f80a2eee0d8626e9c09080b151f56a2f28b0e/ops_sunbeam/charm.py#L75) and [RelationHandler](https://opendev.org/openstack/charm-ops-sunbeam/src/commit/f9fff19596a784d60be40d7076f4c6f40f677fb1/ops_sunbeam/relation_handlers.py#L48)  
* The mysql-router-k8s charm does this for [two components in a stateless way](https://github.com/canonical/mysql-router-k8s-operator/compare/d28fbe139e02a7924805a96c6f192cc0a42c2214...d319d72bee63621959f6226b1ef9526b6f863076) ([update](https://github.com/canonical/mysql-router-k8s-operator/commit/608c094273afdb7adb8ad3e9ef975ca4f8af4865)).  
* [Notes from rich status meetings](https://docs.google.com/document/d/1TPGlFA3GCsWln_-2NLgMnQ2Do0ShoxnDUO933tGBFu0/edit#heading=h.ge3uo3pwlp2q) at Prague sprints

## **Notes from Data Platform team**

* From [this Mattermost thread](https://chat.charmhub.io/charmhub/pl/64mz7ehwf38y5beryzompwbjbw)  
* Marcelo notes that they do have the problem of one component stomping on another component's status. However, they work around it by looking at the current status: "As a workaround for that, we check in the next hooks if the charm is in a blocked state and either defer or completely ignore the hook code, by calling return in the beginning. It's possible that there are still some places in the charm code where it's not being checked and the blocked status may be overwritten by and Waiting or Active Status."  
* Raúl notes that they use a [centralized get\_status() method](https://github.com/canonical/data-integrator/blob/3ded7aa858c932c16ac2e63525fa3c9ba18b6eaf/src/charm.py#L91): "centralized status setting on the data-integrator here. It's kind of the same situation, but you can order the returns and status setting in a way that makes things more expected."  
* Mehdi shows how they work around similar issues by introspecting the current status message, "In OpenSearch we assume that a hook setting a BlockedStatus will always get deferred, so we override it with the statuses from other hooks to show the progress of an operation. Then when the said operation is done we clear the specific related status (i.e: [clear\_status(WaitingToStart)](https://github.com/canonical/opensearch-operator/blob/81baf02cc1f33945a5bbda1b29382aecc36ea14d/lib/charms/opensearch/v0/opensearch_base_charm.py#L187)). Then the hook that triggered a BlockedStatus gets retried, it sets back the status to Blocked."

## **Notes from Kubeflow team**

* Main goals are to support:  
  * Reporting that part of a charm succeeded/failed without asserting the entire charm is ok (eg: relation-changed event for \`Ingress\` is Blocked because the \`Ingress\` data is bad, but this providing charm is still working correctly)  
  * Avoiding clobbering the status from other parts of a charm (eg: eventA leads to blocked, but eventB fires next and works so charm goes Active even though eventA’s problem is unresolved)  
* This would be really helpful for consumable, self-contained charm libs like KubernetesServicePatch or the Prometheus/Grafana libs.  Those libs attach their own handlers to events and make it easy to use as an extension of your charm, but if they hit errors they’ll overwrite the user’s application’s status.  It's really hard to know if/when that happens as the user of these libs.    
* Concerns:  
  * a stateful approach for status might lead to charms relying on this stored state \- is that what we want?  The charm could have handlerA check status of **B** and then act differently based on that.  This is a pattern generally avoided in kubernetes controllers (typically they should always compute their own state) \- doesn’t mean we shouldn’t do it, but worth considering why they avoid this

## **Notes from looking at specific charms**

* [postgresql-operator](https://github.com/canonical/postgresql-operator/blob/main/src/charm.py): quite complex, status set in many many places. I asked the data platform team about this and they said they have had issues (see their notes above).  
* [grafana-operator](https://github.com/canonical/grafana-k8s-operator/blob/main/src/charm.py): significantly simpler, status set only in a few places. I think they could fairly easily modify this to use a Prioritiser with two components, something like this:

  `prioritiser.add('resource_patch', resource_patch.get_status)`

  `prioritiser.add('grafana', grafana.get_status)`

# **Spec History and Changelog**

| Date | Status | Author(s) | Comment |
| :---- | :---- | :---- | :---- |
| 2023-05-15 | Drafting | [Ben Hoyt](mailto:ben.hoyt@canonical.com) | Initial draft |
| 2023-05-29 | Drafting | [Ben Hoyt](mailto:ben.hoyt@canonical.com) | Significant updates, added both APIs and examples |
| 2023-06-13 | Drafting | [Ben Hoyt](mailto:ben.hoyt@canonical.com) | Moved stateless StatusPrioritiser to its own spec, OP035 |
| 2023-07-19 | Rejected | [Ben Hoyt](mailto:ben.hoyt@canonical.com) | Marking as rejected in favour of OP037 |

