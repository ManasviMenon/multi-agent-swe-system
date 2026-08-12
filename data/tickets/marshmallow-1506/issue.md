# JSON deserialization converts dots in field names to deep dictionaries.

**Summary**
The marshmallow JSON deserializer interprets dots as meaning a sub-object in user-provided input, violating the JSON standard.

**Minimal example**
```py
from marshmallow import Schema, INCLUDE

class MySchema(Schema):
    class Meta:
        unknown = INCLUDE

> s = MySchema()
> s.load({"01.12.2019": 1, "01.01.2020": 0})
{'01': {'01': {'2020': 0}, '12': {'2019': 1}}}

> s.loads("""{"01.12.2019": 1, "01.01.2020": 0}""")
{'01': {'01': {'2020': 0}, '12': {'2019': 1}}}
```

**Discussion**
While I appreciate the idea of allowing "compact notation" using dotted paths, this even causes problems in attribute/data_key specifications, such as #1225 (I like the idea discussed there of using lists of strings instead of in-band signaling using path notation). Either way, applying the dot path transformation *on the data* is counter-intuitive, undocumented and dangerous as it's hard to discover during testing.

**Motivation**
My schema allows somewhat "free form" key-value attributes for certain objects (think os.environ, or some other meta-attribute table). The schema (in my case) enforces that all field values are numeric, like so:
```
class MetaAttributeSchema(Schema):
    class Meta:
        unknown = INCLUDE

    @validates_schema
    def validate_types(self, data, **kwargs):
        errors = {}
        for key, value in data.items():
            try:
                NumberValidator.deserialize(value)
            except ValidationError as e:
                errors[key] = e.messages
        if errors:
            raise ValidationError(errors)
```

