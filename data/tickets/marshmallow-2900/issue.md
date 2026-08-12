# `fields.Constant` with `required=True` flag raised `load_default` warning

I have a schema containing a `Constant`, with `required=True` set, as the schema is ultimately exported to an OpenAPI spec, and the field must be marked as `required` there.

The fix from #2894 now triggers the validation failure in https://github.com/marshmallow-code/marshmallow/blob/2a3812d5049c83e98db60a0869919521f97cd77d/src/marshmallow/fields.py#L216

My feeling on the fix is to undo the changes from #2894 and instead reproduce the `allow_none` flag initialization in `Constant.__init__`

Happy to create a PR if you'd be happy with this fix.
