Charm priorities in the bundle builder
======================================
The bundle builder allows setting charm priorities from a YAML file based on the ``charm-priorities-config`` argument, where the YAML file looks similar to this:

.. code:: yaml

    priorities:
      pgbouncer-k8s: 2.0
      other-charm: <float number>

The idea is that prioritizing charms allows the bundle builder to choose bundles that contain certain charms over others.

Example
-------

Suppose we have the following base bundle:

.. mermaid::

    graph TD
        subgraph "base bundle"
            A[mattermost-k8s]
            B[indico]
        end

Two possibilities may be followed by the bundle builder when fulfilling relations there: add a ``pgbouncer-k8s`` charm followed by a ``postgresql-k8s`` one, or two ``postgresql-k8s`` charms instead. This can be seen below.

.. mermaid::

   graph TD
    subgraph "Bad option"
        subgraph "Base bundle "
            mattermost_bad[mattermost-k8s]
            indico_bad[indico]
        end
        postgresql_k8s_bad[postgresql-k8s]
        postgresql_k8s_bad_2[postgresql-k8s]
    end

    subgraph "Desirable option"
        subgraph "Base bundle"
            mattermost_desirable[mattermost-k8s]
            indico_desirable[indico]
        end
        pgbouncer_k8s[pgbouncer-k8s]
        postgresql_k8s[postgresql-k8s]
    end

    mattermost_desirable --> pgbouncer_k8s
    indico_desirable --> pgbouncer_k8s
    pgbouncer_k8s --> postgresql_k8s

    mattermost_bad --> postgresql_k8s_bad
    indico_bad --> postgresql_k8s_bad_2

What the priority value means
-----------------------------

The default priority for a charm (i.e., when this argument is not specified or the file does not set a priority for the charm) will be 1.

The greater the priority, the greater prioritization a bundle with this charm receives. A priority of :math:`X` means the charm is worth :math:`X` times a normal charm.
