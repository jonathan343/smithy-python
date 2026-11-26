#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
"""The transports generated async clients select by name."""

from typing import Any, Literal

from smithy_core.exceptions import MissingDependencyError

from .._settings import UNSET, HTTPOptions, HTTPTransportHooks, connect_timeout

__all__ = ["ASYNC_HTTP_TRANSPORTS", "AsyncTransportName"]

type AsyncTransportName = Literal["aiohttp", "crt"]
"""The transports an async client can create by name."""


def _aiohttp(options: HTTPOptions) -> Any:
    from .aiohttp import HAS_AIOHTTP, AIOHTTPClient, AIOHTTPClientConfig

    if not HAS_AIOHTTP:
        raise MissingDependencyError(
            "transport='aiohttp' needs aiohttp. Install it with: "
            "pip install 'smithy-http[aiohttp]'"
        )
    return AIOHTTPClient(
        client_config=AIOHTTPClientConfig(connect_timeout=connect_timeout(options))
    )


def _crt(options: HTTPOptions) -> Any:
    try:
        from .crt import AWSCRTHTTPClient, AWSCRTHTTPClientConfig
    except ImportError as e:
        raise MissingDependencyError(
            "transport='crt' needs awscrt. Install it with: "
            "pip install 'smithy-http[awscrt]'"
        ) from e
    settings: dict[str, Any] = {"connect_timeout": connect_timeout(options)}
    if options.crt_read_buffer_size is not UNSET:
        settings["read_buffer_size"] = options.crt_read_buffer_size
    return AWSCRTHTTPClient(client_config=AWSCRTHTTPClientConfig(**settings))


ASYNC_HTTP_TRANSPORTS = HTTPTransportHooks(
    {"aiohttp": _aiohttp, "crt": _crt}, default="aiohttp"
)
"""Applies the HTTP settings of a generated async client to its transport."""
