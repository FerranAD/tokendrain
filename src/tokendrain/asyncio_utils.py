"""Small ownership helpers for non-cancellable filesystem mutations."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial


async def durable_io[T, **P](operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Retain the caller's locks until the executor mutation actually finishes.

    Thread work cannot be cancelled. Delaying cancellation prevents an old write
    from overtaking a later credential replacement or deletion after lock release.
    The owned executor Future is shielded directly, rather than wrapping it in a
    Task that asyncio.run would cancel again during shutdown.
    """
    pending = asyncio.get_running_loop().run_in_executor(None, partial(operation, *args, **kwargs))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(pending)
            break
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result
