"""Checks that the narrow-screen rules actually win.

A media query does not raise specificity. A rule inside @media (max-width: 860px) that says the same
thing as a rule outside it, at the same specificity, loses if the plain rule is written later - and
loses silently, which is how a whole mobile layout can be dead code and still look like it is there.
"""
import pathlib
import re
import sys


def specificity(selector):
    selector = re.sub(r"::?[a-z-]+(\([^)]*\))?", "", selector)     # pseudo-elements/classes
    ids = len(re.findall(r"#[\w-]+", selector))
    classes = len(re.findall(r"\.[\w-]+|\[[^\]]+\]", selector))
    elements = len(re.findall(r"(?:^|[\s>+~])([a-z][\w-]*)", selector))
    return ids, classes, elements


# A shorthand and its longhands are the same declaration as far as the cascade is concerned, and
# "flex: 1" written later quietly beating "flex-basis: 100%" is precisely the bug this check exists
# for, so the two have to be compared as the same property.
SHORTHANDS = {
    "flex": {"flex-grow", "flex-shrink", "flex-basis"},
    "flex-flow": {"flex-direction", "flex-wrap"},
    "padding": {"padding-top", "padding-right", "padding-bottom", "padding-left"},
    "margin": {"margin-top", "margin-right", "margin-bottom", "margin-left"},
    "overflow": {"overflow-x", "overflow-y"},
    "font": {"font-size", "font-family", "font-weight", "line-height"},
    "background": {"background-color", "background-image"},
    "inset": {"top", "right", "bottom", "left"},
}


def expand(properties):
    """A set of property names, with every shorthand standing in for what it also sets."""
    wide = set()
    for name in properties:
        wide.add(name)
        wide |= SHORTHANDS.get(name, set())
    return wide


def rules(css):
    """Every (selector, properties, position, inside_media) in source order."""
    found = []
    depth_media = 0
    position = 0
    i = 0
    while i < len(css):
        at = css.find("{", i)
        if at < 0:
            break
        head = css[i:at].strip().rsplit("}", 1)[-1].strip()
        if head.startswith("@media"):
            depth_media += 1
            i = at + 1
            continue
        close = css.find("}", at)
        if close < 0:
            break
        body = css[at + 1:close]
        for selector in head.split(","):
            selector = selector.strip()
            if selector:
                properties = expand({d.split(":", 1)[0].strip()
                                     for d in body.split(";") if ":" in d})
                found.append((selector, properties, position, depth_media > 0))
                position += 1
        i = close + 1
        while depth_media and css[i:].lstrip().startswith("}"):
            i = css.index("}", i) + 1
            depth_media -= 1
    return found


page = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", page, re.S))
all_rules = rules(css)
problems = []
for selector, properties, position, in_media in all_rules:
    if not in_media:
        continue
    for other_selector, other_properties, other_position, other_media in all_rules:
        if other_media or other_position < position:
            continue
        if other_selector != selector:
            continue
        clash = properties & other_properties
        if clash and specificity(other_selector) >= specificity(selector):
            problems.append(f"{selector}: {', '.join(sorted(clash))} is overridden by the plain rule "
                            f"written later")

print(f"{len(all_rules)} rules read from {sys.argv[1]}")
for problem in sorted(set(problems)):
    print("  LOSES:", problem)
print("every narrow-screen rule wins" if not problems else f"{len(set(problems))} rule(s) do nothing")
sys.exit(1 if problems else 0)
