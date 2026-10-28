"""Check the static docs pages: local links resolve, anchors exist, tags balance."""
import os
import re
import sys
from html.parser import HTMLParser

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "website")
PAGES = ["index.html", "quickstart.html", "docs.html", "developer.html"]

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}


class Balance(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.problems = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append((tag, self.getpos()))

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack:
            self.problems.append(f"</{tag}> with nothing open at {self.getpos()}")
            return
        open_tag, pos = self.stack.pop()
        if open_tag != tag:
            self.problems.append(f"</{tag}> closes <{open_tag}> opened at {pos}, at {self.getpos()}")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def main():
    ids = {}
    texts = {}
    for page in PAGES:
        path = os.path.join(ROOT, page)
        if not os.path.exists(path):
            print(f"MISSING PAGE {page}")
            continue
        texts[page] = read(path)
        ids[page] = set(re.findall(r'\bid="([^"]+)"', texts[page]))

    problems = []
    for page, text in texts.items():
        # local links
        for href in re.findall(r'href="([^"]+)"', text):
            if href.startswith(("http://", "https://", "mailto:", "#", "data:")):
                if href.startswith("#") and href[1:] and href[1:] not in ids[page]:
                    problems.append(f"{page}: anchor {href} has no target")
                continue
            target, _, frag = href.partition("#")
            if not target:
                continue
            # "/favicon.svg" is site-root-relative: the site is served from website/, so that is
            # website/favicon.svg. A plain join would resolve it against the drive root.
            base = ROOT if target.startswith("/") else os.path.dirname(os.path.join(ROOT, page))
            target_path = os.path.normpath(os.path.join(base, target.lstrip("/")))
            if not os.path.exists(target_path):
                problems.append(f"{page}: {href} -> missing file")
                continue
            if frag:
                other = os.path.basename(target_path)
                if other in ids and frag not in ids[other]:
                    problems.append(f"{page}: {href} -> no id {frag} in {other}")

        # tags balance
        b = Balance()
        b.feed(text)
        for p in b.problems:
            problems.append(f"{page}: {p}")
        if b.stack:
            problems.append(f"{page}: unclosed {[t for t, _ in b.stack]}")

        # duplicate ids
        seen, dupes = set(), set()
        for i in re.findall(r'\bid="([^"]+)"', text):
            (dupes if i in seen else seen).add(i)
        if dupes:
            problems.append(f"{page}: duplicate ids {sorted(dupes)}")

    # CSS classes used in the pages but never defined
    css = read(os.path.join(ROOT, "docs.css"))
    defined = set(re.findall(r'\.([A-Za-z][\w-]*)', css))
    for page in ("quickstart.html", "docs.html", "developer.html"):
        used = set()
        for m in re.findall(r'class="([^"]+)"', texts[page]):
            used.update(m.split())
        unknown = sorted(u for u in used if u not in defined)
        if unknown:
            problems.append(f"{page}: classes not in docs.css: {unknown}")

    if problems:
        print("\n".join(problems))
        print(f"\n{len(problems)} problem(s)")
        return 1
    print(f"OK: {len(texts)} pages, links, anchors, tags and classes all check out")
    return 0


if __name__ == "__main__":
    sys.exit(main())
