Issue categories
================

Use issues in this repository to describe the behavior, capabilities,
limitations, or tracking needs of Charm Integration Testing. A charm can be a
concrete example that exposes an issue, but the issue should focus on the
project rather than presenting the charm as the subject of a charm-specific bug
report. If a charm has a defect and the project is behaving as expected, report
it to the charm's maintainers.

For example, describe that the test environment cannot exercise charms requiring
a particular feature, then name a charm that demonstrates the limitation.
Avoid framing the issue as though that charm itself is broken.

Categories
----------

Choose a category based on what the project should or should not do, not simply
on which charm or external system appeared in a failed test.

``bug``
   A confirmed defect in Charm Integration Testing that makes a test run fail
   incorrectly, omit expected work, or report an incorrect result. A charm-side
   failure alone is not a project bug.

``enhancement``
   A request for the project to support a new capability. Documentation,
   cleanup, refactoring, and reporting-only improvements are not enhancements
   by themselves.

``wontfix``
   A run blocker or known limitation that is outside the project's supported
   scope and is not planned to be addressed here. This includes documenting
   features the test environment cannot support, even when a charm provides the
   concrete example. The issue may remain open as a reference or tracking
   target. Explain the unsupported capability or scope boundary; do not imply
   that the charm is defective. An upstream or external cause alone is not
   enough to choose this category.

``needs clarity``
   A potentially run-impacting issue whose cause, ownership, or expected
   behavior is not yet clear enough to choose a category. Do not guess whether
   it is a project bug, a requested capability, or an out-of-scope limitation.
   This label can accompany a category when that category is already clear but
   important details still need clarification.

Issues that do not affect run execution or correctness do not need one of these
categories. Use an appropriate existing label, such as ``documentation``,
``question``, or ``duplicate``.

Framing issues
--------------

Describe the project's behavior, limitation, or capability first. Include charm
names, versions, channels, and links as evidence or reproduction details when
useful. Do not treat a test failure as proof that the charm is defective, or
that the project has a bug. State what was observed and what remains uncertain.
