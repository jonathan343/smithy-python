#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

from collections import deque
from collections.abc import Iterator
from io import BytesIO
from typing import NamedTuple
from xml.etree.ElementTree import Element, ParseError, TreeBuilder, XMLParser

from smithy_core.exceptions import SmithyError
from smithy_core.interfaces import BytesReader


class XMLParseError(SmithyError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Error parsing XML: {message}")


class XMLEvent(NamedTuple):
    type: str
    elem: Element


class _XMLTreeBuilder(TreeBuilder):
    """Build elements and queue events using XMLParser's public target API."""

    def __init__(self) -> None:
        super().__init__()
        self.events: deque[XMLEvent] = deque()

    def start(self, tag: str, attrs: dict[str, str]) -> Element:
        element = super().start(tag, attrs)
        self.events.append(XMLEvent("start", element))
        return element

    def end(self, tag: str) -> Element:
        element = super().end(tag)
        self.events.append(XMLEvent("end", element))
        return element

    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        # Reject the declaration before its internal subset or external DTD can
        # be processed. Entity declarations require a DTD; normal XML escapes do not.
        raise XMLParseError("DTD declarations are not supported")


def xml_events(source: bytes | BytesReader) -> Iterator[XMLEvent]:
    """Read XML without permitting DTDs, entity declarations, or external access."""
    if isinstance(source, bytes):
        source = BytesIO(source)
    target = _XMLTreeBuilder()
    parser = XMLParser(target=target)  # noqa: S314 - target rejects all DTDs
    while chunk := source.read(16 * 1024):
        parser.feed(chunk)
        while target.events:
            yield target.events.popleft()
    parser.close()
    while target.events:
        yield target.events.popleft()


def tree_events(element: Element) -> Iterator[XMLEvent]:
    """Yield the start and end events of an already parsed element tree.

    This produces the same sequence of events ``iterparse`` would for the
    document rooted at ``element``.
    """
    yield XMLEvent("start", element)
    for child in element:
        yield from tree_events(child)
    yield XMLEvent("end", element)


class XMLEventReader:
    """Buffered iterator over XML pull parser events with peek support.

    Wraps an iterator of ``(event, element)`` tuples — either from
    ``xml_events`` (streaming from a byte source) or from an in-memory list
    (for flattened member replay).
    """

    def __init__(
        self,
        events: Iterator[XMLEvent],
        *,
        document: bool = False,
    ) -> None:
        self._iter = events
        self._pending: XMLEvent | None = None
        self._document = document
        self._depth = 0

    def __iter__(self):
        return self

    def __next__(self) -> XMLEvent:
        if self._pending is not None:
            result = self._pending
            self._pending = None
            return result
        return self._next()

    def _next(self) -> XMLEvent:
        try:
            event = next(self._iter)
            if event.type == "start":
                self._depth += 1
                if self._depth > 128:
                    raise XMLParseError("Maximum XML nesting depth of 128 exceeded")
            else:
                self._depth -= 1
                if self._document and self._depth == 0:
                    # The parser must consume the epilog before returning the root's
                    # end event, otherwise malformed trailing content goes unnoticed.
                    if next(self._iter, None) is not None:
                        raise XMLParseError("Unexpected content after the root element")
            return event
        except ParseError as error:
            raise XMLParseError(str(error)) from error

    def has_next(self) -> bool:
        if self._pending is not None:
            return True
        try:
            self._pending = self._next()
            return True
        except StopIteration:
            return False

    def peek(self) -> XMLEvent:
        if self._pending is None:
            self._pending = self._next()
        return self._pending
