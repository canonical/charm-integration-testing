The bundle building algorithm
=============================

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


Each edge in our graph is defined as the addition of a charm and its associated integration(s) to a base bundle. Each node therefore represents an unordered set of integrations (connections between application endpoints), along with the set of applications present in the bundle.

This means that the uniqueness of a node is determined by the set of integrations it contains, not just the set of applications. Two bundles with the same applications but different integrations are considered different nodes in the graph.

The depth of a node, or the number of edges from the root node, is equivalent to the number of
integrations added by the bundle builder.


It should be noted that because a node's uniqueness is defined as the *unordered* set of integrations, child nodes can converge, as shown in the example below, so this is not a tree. This makes the traversal more efficient as it removes repetitive checking of paths and allows the algorithm to explore bundles with different integration structures.

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

Graph traversal: uniform cost search
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

The node score is a computed value that determines which bundle configurations are preferred during graph traversal. The scoring algorithm combines three main factors:

1. **Fewer applications, accounting for charm priorities:**
    - For each application in the bundle, the score adds $1/\text{priority}$, so higher-priority charms reduce the score more.
    - This encourages bundles with fewer applications and favours those with higher-priority charms.

2. **Fewer unfulfilled endpoints:**
    - The score adds the number of unfulfilled endpoints remaining in the bundle.
    - Bundles with fewer unfulfilled endpoints are preferred, as they are closer to being fully resolved (greater endpoint fulfilment).

3. **More integrations, scaled by aggression:**
    - The score subtracts $\text{aggression} \times \text{number of integrations} / 4$.
    - As aggression increases (from 0 to 1), the algorithm is more willing to explore bundles with more integrations, effectively searching deeper (DFS) rather than wider (BFS).

The final score is the sum of these components.

Lower scores are preferred. This scoring system balances the desire for minimal bundles (few, high-priority applications), rapid endpoint fulfilment, and the need to explore deeper solutions as the search progresses.

Example:

.. mermaid::

     graph TD;
          A[Bundle A<br>Apps: 2<br>Unfulfilled: 3<br>Integrations: 1];
          B[Bundle B<br>Apps: 3<br>Unfulfilled: 1<br>Integrations: 2];
          C[Bundle C<br>Apps: 4<br>Unfulfilled: 0<br>Integrations: 3];
          A-->B;
          B-->C;

Depending on the aggression value, the algorithm may prefer B over A (fewer unfulfilled endpoints), or A over B (fewer apps), and ultimately C as the goal (all endpoints fulfilled).

Why not DFS?
~~~~~~~~~~~~

Traversing the graph with DFS would mean picking an unfulfilled charm endpoint, picking a charm that
fulfills it, adding that to the bundle, and then picking another unfulfilled charm endpoint, and
repeating that until there are no more unfulfilled charm endpoints.

This results in bundles that contain many applications, and more applications included in the
minimal bundle increases the chance of deployment failures.

However, this is a very fast way to find a bundle with all endpoints fulfilled, so the algorithm
increases aggression towards DFS as the number of nodes visited increases.

Why not BFS?
~~~~~~~~~~~~

Traversing the graph with BFS would be finding all the applications that fulfill at least one
unfulfilled charm endpoint, checking each one to see if there are any more unfulfilled interfaces,
and if so then repeating that process for *each* one of those new bundles.

This is good because it will find the bundle with the fewest number of applications with all charm
endpoints fulfilled, but in practice, it takes too long to resolve bundles with charms that
integrate with many other charms (hours and hours, and the tree is so large we run out of memory).
