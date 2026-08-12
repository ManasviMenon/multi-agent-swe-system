# List(Pluck()) raises while Pluck(many=True) works

For completeness, below is my example where I noticed this but a PR with at least a test added is on it's way and I'll see if I can figure out how to fix it.  This was broken by https://github.com/marshmallow-code/marshmallow/commit/a6fd2b6f759c1b99275f949b7663295f85cc7f27.

https://repl.it/@altendky/UnluckyInfiniteUnits-5
```python3
import attr
import marshmallow


@attr.s
class Member:
    name = attr.ib()
    other = attr.ib()


class MemberSchema(marshmallow.Schema):
    name = marshmallow.fields.String()
    other = marshmallow.fields.String()


@attr.s
class Holder:
    members = attr.ib()

class HolderSchema(marshmallow.Schema):
    members = marshmallow.fields.List(marshmallow.fields.Pluck(MemberSchema, field_name='name'))
    # members = marshmallow.fields.Pluck(MemberSchema, field_name='name', many=True)


def main():
    my_holder = Holder(
        members=[
            Member(name='name_0', other='other_0'),
            Member(name='name_1', other='other_1'),
        ],
    )

    dumped = HolderSchema().dump(my_holder)
    print(dumped)

main()
```
```python-traceback
Traceback (most recent call last):
  File "main.py", line 45, in <module>
    main()
  File "main.py", line 42, in main
    dumped = HolderSchema().dump(my_holder)
  File "/home/runner/.local/share/virtualenvs/python3/lib/python3.7/site-packages/marshmallow/schema.py", line 553, in dump
    result = self._serialize(processed_obj, many=many)
  File "/home/runner/.local/share/virtualenvs/python3/lib/python3.7/site-packages/marshmallow/schema.py", line 517, in _serialize
    value = field_obj.serialize(attr_name, obj, accessor=self.get_attribute)
  File "/home/runner/.local/share/virtualenvs/python3/lib/python3.7/site-packages/marshmallow/fields.py", line 325, in serialize
    return self._serialize(value, attr, obj, **kwargs)
  File "/home/runner/.local/share/virtualenvs/python3/lib/python3.7/site-packages/marshmallow/fields.py", line 695, in _serialize
    return self.inner._serialize(value, attr, obj, many=True, **kwargs)
  File "/home/runner/.local/share/virtualenvs/python3/lib/python3.7/site-packages/marshmallow/fields.py", line 635, in _serialize
    return ret[self._field_data_key]
TypeError: list indices must be integers or slices, not str
```
`requirements.txt`
```
attrs==19.2.0
marshmallow==3.2.1
```
