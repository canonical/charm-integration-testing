Execution Metadata
==================

Execution metadata are arbitrary attributes automatically collected during test execution and written to JUnit XML reports as test case properties. These properties provide rich context about test runs, including charm versions, warnings, and failure details.

The framework distinguishes between **failures** (expected test failures) and **errors** (unexpected issues), using different metadata prefixes for each. See :doc:`../explanation/error-vs-failure-classification` for details on this classification system.

Overview
--------

The ``execution_metadata`` fixture is available in all tests and provides an ``add(category, value)`` function. Metadata is collected throughout test execution and serialized as JSON arrays in JUnit properties, with one property per category containing all unique values.

Normalization
-------------

Many metadata values are automatically normalized to ensure consistency across test runs. Normalization removes variable data like timestamps, IP addresses, UUIDs, and numeric sequences, replacing them with placeholder tokens. This makes metadata suitable for aggregation and analysis across multiple test executions.

The ``normalize_string()`` function applies these transformations:

- **Kubernetes pod names**: ``pod=<podName>_<namespace>(<uid>)`` → ``pod=<POD>``
- **UUIDs**: ``a1b2c3d4-e5f6-...`` → ``<UUID>``
- **Temporary file paths**: ``/tmp5d7rg3qj`` → ``/tmp<TEMP>``
- **MinIO probe URLs**: ``probe-bsign-a1b2c3`` → ``probe-bsign-<NONCE>``
- **OCI image digests**: ``sha256:a1b2c3...`` → ``sha256:<DIGEST>``
- **IP addresses**: IPv4 (``192.168.1.1``) and IPv6 (``2001:db8::1``) → ``<IP>``
- **Timestamps**: ISO 8601, dates, times → ``<TIMESTAMP>``
- **All numeric sequences**: ``12345`` → ``XXX``
- **Truncation**: Values longer than 150 characters are truncated with ``...``

The ``normalize_string_multiline()`` function extends this to multi-line strings by applying ``normalize_string()`` to each line individually, and returns a list of normalized strings.

See ``charm_integration_testing/utils/normalization.py`` for implementation details.

Collected Metadata Categories
------------------------------

The following tables document all execution metadata categories that are automatically collected by the test framework.

Juju Information
~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 25 40 15 20

   * - Category
     - Description
     - Normalized
     - Example Value
   * - ``juju:version``
     - Version of the Juju controller. Collected automatically during test execution.
     - No
     - ``3.5.0``

Charm Information
~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 25 40 15 20

   * - Category
     - Description
     - Normalized
     - Example Value
   * - ``charm``
     - Name of each charm deployed in the test model. Collected at start and end of test.
     - No
     - ``postgresql``
   * - ``charm:<name>:revision``
     - Revision number for a specific charm. Dynamic category based on charm name (e.g., ``charm:postgresql:revision``). Collected at start and end of test.
     - No
     - ``123``

Pipeline Information
~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 25 40 15 20

   * - Category
     - Description
     - Normalized
     - Example Value
   * - ``pipeline:ref``
     - Git commit hash (short) of the repository at test execution time. Collected from the current git HEAD.
     - No
     - ``e484374``
   * - ``pipeline:tag``
     - Git tag pointing to the current commit, if one exists. Only collected when the commit has an exact tag match.
     - No
     - ``v1.2.3``
   * - ``pipeline:workflow_hash``
     - Git hash of the ``.github/workflows/charm-testing.yaml`` workflow file. Provides version tracking of the testing pipeline itself.
     - No
     - ``a1b2c3d4e5f6...``

Warning Information
~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 25 40 15 20

   * - Category
     - Description
     - Normalized
     - Example Value
   * - ``warning:message``
     - Warning messages emitted during test execution. Captures all Python warnings. Format: ``<WarningCategory>: <message>``
     - Yes
     - ``UserWarning: Deprecated function``

Failure and Error Information
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The framework distinguishes between expected failures and unexpected errors, using different metadata prefixes for each category. All metadata keys use either ``failure:*`` or ``error:*`` prefixes depending on the exception type classification.

See :doc:`../explanation/error-vs-failure-classification` for a complete explanation of how exceptions are classified and when each prefix is used.

Failure Metadata (Expected Failures)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Collected when the exception type is in ``KNOWN_FAILURE_EXCEPTIONS`` (``JujuWaitTimeoutError``, ``AssertionError``, ``CalledProcessError``):

.. list-table::
   :header-rows: 1
   :widths: 25 40 15 20

   * - Category
     - Description
     - Normalized
     - Example Value
   * - ``failure:message``
     - Failure message when a test fails with a known exception. Contains the error message from failed tests.
     - Yes
     - ``AssertionError: Expected 'active'``
   * - ``failure:charm:<name>:status``
     - Status information for a specific charm when test fails due to ``JujuWaitTimeoutError``. Format: ``application:<status>:<message>`` or ``unit:<status>:<message>``. The message portion is normalized.
     - Partial
     - ``application:blocked:Init failed``
   * - ``failure:cli:cmd``
     - The command that failed when test fails due to ``CalledProcessError``. Contains the command string.
     - Yes
     - ``juju deploy postgresql``
   * - ``failure:cli:return_code``
     - The return code from a failed command. Only recorded for ``CalledProcessError`` exceptions.
     - No
     - ``1``
   * - ``failure:cli:stdout``
     - Standard output from a failed command. Only recorded for ``CalledProcessError`` with stdout.
     - Yes, multi-line normalized
     - ``Error: unit not found``
   * - ``failure:cli:stderr``
     - Standard error from a failed command. Only recorded for ``CalledProcessError`` with stderr.
     - Yes, multi-line normalized
     - ``ERROR connection refused``

Error Metadata (Unexpected Errors)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Collected when the exception type is **not** in ``KNOWN_FAILURE_EXCEPTIONS``:

.. list-table::
   :header-rows: 1
   :widths: 25 40 15 20

   * - Category
     - Description
     - Normalized
     - Example Value
   * - ``error:exception:message``
     - A catch-all for unexpected errors. Contains the exception message for any error not classified as a known failure.
     - Yes, multi-line normalized
     - ``KeyError: 'applications'``
   
Skip Information
~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 25 40 15 20

   * - Category
     - Description
     - Normalized
     - Example Value
   * - ``skipped:message``
     - Message explaining why a test was skipped. Captured when tests are skipped or marked as xfail.
     - Yes
     - ``Model not idle after XXXs``

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
