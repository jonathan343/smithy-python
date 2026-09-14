#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

from collections.abc import Iterator
from typing import NamedTuple
from xml.etree.ElementTree import Element


class XMLEvent(NamedTuple):
    type: str
    elem: Element


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
    ``iterparse`` (streaming from a byte source) or from an in-memory list
    (for flattened member replay).
    """

    def __init__(self, events: Iterator[tuple[str, Element] | XMLEvent]) -> None:
        self._iter = events
        self._pending: XMLEvent | None = None

    def __iter__(self):
        return self

    def __next__(self) -> XMLEvent:
        if self._pending is not None:
            result = self._pending
            self._pending = None
            return result
        return self._next()

    def _next(self) -> XMLEvent:
        event = next(self._iter)
        if isinstance(event, XMLEvent):
            return event
        event_type, elem = event
        return XMLEvent(event_type, elem)

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
