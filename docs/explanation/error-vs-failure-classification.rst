Error vs Failure Classification
==================================

This document explains how the test framework distinguishes between expected test failures and unexpected errors, and how this classification affects test reporting, metadata collection, and automatic retry behavior.

Overview
--------

The testing framework categorizes test failures into two distinct types:

**Failures (Expected)**
  Test failures that occur due to known, expected conditions. These represent legitimate test assertions that didn't pass or environmental conditions that are anticipated during testing (e.g., timeout waiting for model to stabilize). Failures are recorded with ``<failure>`` tags in JUnit XML and use ``failure:*`` metadata prefixes.

**Errors (Unexpected)**
  Exceptional conditions that represent bugs, infrastructure issues, or unexpected problems in the test framework itself. These are recorded with ``<error>`` tags in JUnit XML and use ``error:*`` metadata prefixes. 

Classification Mechanism
-------------------------

The classification happens in the ``pytest_runtest_makereport`` hook in ``conftest.py``. When an exception is raised during test execution, it's evaluated against the ``KNOWN_FAILURE_EXCEPTIONS`` list:

.. code-block:: python

    KNOWN_FAILURE_EXCEPTIONS = (
        JujuWaitTimeoutError,  # Model didn't reach stable state in time
        AssertionError,        # Test assertion failed
        CalledProcessError,    # CLI command returned non-zero exit code
    )

**Classification Logic:**

1. **Pytest Built-in Exceptions**: ``Skipped``, ``XFailed``, ``Exit`` are never reclassified. These maintain their standard pytest behavior.

2. **Known Failures**: Exceptions in ``KNOWN_FAILURE_EXCEPTIONS`` are:
   
   - Converted from ``"error"`` to ``"failed"`` outcome if pytest initially classified them as errors
   - Keep their original execution phase (``setup``, ``call``, ``teardown``)
   - Recorded as ``<failure>`` in JUnit XML

3. **Unexpected Errors**: All other exceptions are:
   
   - Kept as ``"failed"`` outcome (pytest's standard behavior for exceptions)
   - Forced to ``when="setup"`` phase when occurring during test execution, which causes JUnit XML to emit ``<error>`` tags
   - Marked internally with ``error_message`` in the test item stash

Impact on Test Behavior
------------------------

Test Skipping
~~~~~~~~~~~~~

The ``assert_idle`` fixture runs before each test to verify the Juju model is in a stable state:

.. code-block:: python

    @pytest.fixture(autouse=True)
    def assert_idle(juju_client, model, print_setup_and_teardown_info):
        try:
            juju_client.idle_for_period(model=model, timeout=timedelta(seconds=30), count=5)
        except JujuWaitTimeoutError as e:
            pytest.skip(str(e))

Because ``JujuWaitTimeoutError`` is a known failure, when it's raised and caught here, ``pytest.skip()`` is called successfully. This causes subsequent tests to skip gracefully rather than encountering setup errors.

**Graceful Skip Behavior:**

- ``test_deploy`` fails with timeout → marked as ``<failure>``
- ``test_integration`` runs ``assert_idle`` → catches ``JujuWaitTimeoutError`` → calls ``pytest.skip()`` → test is ``<skipped>``
- ``test_teardown`` runs ``assert_idle`` → catches ``JujuWaitTimeoutError`` → calls ``pytest.skip()`` → test is ``<skipped>``

**Error Propagation (if JujuWaitTimeoutError were unexpected):**

- ``test_deploy`` fails with timeout → marked as ``<error>`` in setup phase
- ``test_integration`` runs ``assert_idle`` → ``JujuWaitTimeoutError`` converted to setup error → test has ``<error>``
- ``test_teardown`` runs ``assert_idle`` → ``JujuWaitTimeoutError`` converted to setup error → test has ``<error>``

Metadata Collection
-------------------

Execution metadata uses different prefixes based on classification for failures and a single catch-all for unexpected errors.:

Failure Metadata (Known Failures)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Collected when ``error_message`` is **not** present in the test item stash:

.. list-table::
   :header-rows: 1
   :widths: 30 50 20

   * - Metadata Key
     - Description
     - Applies To
   * - ``failure:message``
     - The failure message from the test report
     - All known failures
   * - ``failure:charm:<name>:status``
     - Charm status details when timeout occurs
     - ``JujuWaitTimeoutError``
   * - ``failure:cli:cmd``
     - Command that failed
     - ``CalledProcessError``
   * - ``failure:cli:return_code``
     - Exit code from failed command
     - ``CalledProcessError``
   * - ``failure:cli:stdout``
     - Standard output from failed command
     - ``CalledProcessError``
   * - ``failure:cli:stderr``
     - Standard error from failed command
     - ``CalledProcessError``

Error Metadata (Unexpected Errors)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

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

The metadata prefix (``failure:`` vs ``error:``) is determined dynamically based on whether the exception type is in ``KNOWN_FAILURE_EXCEPTIONS``.

Test Observer Integration
--------------------------

The Test Observer receives different status codes based on JUnit XML classification:

**JUnit XML to Test Observer Status Mapping:**

.. code-block:: yaml

    <error>   → ERROR     # Unexpected errors
    <failure> → FAILED    # Expected failures
    <skipped> → SKIPPED   # Skipped tests
    (success) → PASSED    # Passing tests

This mapping is implemented in ``.github/actions/test-observer/post-results/action.yaml``:

.. code-block:: yaml

    "status": (
      if .error != [] then "ERROR"
      elif .failure != [] then "FAILED"
      elif .skipped != [] then "SKIPPED"
      else "PASSED"
      end
    )

Automatic Retry Behavior
-------------------------

The GitHub Actions workflow skips automatic retries when errors are detected:

.. code-block:: yaml

    - name: Calculate Job Status
      run: |
        errors=$(yq -p xml -oy '.testsuites.+@errors' "$JUNIT_FILE")
        if [[ "$errors" -gt 0 ]]; then
          echo "has_errors=true" >> $GITHUB_OUTPUT
        else
          echo "has_errors=false" >> $GITHUB_OUTPUT
        fi

    - name: Trigger rerun on Test Observer if failure
      if: |
        failure() && 
        steps.calculate_job_status.outputs.has_errors != 'true' &&
        !(steps.build_bundle.conclusion == 'failure' && 
          steps.build_bundle.outputs.invalid_input == 'true')
      uses: ./.github/actions/test-observer/trigger-rerun

**Retry Logic:**

- **Failures** (``<failure>`` in JUnit): May trigger automatic retry if the overall job failed
- **Errors** (``<error>`` in JUnit): Do **not** trigger automatic retry, retry would be handled by Test Observer if desired

Adding New Exception Types
---------------------------

To classify a new exception type as a known failure:

1. Add it to ``KNOWN_FAILURE_EXCEPTIONS`` in ``conftest.py``:

   .. code-block:: python

       KNOWN_FAILURE_EXCEPTIONS = (
           JujuWaitTimeoutError,
           AssertionError,
           CalledProcessError,
           YourNewException,  # Add here
       )

2. (Optional) Add specific metadata collection logic in ``record_failure_execution_metadata`` if the exception requires special handling.

To ensure an exception is treated as an unexpected error, make sure it is **not** in ``KNOWN_FAILURE_EXCEPTIONS``.

Example Scenarios
-----------------

Scenario 1: Deployment Timeout (Known Failure)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

    # Test execution
    def test_deploy(juju_client, model):
        juju_client.deploy_bundle(...)
        juju_client.idle_for_period(timeout=timedelta(minutes=1))
        # Timeout after 1 minute

**Result:**

- JUnit: ``<failure message="Timed out...">``
- Test Observer: ``FAILED``
- Metadata: ``failure:message``, ``failure:charm:*:status``
- Retry: May be triggered

Scenario 2: Unexpected Python Error
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

    # Test execution
    def test_integration(juju_client):
        result = juju_client.get_status()
        # KeyError: 'applications' - unexpected bug in framework

**Result:**

- JUnit: ``<error message="KeyError: 'applications'">``
- Test Observer: ``ERROR``
- Metadata: ``error:message``, ``error:exception:message``
- Retry: **Not** triggered

Scenario 3: Graceful Skip After Failure
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

    # test_deploy fails with JujuWaitTimeoutError
    # test_integration runs assert_idle fixture
    
    @pytest.fixture(autouse=True)
    def assert_idle(...):
        try:
            juju_client.idle_for_period(...)
        except JujuWaitTimeoutError as e:
            pytest.skip(str(e))  # Gracefully skip

**Result:**

- JUnit: ``<skipped message="Timed out...">``
- Test Observer: ``SKIPPED``
- Metadata: ``skipped:message``
- No additional errors propagated
