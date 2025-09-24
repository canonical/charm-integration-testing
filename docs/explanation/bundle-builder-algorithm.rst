.. _build:

The Bundle Building Algorithm
===========================

Goal
----

The bundle resolution algorithm solves a graph traversal problem in which additional charms are
added to the bundle set to resolve charm integration endpoints marked as non-optional by the charm's
metadata.

Inputs and outputs
------------------

The bundle-building algorithm accepts a base bundle of charms and integrations specified by the
user, and the product is a bundle with additional applications that fulfill as many non-optional
endpoints as possible.

How the graph is defined
------------------------

Each edge in our graph is defined as the addition of a charm to a base bundle. Each node therefore
represents an unordered set of charms bundle.

The depth of a node, or the number of edges from the root node, is equivalent to the number of
charms added by the bundle builder.

It should be noted that because a node's uniqueness is defined as the *unordered* set of charms,
child nodes can converge, as shown in the example below, so this is not a tree. This makes the
traversal more efficient as it removes repetitive checking of paths.

.. mermaid:: 

    graph TD;
        A[grafana-k8s<br>grafana-agent-k8s];
        B[grafana-k8s<br>grafana-agent-k8s<br>postgresql-k8s];
        C[grafana-k8s<br>grafana-agent-k8s<br>traefik-k8s];
        E[grafana-k8s<br>grafana-agent-k8s<br>mysql-k8s];
        D[grafana-k8s<br>grafana-agent-k8s<br>postgresql-k8s<br>traefik-k8s];

        A-->B;
        A-->C;
        A-->E;
        B-->D;
        C-->D;

The target node
---------------

The target node, or node that we want to find in the undiscovered graph, is one that is the bundle
for which there are no more charm endpoints that can be fulfilled.

Some charms in Charmhub have required endpoints for which no charm can fulfill them. These are
referred to as unfulfillable interfaces.

There may be more than one of these nodes in the graph, but due to the size of some of the graphs,
we cannot fully explore and find all these nodes.

Graph Traversal: Uniform Cost Search
------------------------------------

Uniform Cost Search (UCS) is a pathfinding algorithm that expands the lowest-cost node first using a
priority queue. It guarantees the optimal path in graphs with non-negative edge costs by always
exploring the cheapest path available until it reaches the goal. It is basically Dijkstra's
algorithm but is defined to search for a single node goal on an undiscovered graph.

In the bundle builder's case, we have a priority queue of nodes to look at, and the node with the
lowest score (see below) is removed from the queue to look at.

If this node meets the criteria (no more fulfillable interfaces), we pick that node as the minimal
bundle. If not, we add all of its children to the queue, where each child node is a bundle with an
additional application.

The algorithm implementation is in
`bundle_builder/bundle_builder.py <https://github.com/canonical/charm-integration-testing/blob/main/bundle_builder/bundle_builder/bundle_builder.py>`_.

Node score
~~~~~~~~~~

The node score is the computed value for the cost of a node in the graph. Ideally, this would just
be the number of applications in the bundle result in a BFS algorithm, but we cannot only use BFS
for the reason below.

The other metric we consider is therefore the number of remaining non-optional interfaces that can
be fulfilled in the bundle. The idea here is that bundles with fewer remaining fulfillable
interfaces are closer to the target node and should require fewer charms to be added.

It is not optimal to just use the number of interfaces that can be fulfilled, however. We have found
in practice that this can lead down a path of charms adding as many unfulfilled interfaces as they
remove, when other paths would contain fewer applications, such as in the example below.

.. mermaid::

    graph TD;
        A[Bundle A<br>Fulfillable: 3];
        B[Bundle B<br>Fulfillable: 1];
        C[Bundle C<br>Fulfillable: 1];
        D[Bundle D<br>Fulfillable: 1];
        E[Bundle E<br>Fulfillable: 0];
        X[Bundle X<br>Fulfillable: 2];
        Y[Bundle Y<br>Fulfillable: 0];
        
        
        A-->B;
        B-->C;
        C-->D;
        D-->E;
        
        A-->X;
        X-. Not taken .->Y;

Therefore, the node score is a combination of these two metrics. Initially, the number of
applications in the bundle is the prioritized metric, and as the number of nodes visited increases,
the contribution of the number of fulfillable interfaces to the score increases. This leads to a
balance of finding the optimal bundle in a reasonable (and finite) amount of time.

Why not DFS?
~~~~~~~~~~~~

Traversing the graph with DFS would mean picking an unfulfilled charm endpoint, picking a charm that
fulfills it, adding that to the bundle, and then picking another unfulfilled charm endpoint, and
repeating that until there are no more unfulfilled charm endpoints.

This results in bundles that contain many applications, and more applications included in the
minimal bundle increases the chance of deployment failures.

Why not BFS?
~~~~~~~~~~~~

Traversing the graph with BFS would be finding all the applications that fulfill at least one
unfulfilled charm endpoint, checking each one to see if there are any more unfulfilled interfaces,
and if so then repeating that process for *each* one of those new bundles.

This is good because it will find the bundle with the fewest number of applications with all charm
endpoints fulfilled, but in practice, it takes too long to resolve bundles with charms that
integrate with many other charms (hours and hours, and the tree is so large we run out of memory).
