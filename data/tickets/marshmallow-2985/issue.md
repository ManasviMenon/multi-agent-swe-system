# Question: Should `fields.Enum()` accept `None` when an enum member has value `None`?

I’d like to clarify whether the following behavior is intentional or whether Marshmallow would consider supporting it.

#### Minimal example
```
import marshmallow as ma
from marshmallow import Schema, fields
from enum import Enum

class MyEnum(Enum):
    A = 'a'
    NONE = None

class S(Schema):
    sentinel = fields.Enum(MyEnum, by_value=True)

data = {'sentinel': None}
result = S().load(data)
```

Currently, the execution of this example raises: `ValidationError: {'sentinel': ['Field may not be null.']}`.

However, in Python, enum members can have `None` as values. From that perspective, it might be expected that `None` would be treated as part of the field's value domain when using `by_value=True`. According to the documentation, if this parameter is set to `True`, it delegates deserialization to `fields.Raw`, which has its own mechanism to deal with None values before the enum value resolution occurs.

We noticed that explicitly adding `allow_none=True` to the field allows the example to run successfully . However, this behavior is not documented and might not respect other design decisions. 

Would this be considered this a valid use case to support natively without requiring an explicit `allow_none=True`?

Thank you in advance for any clarification :) 

Similar to #2868

#### Tool Disclosure
This observation was raised by an automated testing tool currently being developed by our research team. Before submitting this issue, we tested the example in the last development version of marshmallow and reviewed the related documentation. 
