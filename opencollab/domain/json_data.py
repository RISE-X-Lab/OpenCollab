import json
import math
from typing import Any


def _finite_float(token: str) -> float:
    number = float(token)
    if not math.isfinite(number):
        raise json.JSONDecodeError("JSON numbers must be finite", token, 0)
    return number


def loads_finite_json(text: str) -> Any:
    return json.loads(text, parse_float=_finite_float, parse_constant=_finite_float)
