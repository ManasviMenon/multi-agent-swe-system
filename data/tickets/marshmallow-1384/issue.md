# Dotted `only` and `exclude` not working for nested schema instances

#1229 fixed propagation of `only` and `exclude` for nested schema classes but no instances.
Dotted strings passed to `only` and `exclude` are not respected when a schema instance is nested.


```python
from marshmallow import Schema, fields


class Child(Schema):
    name = fields.String()
    age = fields.Integer()


class Parent(Schema):
    favorite = fields.Nested(Child, only=("name", "age"))
    children = fields.List(fields.Nested(Child, only=("name", "age")))


class Parent2(Schema):
    favorite = fields.Nested(Child(only=("name", "age")))
    children = fields.List(fields.Nested(Child(only=("name", "age"))))


family = {
    "favorite": {"name": "Lily", "age": 15},
    "children": [{"name": "Tommy", "age": 12}, {"name": "Lily", "age": 15}],
}
# should be the same
print(Parent(only=("favorite.age", "children.name")).dump(family))
print(Parent2(only=("favorite.age", "children.name")).dump(family))
```

Expected output:

```
{'children': [{'name': 'Tommy'}, {'name': 'Lily'}], 'favorite': {'age': 15}}
{'children': [{'name': 'Tommy'}, {'name': 'Lily'}], 'favorite': {'age': 15}}
```

Actual output:

```
{'favorite': {'age': 15}, 'children': [{'name': 'Tommy'}, {'name': 'Lily'}]}
{'favorite': {'age': 15, 'name': 'Lily'}, 'children': [{'age': 12, 'name': 'Tommy'}, {'age': 15, 'name': 'Lily'}]}
```

