from functools import wraps
from time import sleep
from typing import Callable, TypeVar

T = TypeVar("T")


def retry_on_failure(message: str, max_retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """Retry decorator for vault operations that may fail transiently.

    Args:
        message: Substring to look for in exception messages to trigger a retry
        max_retries: Maximum number of retry attempts
        delay: Initial delay between retries in seconds
        backoff: Multiplier for delay on each retry
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exception = None
            current_delay = delay

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except RuntimeError as e:
                    last_exception = e
                    if attempt < max_retries and message in str(e).lower():
                        sleep(current_delay)
                        current_delay *= backoff
                    else:
                        # Re-raise the last exception after all retries exhausted
                        raise
                except Exception:
                    # Don't retry on non-RuntimeError exceptions (e.g., programming errors)
                    raise

            # This should never be reached, but satisfy type checker
            raise last_exception

        return wrapper

    return decorator
