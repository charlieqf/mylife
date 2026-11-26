"""Before a deploy (deploy/deploy.sh runs it): every rule in prototype.css's @media (prefers-color-scheme: dark) blocks
has a twin for browsers that force dark pages (.autodark, set by the page's first script) with the same declarations.
The page no longer rewrites the dark media rules at run time: opened from a file, it cannot read an external sheet."""
import os
import re
import sys

CSS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prototype", "prototype.css")


def rules(text):
    """[(selector, declarations)] of plain rules in a block of CSS (comments removed, no nesting)."""
    return [(" ".join(m.group(1).split()), " ".join(m.group(2).split()))
            for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", text)]


def main():
    css = re.sub(r"/\*.*?\*/", "", open(CSS, encoding="utf-8").read(), flags=re.S)
    dark, top, i = {}, {}, 0
    for m in re.finditer(r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{((?:[^{}]*\{[^{}]*\})*[^{}]*)\}", css):
        dark.update(rules(m.group(1)))
        top.update(rules(css[i:m.start()]))
        i = m.end()
    top.update(rules(css[i:]))
    twin = lambda sel: "html.autodark" if sel == ":root" else ".autodark " + sel
    bad = [sel for sel, decl in dark.items() if top.get(twin(sel)) != decl]
    for sel in bad:
        print("prototype.css: %s in the dark @media has no identical %s rule (forced dark pages would differ)" % (sel, twin(sel)))
    if not dark:
        print("prototype.css: no @media (prefers-color-scheme: dark) rules found")
    return 1 if bad or not dark else 0


if __name__ == "__main__":
    sys.exit(main())
