# Setting many=True through Meta - SchemaOpts

I would like to propose adding support for setting `many=True` within the `Meta` class of a schema in Marshmallow. This feature would allow developers to configure a schema to always handle lists of objects by default, streamlining schema instantiation (no need to set `many=True` at every instantiation), reducing code redundancy and increasing consistency while reducing errors (the schema always handles lists of objects as intended).

It finds its use cases where the instantiation is not always in place, but is first passed e.g. to a decorator that does the instantiation, loading of the schema (etc. other logic).

Currently, to achieve this behavior, we must either set `many=True` during schema instantiation or create a custom base schema that enforces this behavior. Some example workaround:

```python
class MySchema(Schema):
    field = fields.String()

    @classmethod
    def create(cls, many=True, *args, **kwargs):
        return cls(many=many, *args, **kwargs)


schema = MySchema.create()
data = schema.load([{"field": "value"}])
schema = MySchema(many=True)
data = schema.load([{"field": "value"}])
```

Example of implemented:

```python
class MySchema(Schema):
    field = fields.String()

    class Meta:
        many = True


schema = MySchema()
data = schema.load([{"field": "value"}])
```

Throws as expected ValidationError, because this options is not possible to be set through Meta as other options do.

With this in place it would be always, if someone wants, giving more control, instead of just at the instance level.

If this would be fine to be given a try, I'm happy to prepare a PR for that :smiley: 
