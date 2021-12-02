# ---------------------------------------------------------------------------- #

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Union

import pytest
from jinja2 import TemplateSyntaxError, UndefinedError

from pav.shared.templating import evaluate_templates

# ---------------------------------------------------------------------------- #


class TestEvaluateTemplates:
    @dataclass(frozen=True)
    class ValidTestCase:
        """The test will succeed if evaluating templates for every object in
        'objects', with the given context, results in the given expected
        value."""

        objects: Sequence[object]
        context: Mapping[str, object]
        expected: object

    @dataclass(frozen=True)
    class InvalidTestCase:
        """The test will succeed if evaluating templates for every object in
        'objects', with the given context, results in the given exception being
        raised."""

        objects: Sequence[object]
        context: Mapping[str, object]
        error: type[Exception]

    TestCase = Union[ValidTestCase, InvalidTestCase]

    test_cases: Sequence[TestCase] = [
        ValidTestCase(
            objects=[
                None,
                "{% set yaml = true %}{{ '' }}",
                "{% set yaml = true %}{{ ' \n ' }}",
            ],
            context={},
            expected=None,
        ),
        ValidTestCase(
            objects=["", "{{ '' }}"],
            context={},
            expected="",
        ),
        ValidTestCase(
            objects=[" \n ", "{{ ' \n ' }}"],
            context={},
            expected=" \n ",
        ),
        ValidTestCase(
            objects=[
                "hello",
                "{% set yaml = false %}hello",
                "{% set yaml = true %}hello",
            ],
            context={},
            expected="hello",
        ),
        ValidTestCase(
            objects=[
                {
                    "a": 42,
                    "b": "{{ 1 + 2 }}",
                    "c": "{% set yaml = true %}{{ 1 + 2 }}",
                }
            ],
            context={},
            expected={"a": 42, "b": "3", "c": 3},
        ),
        ValidTestCase(
            objects=[
                {
                    "a": {
                        "1": (
                            "ab{% if b==3 %}c{% endif %}"
                            "{% if x is defined %}d{% endif %}"
                        )
                    },
                    "b": ["hello", "{{ a+b*c }}"],
                }
            ],
            context={"a": 2, "b": 3, "c": 4},
            expected={"a": {"1": "abc"}, "b": ["hello", "14"]},
        ),
        ValidTestCase(
            objects=["a{{ '42' }}b"],
            context={},
            expected="a42b",
        ),
        ValidTestCase(
            objects=[R"""{% set yaml = true %}x: {{ '[1, "2", 3]' }}"""],
            context={},
            expected={"x": [1, "2", 3]},
        ),
        InvalidTestCase(
            objects=["{{", "{{ }}"],
            context={},
            error=TemplateSyntaxError,
        ),
        ValidTestCase(
            objects=["42", "{{ 42 }}", "{{ '42' }}"],
            context={},
            expected="42",
        ),
        ValidTestCase(
            objects=["a{{ '42' }}b"],
            context={},
            expected="a42b",
        ),
        ValidTestCase(
            objects=[42, "{% set yaml = true %}{{ 42 }}"],
            context={},
            expected=42,
        ),
        InvalidTestCase(
            objects=[R"""x: {{ [1, "2", 3] }}"""],
            context={},
            error=TypeError,
        ),
        #
        # undefined
        #
        ValidTestCase(
            objects=["{% set yaml = true %}{{ abc or 42 }}"],
            context={},
            expected=42,
        ),
        InvalidTestCase(
            objects=["{{ abc }}", "{{ (abc + 3) or 42 }}"],
            context={},
            error=UndefinedError,
        ),
        #
        # |tobash
        #
        ValidTestCase(
            objects=["{{ '' | tobash }}"],
            context={},
            expected="''",
        ),
        ValidTestCase(
            objects=[R"{{ '\n' | tobash }}"],
            context={},
            expected=R"$'\n'",
        ),
        ValidTestCase(
            objects=[R"{{ ' a\nb' | tobash }}"],
            context={},
            expected=R"' a'$'\n'b",
        ),
        ValidTestCase(
            objects=["{{ 42 | tobash }}", "{{ '42' | tobash }}"],
            context={},
            expected="42",
        ),
        InvalidTestCase(
            objects=["{{ [42] | tobash }}"],
            context={},
            error=TypeError,
        ),
        #
        # |tojson
        #
        ValidTestCase(
            objects=[R"{{ ' a\nb ' | tojson }}", "{{ ' a\nb ' | tojson }}"],
            context={},
            expected=R'" a\nb "',
        ),
        ValidTestCase(
            objects=[R"""{{ { "a": "1", "b": 2 } | tojson }}"""],
            context={},
            expected=R'{"a": "1", "b": 2}',
        ),
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", test_cases)
    async def test(self, case: TestCase) -> None:

        assert case.objects

        if isinstance(case, TestEvaluateTemplates.ValidTestCase):

            for obj in case.objects:
                result = await evaluate_templates(obj, case.context, None)
                assert result == case.expected

        else:

            for obj in case.objects:
                with pytest.raises(case.error):
                    await evaluate_templates(obj, case.context, None)


# ---------------------------------------------------------------------------- #
