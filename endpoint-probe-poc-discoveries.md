## What are we testing

* Testing the interface (interface schema)
* Interfaces can be fulfilled by different libraries, one library can fulfil multiple interfaces, we are *not* testing libraries
    - https://github.com/canonical/postgresql-k8s-operator/blob/main/lib/charms/postgresql_k8s/v0/postgresql.py
          - Implements only `postgresql_client`
    - https://github.com/canonical/test_observer/blob/main/backend/charm/lib/charms/data_platform_libs/v0/data_interfaces.py
          - Implements `postgresql_client`, `mysql_client`, etc.
* We are effectively *building a library compliant with the spec that performs validation*

### Interesting findings:

* https://github.com/canonical/pytest-interface-tester
    - Meant to check that providing charms interface libraries are compliant with spec
* https://documentation.ubuntu.com/charmlibs/how-to/manage-libraries/
    - Charmhub hosted libraries are legacy
    - We should build around the new distribution system, which is pypi distribution
* https://github.com/canonical/charmlibs
    - Currently only charmlibs-interfaces-tls-certificates is packaged
    - https://pypi.org/project/charmlibs-interfaces-tls-certificates/
    - That package is a *library*, not the spec

## What we want charm authors to see

* Built in to CharmBase

## What that means for OPS

## Phase 1

* No modifications to ops
* We maintain validators (again, interface based)
* How we run validators
    - We copy validators into charm operator container
    - We install dependencies (from pypi or copy)
    - We give feed validator data from juju show-unit

## Phase 2

* We move validators to charmlibs
* Validators are published to mypi
* Charms can import validators
* Ops framework is updated to support validators (see previous section)

## Phase 1 POC (incomplete, but manually tested)

* `test_deploy` calls `JujuClient.validate_model()`
* For each application:
    - `JujuClient` calls `JubilantClient.validate_application()`
    - `JubilantClient` does nothing (Phase 2)
    - `JujuClient` calls `post_validate()` on **extensions**
    - We make a `ValidatorInjector` extension that manually copies validators to charms based off interface types and calls them