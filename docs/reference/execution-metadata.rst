Execution Metadata
==================

Execution metadata are arbitrary attributes automatically collected during test execution and written to JUnit XML reports as test case properties. These properties provide rich context about test runs, including charm versions, warnings, and failure details.

Overview
--------

The ``execution_metadata`` fixture is available in all tests and provides an ``add(category, value)`` function. Metadata is collected throughout test execution and serialized as JSON arrays in JUnit properties, with one property per category containing all unique values.

Collected Metadata Categories
------------------------------

The following tables document all execution metadata categories that are automatically collected by the test framework.

Charm Information
~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 30 50 20

   * - Category
     - Description
     - Example Value
   * - ``charm``
     - Name of each charm deployed in the test model. Collected at start and end of test.
     - ``postgresql``
   * - ``charm:<name>:revision``
     - Revision number for a specific charm. Dynamic category based on charm name (e.g., ``charm:postgresql:revision``). Collected at start and end of test.
     - ``123``

Warning Information
~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 30 50 20

   * - Category
     - Description
     - Example Value
   * - ``warning:message``
     - Normalized warning messages emitted during test execution. Captures all Python warnings. Format: ``<WarningCategory>: <message>``
     - ``UserWarning: Deprecated function``

Failure Information
~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 30 50 20

   * - Category
     - Description
     - Example Value
   * - ``failure:message``
     - Normalized failure message when a test fails. Contains the error message from failed tests.
     - ``AssertionError: Expected 'active'``
   * - ``failure:charm:<name>:status``
     - Status information for a specific charm when test fails due to ``JujuWaitTimeoutError``. Format: ``application:<status>:<message>`` or ``unit:<status>:<message>``
     - ``application:blocked:Init failed``
   * - ``failure:cli:cmd``
     - The command that failed when test fails due to ``CalledProcessError``. Contains the normalized command string.
     - ``juju deploy postgresql``
   * - ``failure:cli:return_code``
     - The return code from a failed command. Only recorded for ``CalledProcessError`` exceptions.
     - ``1``
   * - ``failure:cli:stdout``
     - Standard output from a failed command. Only recorded for ``CalledProcessError`` with stdout.
     - ``Error: unit not found``
   * - ``failure:cli:stderr``
     - Standard error from a failed command. Only recorded for ``CalledProcessError`` with stderr.
     - ``ERROR connection refused``

Skip Information
~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 30 50 20

   * - Category
     - Description
     - Example Value
   * - ``skipped:message``
     - Normalized message explaining why a test was skipped. Captured when tests are skipped or marked as xfail.
     - ``Model not idle after 30s``

Using Execution Metadata
-------------------------

Automatic Collection
~~~~~~~~~~~~~~~~~~~~

Most metadata is collected automatically through pytest fixtures. You don't need to do anything special to get charm information, warnings, failures, or skip data.

Manual Collection
~~~~~~~~~~~~~~~~~

Tests can also add custom execution metadata using the ``execution_metadata`` fixture:

.. code-block:: python

    def test_custom_metadata(execution_metadata):
        # Record custom metadata
        execution_metadata("custom:category", "custom-value")
        execution_metadata("operation", "backup")
        execution_metadata("operation", "restore")
        
        # The fixture handles deduplication automatically
        execution_metadata("operation", "backup")  # Won't create duplicate

Accessing Metadata
~~~~~~~~~~~~~~~~~~

Execution metadata is written to JUnit XML files as properties:

.. code-block:: xml

    <testcase name="test_example">
        <properties>
            <property name="charm" value="[&quot;postgresql&quot;, &quot;vault&quot;]"/>
            <property name="charm:postgresql:revision" value="[&quot;123&quot;]"/>
            <property name="warning:message" value="[&quot;DeprecationWarning: ...&quot;]"/>
        </properties>
    </testcase>

The values are JSON-encoded arrays to support multiple values per category.

Implementation Details
----------------------

- Metadata values are stored in sets to automatically deduplicate entries
- All values are converted to strings before storage
- Values are sorted alphabetically before serialization
- String normalization is applied to messages to ensure consistent formatting
- JUnit properties are written after test completion (teardown phase)
