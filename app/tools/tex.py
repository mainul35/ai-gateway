"""Turning the LaTeX a model reaches for into the characters it meant.

Asked for an arrow, a model writes $\\rightarrow$, because that is what its training data does. The
playground has no maths renderer, so the reader gets the source code of an arrow instead of an arrow.

This is not a LaTeX engine and is not trying to be one. It knows a few hundred symbols that have a
Unicode character of their own, and it converts a span only when it can convert all of it. Anything
with a fraction, a sum, a matrix or a command it does not know is left exactly as written: showing
somebody raw LaTeX is a poor outcome, but showing them half-converted nonsense is a worse one.

Dollars are treated with suspicion. "$5 and $10" is not a maths span, so a span is only unwrapped
when its contents actually contain a backslash command that converted.
"""
import re

SYMBOLS = {
    # the arrows, which is what started this
    "rightarrow": "→", "to": "→", "longrightarrow": "⟶", "Rightarrow": "⇒",
    "implies": "⇒", "leftarrow": "←", "gets": "←", "longleftarrow": "⟵",
    "Leftarrow": "⇐", "leftrightarrow": "↔", "Leftrightarrow": "⇔",
    "iff": "⇔", "mapsto": "↦", "uparrow": "↑", "downarrow": "↓",
    # arithmetic and comparison
    "times": "×", "div": "÷", "pm": "±", "mp": "∓", "cdot": "·",
    "ast": "∗", "star": "⋆", "circ": "∘", "bullet": "•",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠",
    "ne": "≠", "approx": "≈", "equiv": "≡", "cong": "≅", "sim": "∼",
    "simeq": "≃", "propto": "∝", "ll": "≪", "gg": "≫",
    # sets and logic
    "in": "∈", "notin": "∉", "ni": "∋", "subset": "⊂", "subseteq": "⊆",
    "supset": "⊃", "supseteq": "⊇", "cup": "∪", "cap": "∩",
    "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃",
    "nexists": "∄", "neg": "¬", "lnot": "¬", "land": "∧", "wedge": "∧",
    "lor": "∨", "vee": "∨", "therefore": "∴", "because": "∵",
    # the rest of the furniture
    "infty": "∞", "partial": "∂", "nabla": "∇", "sum": "∑", "prod": "∏",
    "int": "∫", "iint": "∬", "oint": "∮", "sqrt": "√", "angle": "∠",
    "degree": "°", "prime": "′", "ldots": "…", "dots": "…", "cdots": "⋯",
    "vdots": "⋮", "langle": "⟨", "rangle": "⟩", "hbar": "ℏ", "ell": "ℓ",
    "Re": "ℜ", "Im": "ℑ", "aleph": "ℵ", "checkmark": "✓",
    # Greek, lower then upper
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ",
    "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ",
    "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ",
    "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "ϕ", "chi": "χ",
    "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ",
    "Omega": "Ω",
}

# Spacing and grouping commands that mean nothing here: dropped rather than converted
DROPPED = {"left", "right", "big", "Big", "bigg", "Bigg", "displaystyle", "textstyle", ",", ";",
           "!", ":", " ", "quad", "qquad"}
SPACED = {",", ";", ":", " ", "quad", "qquad"}

# A character no answer will contain, to stand in for a span left as written
HOLD = chr(1)

COMMAND = re.compile(r"\\([A-Za-z]+|[,;:! ])")
# A span is given up on if it has any of the structure this cannot represent
STRUCTURE = re.compile(r"[{}^_&\\]|\b(frac|begin|end|matrix|text|mathrm|mathbf)\b")

INLINE_DOLLAR = re.compile(r"(?<![\\$])\$(?!\s)([^$\n]{1,200}?)(?<!\s)\$(?!\d)")
DISPLAY_DOLLAR = re.compile(r"\$\$([^$]{1,400}?)\$\$", re.S)
PAREN = re.compile(r"\\\((.{1,200}?)\\\)", re.S)
BRACKET = re.compile(r"\\\[(.{1,400}?)\\\]", re.S)


def _convert(inner):
    """The span as plain characters, or None when any of it cannot be represented."""
    if "\\" not in inner:
        return None                      # nothing to convert; leave the dollars where they were

    def one(match):
        name = match.group(1)
        if name in SYMBOLS:
            return SYMBOLS[name]
        if name in DROPPED:
            return " " if name in SPACED else ""
        return "\x00"                    # a command with no character of its own: give up below

    converted = COMMAND.sub(one, inner)
    if "\x00" in converted or STRUCTURE.search(converted):
        return None
    return re.sub(r"\s{2,}", " ", converted).strip()


def unwrap(text):
    """Replaces the maths spans that are only symbols; leaves every other one alone."""
    if not text or "\\" not in text:
        return text

    # A span this cannot represent is put aside whole, not left in the text. The loose pass below
    # would otherwise reach inside it and turn $\sum_{i=1}^{n}$ into $∑_{i=1}^{n}$, which is the
    # half-converted nonsense this module exists to avoid.
    kept = []

    def span(match):
        converted = _convert(match.group(1))
        if converted is not None:
            return converted
        kept.append(match.group(0))
        return f"{HOLD}{len(kept) - 1}{HOLD}"

    for pattern in (DISPLAY_DOLLAR, BRACKET, PAREN, INLINE_DOLLAR):
        text = pattern.sub(span, text)

    # Models also write the command bare, with no span around it at all
    text = COMMAND.sub(lambda m: SYMBOLS.get(m.group(1), m.group(0)), text)
    return re.sub(HOLD + r"(\d+)" + HOLD, lambda m: kept[int(m.group(1))], text)
