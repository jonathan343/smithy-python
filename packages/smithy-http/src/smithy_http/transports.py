#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
"""HTTP settings of generated clients, and the transports they select."""

from typing import Any, Literal, TypedDict

from smithy_core.exceptions import MissingDependencyError

from ._settings import HTTPOptions, HTTPTransportHooks, connect_timeout

__all__ = [
    "HTTP_TRANSPORTS",
    "CRTSettings",
    "HTTPSettings",
    "TransportName",
]


class CRTSettings(TypedDict, total=False):
    """Settings of the ``crt`` transport, used only by async clients."""

    read_buffer_size: int
    """How many bytes of a request body to read at a time."""


class HTTPSettings(TypedDict, total=False):
    """A client's HTTP settings.

    A timeout left out is the SDK's default on a transport the SDK creates from its
    name, whichever transport that is, and the transport's own default on a
    transport instance. A timeout set to None is disabled, except the crt
    transport's ``connect_timeout``. No timeout bounds a whole call. Settings
    applied when the transport is created, such as ``connect_timeout``, apply only
    to a transport the SDK creates from its name, not to a transport instance, and
    can't change for one call.
    """

    connect_timeout: float | None
    """How long, in seconds, to wait to establish a connection. Defaults to 60.

    The crt transport can't wait indefinitely, so it rejects None and more than
    2147483.647 seconds, and rounds to the nearest millisecond, at least 1.
    """

    read_timeout: float | None
    """How long, in seconds, to wait for the response to start, and then for each
    read of its body. Operation plugins may change it for one call.

    Defaults to 60. Calls that stream their request body or receive an event
    stream have no default, and a long poll waits at least as long as its model
    says the service may hold it. Set a longer timeout, or None, for calls the
    service may hold open longer that aren't modeled as long polls.
    """

    crt: CRTSettings
    """Settings only the ``crt`` transport takes."""


type TransportName = Literal["urllib3"]
"""The transports a sync client can create by name."""


def _urllib3(options: HTTPOptions) -> Any:
    from .urllib3 import HAS_URLLIB3, Urllib3HTTPClient, Urllib3HTTPClientConfig

    if not HAS_URLLIB3:
        raise MissingDependencyError(
            "transport='urllib3' needs urllib3. Install it with: "
            "pip install 'smithy-http[urllib3]'"
        )
    return Urllib3HTTPClient(
        client_config=Urllib3HTTPClientConfig(connect_timeout=connect_timeout(options))
    )


HTTP_TRANSPORTS = HTTPTransportHooks({"urllib3": _urllib3}, default="urllib3")
"""Applies the HTTP settings of a generated sync client to its transport."""
