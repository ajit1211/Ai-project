"""`calculator`: arithmetic with a parser of its own (docs/architecture/tools.md#calculator).

No `eval`, no `exec`, no Python AST: the expression is tokenized and parsed by a small
recursive-descent parser that only knows numbers, `+ - * / // % ^ **`, parentheses, unary
signs, the constants `pi` and `e`, and a fixed list of functions. Everything is bounded: the
length of the expression, the number of tokens, the nesting depth, the size of powers and
factor results, and the magnitude of every intermediate value. Results are floats, shown with
up to 15 significant digits (so 0.1 + 0.2 shows as 0.3).
"""

import math
import re
from collections.abc import Callable
from typing import Any

from nexa_runtime.tools.registry import ToolContext, ToolDefinition, ToolFailure, ToolOutput

MAX_EXPRESSION_CHARS = 200
MAX_TOKENS = 120
MAX_DEPTH = 24
MAX_MAGNITUDE = 1e300
MAX_EXPONENT = 1_000

_TOKEN = re.compile(
    r"\s*(?:(\d+(?:\.\d*)?(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?)|(\*\*|//|[-+*/%^(),])|([a-z][a-z0-9]*))"
)

_CONSTANTS = {"pi": math.pi, "e": math.e}


def _log(x: float, base: float = math.e) -> float:
    if x <= 0 or base <= 0 or base == 1:
        raise ToolFailure("math_error", "A logarithm needs a positive number (and base not 1).")
    return math.log(x, base)


def _sqrt(x: float) -> float:
    if x < 0:
        raise ToolFailure("math_error", "The square root of a negative number is not real.")
    return math.sqrt(x)


def _exp(x: float) -> float:
    if x > 690:
        raise ToolFailure("too_large", "The result is too large.")
    return math.exp(x)


def _round(x: float, digits: float = 0) -> float:
    if digits != int(digits) or not 0 <= digits <= 15:
        raise ToolFailure("invalid_expression", "round() keeps 0 to 15 decimal places.")
    return round(x, int(digits))


# name -> (function, smallest and largest number of arguments)
_FUNCTIONS: dict[str, tuple[Callable[..., float], int, int]] = {
    "sqrt": (_sqrt, 1, 1),
    "abs": (abs, 1, 1),
    "round": (_round, 1, 2),
    "floor": (lambda x: float(math.floor(x)), 1, 1),
    "ceil": (lambda x: float(math.ceil(x)), 1, 1),
    "min": (min, 1, 10),
    "max": (max, 1, 10),
    "ln": (_log, 1, 1),
    "log": (_log, 1, 2),
    "log10": (lambda x: _log(x, 10), 1, 1),
    "log2": (lambda x: _log(x, 2), 1, 1),
    "exp": (_exp, 1, 1),
    "sin": (math.sin, 1, 1),
    "cos": (math.cos, 1, 1),
    "tan": (math.tan, 1, 1),
}


def _invalid(reason: str) -> ToolFailure:
    return ToolFailure("invalid_expression", reason)


def tokenize(expression: str) -> list[str]:
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise _invalid(f"The expression is longer than {MAX_EXPRESSION_CHARS} characters.")
    text = expression.strip().lower()
    tokens: list[str] = []
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None or match.end() == position:
            raise _invalid(f"Unexpected character at position {position + 1}.")
        token = match.group(1) or match.group(2) or match.group(3)
        if token is None:  # only spaces were left
            break
        tokens.append(token)
        position = match.end()
        if len(tokens) > MAX_TOKENS:
            raise _invalid("The expression is too long.")
    if not tokens:
        raise _invalid("The expression is empty.")
    return tokens


class _Parser:
    """expression := term (("+" | "-") term)*
    term       := unary (("*" | "/" | "//" | "%") unary)*
    unary      := ("+" | "-") unary | power
    power      := atom (("^" | "**") unary)?          (right-associative)
    atom       := number | constant | function "(" expression ("," expression)* ")"
                | "(" expression ")"
    """

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.index = 0
        self.depth = 0

    def parse(self) -> float:
        value = self.expression()
        if self.index != len(self.tokens):
            raise _invalid(f"Unexpected '{self.tokens[self.index]}'.")
        return value

    def peek(self) -> str | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self, expected: str | None = None) -> str:
        token = self.peek()
        if token is None or (expected is not None and token != expected):
            raise _invalid(f"Expected '{expected}'." if expected else "The expression ends early.")
        self.index += 1
        return token

    def nested(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise _invalid("The expression is nested too deeply.")

    def expression(self) -> float:
        self.nested()
        value = self.term()
        while self.peek() in ("+", "-"):
            value = _checked(value + self.term() if self.take() == "+" else value - self.term())
        self.depth -= 1
        return value

    def term(self) -> float:
        value = self.unary()
        while self.peek() in ("*", "/", "//", "%"):
            operator = self.take()
            right = self.unary()
            if operator == "*":
                value = _checked(value * right)
            else:
                if right == 0:
                    raise ToolFailure("division_by_zero", "Division by zero.")
                if operator == "/":
                    value = _checked(value / right)
                elif operator == "//":
                    value = _checked(float(math.floor(value / right)))
                else:
                    value = _checked(
                        math.fmod(value, right) if value * right >= 0 else value % right
                    )
        return value

    def unary(self) -> float:
        if self.peek() in ("+", "-"):
            self.nested()
            sign = self.take()
            value = self.unary()
            self.depth -= 1
            return -value if sign == "-" else value
        return self.power()

    def power(self) -> float:
        base = self.atom()
        if self.peek() in ("^", "**"):
            self.take()
            self.nested()
            exponent = self.unary()
            self.depth -= 1
            return _power(base, exponent)
        return base

    def atom(self) -> float:
        symbol = self.take()
        if symbol == "(":
            value = self.expression()
            self.take(")")
            return value
        if symbol[0].isdigit() or symbol[0] == ".":
            return _checked(float(symbol))
        if symbol in _CONSTANTS:
            return _CONSTANTS[symbol]
        if symbol in _FUNCTIONS:
            function, smallest, largest = _FUNCTIONS[symbol]
            self.take("(")
            arguments = [self.expression()]
            while self.peek() == ",":
                self.take()
                arguments.append(self.expression())
            self.take(")")
            if not smallest <= len(arguments) <= largest:
                raise _invalid(f"{symbol}() takes {smallest} to {largest} arguments.")
            try:
                return _checked(float(function(*arguments)))
            except (ValueError, OverflowError) as exc:
                raise ToolFailure("math_error", f"{symbol}() is not defined there.") from exc
        raise _invalid(f"Unknown name or symbol '{symbol}'.")


def _power(base: float, exponent: float) -> float:
    if abs(exponent) > MAX_EXPONENT:
        raise ToolFailure("too_large", "The exponent is too large.")
    if base == 0 and exponent < 0:
        raise ToolFailure("division_by_zero", "Zero cannot be raised to a negative power.")
    if base < 0 and exponent != int(exponent):
        raise ToolFailure("math_error", "A negative number to a fractional power is not real.")
    if base != 0 and abs(exponent) * math.log10(abs(base)) > 300:
        raise ToolFailure("too_large", "The result is too large.")
    return _checked(math.pow(base, exponent))


def _checked(value: float) -> float:
    if math.isnan(value):
        raise ToolFailure("math_error", "The result is not a number.")
    if math.isinf(value) or abs(value) > MAX_MAGNITUDE:
        raise ToolFailure("too_large", "The result is too large.")
    return value


def evaluate(expression: str) -> float:
    return _Parser(tokenize(expression)).parse()


def formatted(value: float) -> str:
    text = format(value, ".15g")
    return "0" if text == "-0" else text


async def calculate(context: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    expression = arguments["expression"]
    value = evaluate(expression)
    text = formatted(value)
    return ToolOutput(
        data={
            "expression": " ".join(expression.split()),
            "result": float(text),
            "result_text": text,
        }
    )


CALCULATOR = ToolDefinition(
    id="calculator",
    name="Calculator",
    description=(
        "Evaluate an arithmetic expression exactly as written. Supports + - * / // % ^, "
        "parentheses, pi, e, and sqrt, abs, round, floor, ceil, min, max, ln, log, log10, "
        "log2, exp, sin, cos, tan (radians). Use it for any calculation instead of "
        "computing in your head."
    ),
    version="1",
    category="math",
    input_schema={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_EXPRESSION_CHARS,
                "description": "For example: (12.5 * 4) / 3 + sqrt(16)",
            }
        },
        "required": ["expression"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "expression": {"type": "string"},
            "result": {"type": "number"},
            "result_text": {"type": "string"},
        },
        "required": ["expression", "result", "result_text"],
        "additionalProperties": False,
    },
    handler=calculate,
    risk="low",
    timeout_seconds=2.0,
    max_input_bytes=512,
    max_output_bytes=1_024,
    describe=lambda arguments: f"Calculating {arguments.get('expression', '')}"[:120],
)
