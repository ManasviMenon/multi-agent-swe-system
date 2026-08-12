# And validator

Hello everyone!
I would like to add a new validator to combine multiple validators into one. Simple example:

```py
def calculate_age(birthday: date) -> int:
    today = date.today()
    offset = (today.month, today.day) < (birthday.month, birthday.day)
    return today.year - birthday.year - offset

def validate_not_future_date(value: date) -> None:
    if value > date.today():
        raise ValidationError("Not future date.")

def validate_14_years_old(value: date) -> None:
    if calculate_age(value) < 14:
        raise ValidationError("Must be 14 years old.")

class PassportSchema(Schema):
    issued_at = fields.Date(
        "%Y-%m-%d",
        validate=[
            validate_not_future_date,
            validate_14_years_old,
        ],
    )

    @post_load
    def make_passport(self, data: dict, **kwargs) -> Passport:
        return Passport(**data)

@attr.s(auto_attribs=True, slots=True, frozen=True)
class Passport:
    issued_at: date

    def __attrs_post_init__(self) -> None:
        validate_not_future_date(self.issued_at)
        validate_14_years_old(self.issued_at)
```

In this example, I am forced to pass validators as a list in the `issued_at` field (in `PassportSchema`) and call two functions to validate a single value in `Passport` entity.
I would like to combine the two functions into one and turn them into a single validator:

```py
T = TypeVar("T")

Validator = Callable[[T], T]

@attr.s(auto_attribs=True, slots=True, frozen=True)
class _AndValidator:
    validators: Sequence[Validator]

    def __call__(self, value: T) -> T:
        for validator in self.validators:
            validator(value)
        return value

def and_validator(*validators) -> _AndValidator:
    return _AndValidator(validators)

validate_passport_issued_at = and_validator(
    validate_14_years_old,
    validate_not_future_date,
)
```

Then the example above will look much more concise:

```py
class PassportSchema(Schema):
    issued_at = fields.Date("%Y-%m-%d", validate=validate_passport_issued_at)

    @post_load
    def make_passport(self, data: dict, **kwargs) -> Passport:
        return Passport(**data)

@attr.s(auto_attribs=True, slots=True, frozen=True)
class Passport:
    issued_at: date

    def __attrs_post_init__(self) -> None:
        validate_passport_issued_at(self.issued_at)
```

I would like to add such functionality to the `marshmallow.validate` package.
