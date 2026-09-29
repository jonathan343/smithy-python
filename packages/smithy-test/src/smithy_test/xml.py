# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""XML protocol assertions: expanded names, attributes, and significant text."""

from xml.etree import ElementTree as ET


def xml_equal(actual: bytes, expected: bytes) -> bool:
    """Ignore prefix spelling, attribute order and indentation, not scalar whitespace."""

    if not actual or not expected:
        return actual == expected

    def equivalent(left: ET.Element, right: ET.Element) -> bool:
        if (
            left.tag != right.tag
            or left.attrib != right.attrib
            or len(left) != len(right)
        ):
            return False
        left_text, right_text = left.text or "", right.text or ""
        if len(left):
            left_text = left_text if left_text.strip() else ""
            right_text = right_text if right_text.strip() else ""
        if left_text != right_text:
            return False
        left_tail, right_tail = left.tail or "", right.tail or ""
        if (left_tail if left_tail.strip() else "") != (
            right_tail if right_tail.strip() else ""
        ):
            return False
        # Structure members are unordered; preserve relative order among repeated
        # names so list item order remains significant.
        return all(
            equivalent(a, b)
            for a, b in zip(
                sorted(left, key=lambda e: e.tag),
                sorted(right, key=lambda e: e.tag),
                strict=True,
            )
        )

    return equivalent(ET.fromstring(actual), ET.fromstring(expected))  # noqa: S314
