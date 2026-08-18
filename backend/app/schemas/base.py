from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Base for every request/response schema: the wire format is camelCase
    (idiomatic for the TypeScript frontend), while Python code everywhere
    else — construction, `.model_validate()` from ORM objects, attribute
    access — keeps using normal snake_case names.

    `populate_by_name=True` is what makes both work: a model can be built
    from snake_case kwargs in Python *or* from a camelCase JSON body: the
    frontend is never asked to know or send Python's naming convention, and
    existing backend code that constructs these models by snake_case
    keyword didn't need to change when this was introduced.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )
