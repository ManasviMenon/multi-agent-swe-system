# `fields.Constant` rejects `None` constant values during load

When that constant is `None`, loading any payload that contains null triggers `ValidationError: Field may not be null`. The stems from the initialization order: `Field.__init__` sets `allow_none` based on the (yet-unknown) `load_default`, and only afterwards `Constant.__init__` assigns the actual constant/load_default.

```python
from marshmallow import Schema, fields

class DemoSchema(Schema):
    # Should always load/dump None, independent of input
    sentinel = fields.Constant(None)

schema = DemoSchema()

assert schema.dump({"sentinel": "anything"})["sentinel"] is None

schema.load({"sentinel": None})
```

```
Traceback (most recent call last):
  File "/data/src/test.py", line 11, in <module>
    schema.load({"sentinel": None})
  File "/home/hdd/miniconda3/envs/py312/lib/python3.12/site-packages/marshmallow/schema.py", line 730, in load
    return self._do_load(
           ^^^^^^^^^^^^^^
  File "/home/hdd/miniconda3/envs/py312/lib/python3.12/site-packages/marshmallow/schema.py", line 938, in _do_load
    raise exc
marshmallow.exceptions.ValidationError: {'sentinel': ['Field may not be null.']}
```

> Note: This issue was identified by an automated testing tool for academic research and manually verified. If you have any concerns about this type of reporting, please let me know, and I will adjust my workflow accordingly.
