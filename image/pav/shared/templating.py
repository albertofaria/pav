# ---------------------------------------------------------------------------- #

from __future__ import annotations

import shlex
from collections.abc import Mapping
from typing import Optional

import yaml
from jinja2 import Undefined, UndefinedError
from jinja2.sandbox import ImmutableSandboxedEnvironment
from kubernetes_asyncio.client import ApiClient, CoreV1Api  # type: ignore

# ---------------------------------------------------------------------------- #


def validate_templates(obj: object) -> None:
    """
    Validate the _syntax_ of all templates in `obj`.

    Also ensures that all objects are bool, float, int, None, list, or dict, and
    that list items and dict values obey the same rule, and that dict keys are
    strings.
    """

    env = _create_env(context={}, api_client=None)

    def validate(o: object) -> None:

        if isinstance(o, dict):

            for (key, value) in o.items():
                if not isinstance(key, str):
                    raise ValueError("All mapping keys must be strings")
                validate(value)

        elif isinstance(o, (list, tuple)):

            for value in o:
                validate(value)

        elif isinstance(o, str):

            env.compile(o)

        elif not isinstance(o, (bool, float, int, type(None))):

            raise ValueError(f"Unsupported type {type(o).__qualname__}")

    validate(obj)


async def evaluate_templates(
    obj: object, context: Mapping[str, object], api_client: Optional[ApiClient]
) -> object:
    """
    Go over all string fields in maps and lists (recursively) and evaluate them
    as Jinja templates. The final string resulting from the evaluation of each
    template is then substituted for the template as the field's value. The
    resulting whole object is returned (`obj` is not mutated).

    Expressions must evaluate to string or numeric values.

    If a template sets the `yaml` variable to `true`, such as by including the
    statement `{% set yaml = true %}`, then the final string resulting from the
    template's evaluation is parsed as YAML, and the field takes on the
    resulting value.

    `api_client` can be `None` to help writing tests.
    """

    env = _create_env(context=context, api_client=api_client)

    async def evaluate(o: object) -> object:

        if isinstance(o, dict):

            return {key: await evaluate(value) for (key, value) in o.items()}

        elif isinstance(o, (list, tuple)):

            return [await evaluate(item) for item in o]

        elif isinstance(o, str):

            template = env.from_string(o)

            new_o = await template.render_async()
            is_yaml = getattr(await template.make_module_async(), "yaml", False)

            if not isinstance(is_yaml, bool):
                raise TypeError

            if is_yaml:
                new_o = yaml.safe_load(new_o)

            return new_o

        else:

            return o

    return await evaluate(obj)


def _create_env(
    context: Mapping[str, object], api_client: Optional[ApiClient]
) -> ImmutableSandboxedEnvironment:
    def finalize(value: object) -> object:

        # This custom finalizer allows operations like 'or' on undefined values,
        # but doesn't allow expressions to evaluate to 'undefined'.

        if isinstance(value, Undefined):
            raise UndefinedError("Expressions must not evaluate to undefined")

        if not isinstance(value, (str, int, float)):
            raise TypeError(
                "Expressions must evaluate to a string or numeric value"
            )

        return str(value)

    def tobash(value: object) -> str:

        # This function ensures that newlines are escaped using ANSI-C quoting:
        # https://www.gnu.org/software/bash/manual/bash.html#ANSI_002dC-Quoting

        if isinstance(value, Undefined):
            raise UndefinedError(
                "Filter 'tobash' may not be applied to undefined"
            )

        if not isinstance(value, (str, int, float)):
            raise TypeError("Filter 'tobash' expects a string or numeric value")

        value_str = str(value)

        if value_str:
            result = R"$'\n'".join(
                (shlex.quote(s) if s else "") for s in value_str.split("\n")
            )
            assert "\n" not in result
            return result
        else:
            return "''"

    env = ImmutableSandboxedEnvironment(
        trim_blocks=True,
        lstrip_blocks=True,
        finalize=finalize,
        enable_async=True,
    )

    env.filters["tobash"] = tobash
    env.globals |= context

    if api_client is not None:

        # so typechecker knows it never becomes None
        api_client_copy = api_client

        async def get_pvc(name: str, namespace: str) -> object:

            if not isinstance(name, str) or not isinstance(namespace, str):
                raise TypeError(
                    "Arguments to function get_pvc() must be strings"
                )

            api = CoreV1Api(api_client_copy)

            pvc = await api.read_namespaced_persistent_volume_claim(
                name=name, namespace=namespace
            )

            return api_client_copy.sanitize_for_serialization(pvc)

        env.globals["get_pvc"] = get_pvc

    return env


# ---------------------------------------------------------------------------- #
