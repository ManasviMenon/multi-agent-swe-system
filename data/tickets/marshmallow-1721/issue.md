# Is it intentional that setting parameter unknown=<any non-empty string> behaves the same as unknown=EXCLUDE?

I was playing around with some of the unknown options and noticed that if you set unknown to a random string when instantiating a schema, it behaves the same as setting `unknown=EXCLUDE`.

Is this intentional? 

```
class TestSchema(Schema):
    schema_field = fields.Str(required=True)

test = dict(schema_field="random", non_schema_field="also random")
schema = TestSchema(unknown=EXCLUDE)
print(schema.load(test))
# {'schema_field': 'random'}
```

```
schema = TestSchema(unknown=INCLUDE)
print(schema.load(test))
# {'schema_field': 'random', 'non_schema_field': 'also random'}
```

```
schema = TestSchema(unknown="1234")
print(schema.load(test))
# {'schema_field': 'random'}
```
