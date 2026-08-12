# Make fields.Number abstract

Does it make sense to make `fields.Number` abstract?

The reason I ask is because I just had to do an upgrade from 2.15.4 to 4.2.2 and most of the errors were very quick to work through because the error information was very accurate to the problem (thank you for that).

But I had two fields on a custom `Schema` that were `fields.Number`. The error message that came back was `AttributeError: 'Number' object has no attribute 'num_type'.` It happens in the `_format_num` function since `num_type` is set on the subclasses (like `IntegerField`) but not on the `Number` base class.

Since this is a class that isn't meant to be used, would it be better to make it not instantiate-able? It could be an `abc.ABC` with a very simple `@abstractmethod` on the `__init__` method or you could raise an error in `__new__`, or however else one typically does this.

Then, for example, I could have seen `Can't instantiate abstract class B...` or something of the like. I would then know that the class is meant to be abstract without having to rely on the docstring.

This is obviously not a big deal, but I typically don't like relying on docstrings to decide implementation. Though I prefer the docstring over having to read the entire implementation and assuming the intent.

## Example

On a Django model:

```python
from django.db import models

from marshmallow import EXCLUDE, Schema, fields, validate

class TimeframeSchema(Schema):
    type = fields.Str(required=True)
    duration = fields.Number(required=True, validate=validate.Range(min=0))

    class Meta:
        unknown = EXCLUDE

def validate_timeframe(timeframe):
    TimeframeSchema().load(timeframe)

class AModel(models.Model):
    timeframe = models.JSONField(validators=[validate_timeframe])

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
```

Then in a test file:

```python
from model_bakery import baker
from app.models import AModel

@python.mark.django_db
def test__some_test:
    a_model = baker.make(AModel, timeframe={"type": "annual", "duration": 12})
    assert a_model
```

Then running `pytest my_test_file`:

```
tests/apps/app/models/my_test_file.py:6: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
/opt/venv/lib/python3.10/site-packages/model_bakery/baker.py:133: in make
    return baker.make(
/opt/venv/lib/python3.10/site-packages/model_bakery/baker.py:389: in make
    return self._make(**params)
/opt/venv/lib/python3.10/site-packages/model_bakery/baker.py:470: in _make
    instance = self.instance(
/opt/venv/lib/python3.10/site-packages/model_bakery/baker.py:504: in instance
    instance.save(**_save_kwargs)
the_app/apps/app/models/a_model.py:18: in save
    self.full_clean()
/opt/venv/lib/python3.10/site-packages/django/db/models/base.py:1470: in full_clean
    self.clean_fields(exclude=exclude)
/opt/venv/lib/python3.10/site-packages/django/db/models/base.py:1522: in clean_fields
    setattr(self, f.attname, f.clean(raw_value, self))
/opt/venv/lib/python3.10/site-packages/django/db/models/fields/__init__.py:778: in clean
    self.run_validators(value)
/opt/venv/lib/python3.10/site-packages/django/db/models/fields/__init__.py:730: in run_validators
    v(value)
the_app/apps/app/models/a_model.py:13: in validate_timeframe
    TimeframeSchema().load(timeframe)
/opt/venv/lib/python3.10/site-packages/marshmallow/schema.py:730: in load
    return self._do_load(
/opt/venv/lib/python3.10/site-packages/marshmallow/schema.py:887: in _do_load
    result = self._deserialize(
/opt/venv/lib/python3.10/site-packages/marshmallow/schema.py:676: in _deserialize
    value = self._call_and_store(
/opt/venv/lib/python3.10/site-packages/marshmallow/schema.py:517: in _call_and_store
    value = getter_func(data)
/opt/venv/lib/python3.10/site-packages/marshmallow/schema.py:669: in getter
    return field_obj.deserialize(
/opt/venv/lib/python3.10/site-packages/marshmallow/fields.py:374: in deserialize
    output = self._deserialize(value, attr, data, **kwargs)
/opt/venv/lib/python3.10/site-packages/marshmallow/fields.py:960: in _deserialize
    return self._validated(value)
/opt/venv/lib/python3.10/site-packages/marshmallow/fields.py:943: in _validated
    return self._format_num(value)
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

self = <fields.Number(dump_default=<marshmallow.missing>, attribute=None, validate=<Range(min=0, max=None, min_inclusive=True... be null.', 'validator_failed': 'Invalid value.', 'invalid': 'Not a valid number.', 'too_large': 'Number too large.'})>
value = 12

    def _format_num(self, value) -> _NumT:
        """Return the number value for value, given this field's `num_type`."""
>       return self.num_type(value)  # type: ignore[call-arg]
E       AttributeError: 'Number' object has no attribute 'num_type'

/opt/venv/lib/python3.10/site-packages/marshmallow/fields.py:935: AttributeError
```

