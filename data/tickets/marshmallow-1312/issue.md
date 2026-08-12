# Add a way to generate a Schema from a dictionary

Generating schemas at runtime is a fairly common use case that I've run into a handful of times at this point ([webargs](https://github.com/marshmallow-code/webargs/blob/de061e037285fd08a42d73be95bc779f2a4e3c47/src/webargs/core.py#L50-L63), [environs](https://github.com/sloria/environs/blob/91b32a0e4bdf40281fab3978368164b27ce65e39/environs.py#L83-L95) , form builders...). 

It might make sense to incorporate this into marshmallow core.

## Proposed API

```python
>>> MyDynamicSchema = Schema.from_dict({"foo": fields.Str()})
>>> MyDynamicSchema
<class 'marshmallow.schema.GeneratedSchema'>
```

```python
class AlbumSchema(Schema):
     artist = fields.Nested(Schema.from_dict({"name": fields.Str()})
```
