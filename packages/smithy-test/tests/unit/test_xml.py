# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from smithy_test.xml import xml_equal


def test_xml_equivalent_names_and_indentation() -> None:
    assert xml_equal(
        b'<a:R xmlns:a="urn:r"><a:x k="v"> a </a:x></a:R>',
        b'<R xmlns="urn:r">\n<x k="v"> a </x>\n</R>',
    )
    assert xml_equal(b"", b"")
    assert xml_equal(b"<R><a>1</a><b>2</b></R>", b"<R><b>2</b><a>1</a></R>")


def test_xml_differences_are_significant() -> None:
    assert not xml_equal(b'<R xmlns="urn:a"/>', b'<R xmlns="urn:b"/>')
    assert not xml_equal(b'<R k="a"/>', b'<R k="b"/>')
    assert not xml_equal(b"<R> a </R>", b"<R>a</R>")
    assert not xml_equal(b"<R><x>1</x><x>2</x></R>", b"<R><x>2</x><x>1</x></R>")
    assert not xml_equal(b"<R>bad<x/></R>", b"<R><x/></R>")
    assert not xml_equal(b"<R><x/>bad</R>", b"<R><x/></R>")
