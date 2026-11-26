#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
"""Validated HTTP settings, applied the same way by sync and async clients."""

import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from enum import Enum
from math import isfinite
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, cast

from smithy_core.shapes import ShapeType
from smithy_core.traits import LongPollTrait

from .exceptions import SmithyHTTPError
from .interfaces import HTTPRequestConfiguration

if TYPE_CHECKING:
    from smithy_core.schemas import APIOperation


class Unset(Enum):
    """The type of :py:data:`UNSET`."""

    UNSET = "UNSET"


UNSET: Final = Unset.UNSET
"""Marks a setting the config leaves out, so the transport's default applies."""

DEFAULT_CONNECT_TIMEOUT: Final = 60.0
"""The connect timeout, in seconds, of a transport the SDK creates."""

DEFAULT_READ_TIMEOUT: Final = 60.0
"""The read timeout, in seconds, of a request on a transport the SDK creates."""

_SETTINGS: Final = ("connect_timeout", "read_timeout", "crt")
_CRT_SETTINGS: Final = ("read_buffer_size",)
_MAX_FLOAT: Final = sys.float_info.max


@dataclass(frozen=True, kw_only=True)
class HTTPOptions:
    """A config's validated HTTP settings.

    A timeout of None disables it, while ``UNSET`` leaves the transport's default.
    The crt transport rejects a ``connect_timeout`` of None.
    """

    connect_timeout: float | None | Unset = UNSET
    read_timeout: float | None | Unset = UNSET
    crt: Mapping[str, int] | Unset = UNSET
    """The ``crt`` settings, which may be empty, or ``UNSET`` if there are none."""

    @property
    def crt_read_buffer_size(self) -> int | Unset:
        return UNSET if self.crt is UNSET else self.crt.get("read_buffer_size", UNSET)

    def connection(self) -> dict[str, Any]:
        """The settings fixed when a transport is created, by setting name."""
        return {
            name: value
            for name, value in (
                ("connect_timeout", self.connect_timeout),
                ("crt", self.crt),
            )
            if value is not UNSET
        }


@dataclass(kw_only=True)
class ExactRequestConfiguration(HTTPRequestConfiguration):
    """Request settings in which a ``read_timeout`` of None means no deadline.

    In a plain :py:class:`HTTPRequestConfiguration`, None means the transport's
    default.
    """


def read_timeout(
    request_config: HTTPRequestConfiguration | None, default: float | None
) -> float | None:
    """The read timeout of a request, or None for no deadline.

    :param request_config: The request's settings.
    :param default: The transport's read timeout.
    """
    if request_config is None:
        return default
    if isinstance(request_config, ExactRequestConfiguration):
        return request_config.read_timeout
    return (
        default if request_config.read_timeout is None else request_config.read_timeout
    )


def connect_timeout(options: HTTPOptions) -> float | None:
    """The connect timeout of a transport the SDK creates with these settings."""
    if options.connect_timeout is UNSET:
        return DEFAULT_CONNECT_TIMEOUT
    return options.connect_timeout


def parse_settings(settings: object) -> HTTPOptions:
    """Validate a config's ``http`` settings.

    :raises SmithyHTTPError: If a setting is unknown or its value is invalid.
    """
    if settings is None:
        return HTTPOptions()
    if not isinstance(settings, Mapping):
        raise SmithyHTTPError(
            f"http must be a dict of HTTP settings, got {type(settings).__name__}"
        )
    values: dict[str, Any] = {}
    for key, value in cast(Mapping[object, object], settings).items():
        if key == "crt":
            values["crt"] = _crt(value)
        elif key in ("connect_timeout", "read_timeout"):
            values[key] = _timeout(key, value)
        else:
            raise SmithyHTTPError(
                f"Unknown HTTP setting {key!r}. Valid settings are: "
                f"{', '.join(_SETTINGS)}"
            )
    return HTTPOptions(**values)


class HTTPTransportHooks:
    """Creates a client's HTTP transport and gives it each call's settings.

    Generated clients set one as their ``_transport_hooks``.
    """

    def __init__(
        self,
        factories: Mapping[str, Callable[[HTTPOptions], Any]],
        default: str,
    ) -> None:
        """
        :param factories: Create each transport the SDK can select, by name.
        :param default: The name of the transport used when the config names none.
        """
        self._factories = factories
        self._default = default

    def setup(self, config: Any) -> Any:
        """Return the transport for a client's base config.

        Creates the transport the config names, or the default transport when it
        names none.

        :raises SmithyHTTPError: If the settings are invalid, name an unknown
            transport, or set what the transport can't take.
        """
        transport = config.transport
        options = parse_settings(getattr(config, "http", None))
        if transport is not None and not isinstance(transport, str):
            if options.crt is not UNSET:
                raise _foreign_namespace(f"a {type(transport).__name__} instance")
            if options.connect_timeout is not UNSET:
                raise SmithyHTTPError(
                    "connect_timeout applies to a transport the SDK creates, but the "
                    f"config sets a {type(transport).__name__} instance. Set it on "
                    "that transport's own config, or set transport to one of: "
                    f"{self._names()}"
                )
            return transport
        name = self._default if transport is None else transport
        if (factory := self._factories.get(name)) is None:
            raise SmithyHTTPError(
                f"Unknown transport {name!r}. Choose one of: {self._names()}, or set "
                "a transport instance."
            )
        if name != "crt" and options.crt is not UNSET:
            raise _foreign_namespace(f"the {name} transport")
        return factory(options)

    def send_options(
        self,
        config: Any,
        base: Any,
        *,
        operation: "APIOperation[Any, Any] | None" = None,
        created: bool = False,
    ) -> Mapping[str, Any]:
        """Return the keyword arguments each send of a call gives the transport.

        Settings a call's config leaves out are those of the client's base config.
        A read timeout left out everywhere is the SDK's default on a transport
        setup created, and the transport's own default on any other.

        :param operation: The operation being called.
        :param created: Whether the call sends with the transport setup created.
        :raises SmithyHTTPError: If the call changes a setting fixed when the
            transport was created, including one the base config leaves out, or
            sets its read timeout in two places.
        """
        transport = config.transport
        if isinstance(transport, str):
            raise SmithyHTTPError(
                f"transport={transport!r} can only be set when the client is created. "
                "A plugin for one call may set a transport instance instead."
            )
        options = parse_settings(getattr(config, "http", None))
        changed: list[str] = []
        if config is not base:
            # Setup validated the base settings, so a call may only repeat them.
            fixed = parse_settings(getattr(base, "http", None))
            options = _inherit(options, fixed)
            connection, fixed_connection = options.connection(), fixed.connection()
            changed = [
                name
                for name in sorted(connection.keys() | fixed_connection.keys())
                if connection.get(name, UNSET) != fixed_connection.get(name, UNSET)
            ]
        if changed:
            raise SmithyHTTPError(
                f"{', '.join(changed)} can't be changed for one call, since it is set "
                "when the client creates its transport. Set it when creating the "
                "client instead."
            )
        legacy: HTTPRequestConfiguration | None = getattr(
            config, "http_request_config", None
        )
        if options.read_timeout is UNSET:
            # A legacy read timeout of None means the default, not none, unless
            # it's exact.
            if created and (
                legacy is None
                or (
                    legacy.read_timeout is None
                    and not isinstance(legacy, ExactRequestConfiguration)
                )
            ):
                return {
                    "request_config": ExactRequestConfiguration(
                        read_timeout=_default_read_timeout(operation)
                    )
                }
            return {} if legacy is None else {"request_config": legacy}
        if legacy is not None and legacy.read_timeout is not None:
            raise SmithyHTTPError(
                "The read timeout is set by both http['read_timeout'] and "
                "http_request_config. Set it only in http."
            )
        return {
            "request_config": ExactRequestConfiguration(
                read_timeout=options.read_timeout
            )
        }

    def _names(self) -> str:
        return ", ".join(map(repr, self._factories))


def _default_read_timeout(operation: "APIOperation[Any, Any] | None") -> float | None:
    """The read timeout of an operation whose settings leave it out.

    A request that streams its body, or whose response is an event stream, has
    none, since a deadline on reading the response would end it while it's still
    active. A response that streams a blob keeps it, to bound each read. A long
    poll waits at least as long as its model says the service may hold it.
    """
    if operation is None:
        return DEFAULT_READ_TIMEOUT
    if operation.input_stream_member is not None:
        return None
    output = operation.output_stream_member
    if output is not None and output.expect_member_target().shape_type is (
        ShapeType.UNION
    ):
        return None
    if (long_poll := operation.schema.get_trait(LongPollTrait)) is not None:
        return max(DEFAULT_READ_TIMEOUT, long_poll.timeout_millis / 1000)
    return DEFAULT_READ_TIMEOUT


def _inherit(options: HTTPOptions, base: HTTPOptions) -> HTTPOptions:
    """Fill the settings ``options`` leaves out from ``base``."""
    values: dict[str, Any] = {}
    for f in fields(HTTPOptions):
        value = getattr(options, f.name)
        values[f.name] = getattr(base, f.name) if value is UNSET else value
    return HTTPOptions(**values)


def _foreign_namespace(transport: str) -> SmithyHTTPError:
    return SmithyHTTPError(
        f"'crt' settings apply only to the crt transport, but the client uses "
        f"{transport}. Remove them, or set transport='crt'."
    )


def _timeout(name: str, value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, int) and abs(value) > _MAX_FLOAT:
        # isfinite can't convert it to a float, and its repr may be too long to
        # write.
        raise SmithyHTTPError(
            f"{name} must be a positive number or None, got an int too large to "
            "be a float."
        )
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not isfinite(value)
        or value <= 0
    ):
        raise SmithyHTTPError(
            f"{name} must be a positive number or None, got {value!r}. None "
            "disables the timeout."
        )
    return value


def _crt(value: object) -> Mapping[str, int]:
    if not isinstance(value, Mapping):
        raise SmithyHTTPError(
            f"http['crt'] must be a dict of CRT settings, got {type(value).__name__}"
        )
    settings = cast(Mapping[object, object], value)
    for key in settings:
        if key not in _CRT_SETTINGS:
            raise SmithyHTTPError(
                f"Unknown CRT setting {key!r}. Valid settings are: "
                f"{', '.join(_CRT_SETTINGS)}"
            )
    if (size := settings.get("read_buffer_size", UNSET)) is UNSET:
        return MappingProxyType({})
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise SmithyHTTPError(
            f"http['crt']['read_buffer_size'] must be a positive int, got {size!r}"
        )
    return MappingProxyType({"read_buffer_size": size})
