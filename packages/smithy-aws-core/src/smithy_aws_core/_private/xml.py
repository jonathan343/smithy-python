#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
"""Helpers shared by the XML-based AWS protocols."""

from importlib.util import find_spec
from typing import TYPE_CHECKING
from xml.etree.ElementTree import Element

from smithy_core.exceptions import MissingDependencyError

_HAS_XML = find_spec("smithy_xml") is not None

if TYPE_CHECKING or _HAS_XML:
    from smithy_xml import XMLParseError, parse_xml


def assert_xml() -> None:
    if not _HAS_XML:
        raise MissingDependencyError(
            "Attempted to use XML codec, but smithy-xml is not installed."
        )


def local_name(tag: str) -> str:
    """Strip namespace URI from an element tag: {uri}local -> local."""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def find_child(element: Element, name: str) -> Element | None:
    """Return the first child element whose local name matches ``name``."""
    for child in element:
        if local_name(child.tag) == name:
            return child
    return None


def parse_xml_root(body: bytes) -> Element | None:
    """Parse the root element of an XML document, or None if it isn't valid XML."""
    if not body:
        return None
    assert_xml()
    try:
        return parse_xml(body)
    except XMLParseError:
        return None


def unwrap(root: Element | None, wrapper_elements: tuple[str, ...]) -> Element | None:
    """Descend through protocol wrapper elements, outermost first.

    The root element must match the first wrapper. Returns the innermost wrapper,
    or None if any wrapper is missing.
    """
    if root is None or local_name(root.tag) != wrapper_elements[0]:
        return None
    element = root
    for wrapper in wrapper_elements[1:]:
        element = find_child(element, wrapper)
        if element is None:
            return None
    return element


def find_rest_xml_error(root: Element | None) -> Element | None:
    """Find the ``Error`` element of a restXml error response.

    Errors are either wrapped, ``<ErrorResponse><Error>``, or bare, ``<Error>``.
    Both forms are accepted regardless of the service's ``noErrorWrapping``
    setting because the response itself is unambiguous.
    """
    if root is None:
        return None
    match local_name(root.tag):
        case "Error":
            return root
        case "ErrorResponse":
            return find_child(root, "Error")
        case _:
            return None


def child_text(element: Element | None, name: str) -> str | None:
    """Read a direct child's text, ignoring namespace prefixes."""
    if element is None:
        return None
    child = find_child(element, name)
    return child.text or None if child is not None else None
