# Empty string data_key disallowed

Having an empty string as `data_key` doesn't seem to work.

```py
import marshmallow
from marshmallow import fields


class RootSchema(marshmallow.Schema):
    x = fields.Raw(data_key="")


RootSchema().load({'': 1})

```
```py

---> 14 RootSchema().load({'': 1})

~/.pyenv/versions/3.7.3/lib/python3.7/site-packages/marshmallow/schema.py in load(self, data, many, partial, unknown)
    730         """
    731         return self._do_load(
--> 732             data, many=many, partial=partial, unknown=unknown, postprocess=True
    733         )
    734 

~/.pyenv/versions/3.7.3/lib/python3.7/site-packages/marshmallow/schema.py in _do_load(self, data, many, partial, unknown, postprocess)
    892             exc = ValidationError(errors, data=data, valid_data=result)
    893             self.handle_error(exc, data)
--> 894             raise exc
    895 
    896         return result

ValidationError: {'': ['Unknown field.']}
```

marshmallow-3.0.2
