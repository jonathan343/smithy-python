#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
"""Error helpers shared by the AWS protocols."""

from typing import Any

from smithy_core.exceptions import CallError
from smithy_core.schemas import APIOperation
from smithy_core.shapes import ShapeID


def unknown_error(
    *,
    operation: APIOperation[Any, Any],
    status: int,
    reason: str | None = None,
    code: str | None = None,
    error_id: ShapeID | None = None,
    retry_after: float | None = None,
    service_message: str | None = None,
    request_id: str | None = None,
) -> CallError:
    """Create the generic error raised when a response can't be matched to a
    modeled error.

    :param operation: The operation that was called.
    :param status: The HTTP status code of the response.
    :param reason: The HTTP reason phrase of the response, if any.
    :param code: The error code found in the response body, if any.
    :param error_id: The error shape ID identified from the response, if any. This
        is only included in the message if ``code`` is not set.
    :param retry_after: The retry delay requested by the server, if any.
    :param service_message: The service's diagnostic message, if present.
    :param request_id: The service's request identifier, if present.
    """
    message = f"Unknown error for operation {operation.schema.id} - status: {status}"
    if code is not None:
        message += f" - code: {code}"
    elif error_id is not None:
        message += f" - id: {error_id}"
    if reason is not None:
        message += f" - reason: {reason}"
    if service_message is not None:
        message += f" - message: {service_message}"
    if request_id is not None:
        message += f" - request id: {request_id}"

    is_timeout = status == 408
    is_throttle = status == 429

    return CallError(
        message=message,
        fault="client" if status < 500 else "server",
        is_throttling_error=is_throttle,
        is_timeout_error=is_timeout,
        is_retry_safe=is_throttle or is_timeout or None,
        retry_after=retry_after,
    )
