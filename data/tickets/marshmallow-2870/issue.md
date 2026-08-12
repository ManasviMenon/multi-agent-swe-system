# `URL` validator treats custom schemes case-sensitively

`marshmallow.validate.URL` lowercases the scheme parsed from the input, but it stores the user-provided schemes iterable as-is. When a caller supplies uppercase or mixed-case schemes (e.g. ["A"]), validation always fails: the parsed scheme becomes "a", which isn’t found in ["A"], and the validator raises ValidationError: "Not a valid URL.".

```python
from marshmallow.validate import URL
from marshmallow import ValidationError

validator = URL(schemes=["A"])  # custom scheme in uppercase

validator("A://example.com")
```

```
Traceback (most recent call last):
  File "/data/src/test.py", line 6, in <module>
    validator("A://example.com")
  File "/home/hdd/miniconda3/envs/py312/lib/python3.12/site-packages/marshmallow/validate.py", line 209, in __call__
    raise ValidationError(message)
marshmallow.exceptions.ValidationError: Not a valid URL.
```

> Note: This issue was identified by an automated testing tool for academic research and manually verified. If you have any concerns about this type of reporting, please let me know, and I will adjust my workflow accordingly.
