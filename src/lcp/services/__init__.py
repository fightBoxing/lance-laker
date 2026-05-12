"""Business-logic services package.

Services translate Pydantic schemas into ORM operations and back.  Routers stay
thin: they only handle HTTP-specific concerns (status codes, dependency
injection); persistence stays here.
"""
