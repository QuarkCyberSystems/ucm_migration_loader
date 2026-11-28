"""Build the user manual from its one source, `docs/user-manual.src.html`:

	python docs/build_manual.py            write both
	python docs/build_manual.py --check    say which is out of date, and write nothing

- `docs/user-manual.html`: the manual in one file, with its pictures inside it, to read without the repository.
- `docs/user-manual.md`: the same manual as a page a repository host shows, with the pictures of
  `docs/images/user-guide/` beside it.

Edit the source, never the two built files. The source keeps to a few elements (headings, paragraphs, lists,
tables, code blocks, figures), and the page is made from exactly those: an element this script does not know
stops the build.
"""

import base64
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

DOCS = Path(__file__).resolve().parent
SOURCE, PAGE, FILE = DOCS / "user-manual.src.html", DOCS / "user-manual.md", DOCS / "user-manual.html"
PICTURE = re.compile(r'src="(images/user-guide/[a-z-]+\.png)"')
VOID = ("img", "br", "meta", "link")


class Node:
	def __init__(self, tag, attrs=None):
		self.tag, self.attrs, self.children = tag, dict(attrs or ()), []

	def find(self, tag):
		for child in self.children:
			if isinstance(child, Node):
				if child.tag == tag:
					yield child
				yield from child.find(tag)

	def text(self) -> str:
		return "".join(child if isinstance(child, str) else child.text() for child in self.children)


class Tree(HTMLParser):
	def __init__(self):
		super().__init__(convert_charrefs=True)
		self.root = Node("root")
		self.open = [self.root]

	def handle_starttag(self, tag, attrs):
		node = Node(tag, attrs)
		self.open[-1].children.append(node)
		if tag not in VOID:
			self.open.append(node)

	def handle_endtag(self, tag):
		if tag not in VOID:
			closed = self.open.pop()
			if closed.tag != tag:
				raise ValueError(f"<{closed.tag}> is closed by </{tag}>")

	def handle_data(self, data):
		self.open[-1].children.append(data)


def slug(heading: str) -> str:
	"""The anchor a repository host gives a heading."""
	kept = re.sub(r"[^\w\- ]", "", heading.strip().lower())
	return kept.replace(" ", "-")


def inline(node, anchors: dict, cell: bool = False) -> str:
	"""The text of a paragraph, an item or a cell, with what is bold, stressed, code or a link."""
	out = []
	for child in node.children:
		if isinstance(child, str):
			text = re.sub(r"\s+", " ", child)
			out.append(text.replace("|", "\\|") if cell else text)
		elif child.tag == "strong":
			out.append(f"**{inline(child, anchors, cell).strip()}**")
		elif child.tag == "em":
			out.append(f"*{inline(child, anchors, cell).strip()}*")
		elif child.tag == "code":
			out.append(f"`{child.text()}`")
		elif child.tag == "a":
			target = child.attrs["href"]
			if target.startswith("#"):
				target = "#" + anchors[target[1:]]
			out.append(f"[{inline(child, anchors, cell).strip()}]({target})")
		else:
			raise ValueError(f"<{child.tag}> inside a line of text")
	return "".join(out).strip()


def fenced(node) -> str:
	text = node.text().strip("\n")
	first = text.lstrip()
	kind = "text"
	if first.startswith("{"):
		kind = "json"
	elif re.match(r"(ucm-push|bench|git|python3?|cd|\.) ", first) and "Sealed " not in first:
		kind = "sh"
	elif re.match(r"(#|\w+ = )", first):
		kind = "toml"
	return f"```{kind}\n{text}\n```"


def table(node, anchors: dict) -> str:
	rows = [list(row.children) for row in node.find("tr")]
	lines = []
	for number, row in enumerate(rows):
		cells = []
		for cell in (c for c in row if isinstance(c, Node)):
			said = inline(cell, anchors, cell=True)
			if number and cell.tag == "th" and "**" not in said and "`" not in said:
				said = f"**{said}**"
			cells.append(said)
		lines.append("| " + " | ".join(cells) + " |")
		if not number:
			lines.append("|" + "---|" * len(cells))
	return "\n".join(lines)


def items(node, anchors: dict) -> str:
	first = int(node.attrs.get("start", 1))
	lines = []
	for number, item in enumerate(node.find("li")):
		mark = f"{first + number}." if node.tag == "ol" else "-"
		lines.append(f"{mark} {inline(item, anchors)}")
	return "\n".join(lines)


def block(node, anchors: dict) -> str | None:
	kind = node.attrs.get("class")
	if node.tag in ("h2", "h3"):
		return "#" * int(node.tag[1]) + " " + inline(node, anchors)
	if node.tag == "p":
		said = inline(node, anchors)
		if kind in ("who", "subtitle"):
			return f"*{said}*"
		return "> " + said if kind == "check" else said
	if node.tag in ("ol", "ul"):
		return items(node, anchors)
	if node.tag == "table":
		return table(node, anchors)
	if node.tag == "pre":
		return fenced(node)
	if node.tag == "figure":
		(picture,) = node.find("img")
		(caption,) = node.find("figcaption")
		return f"![{picture.attrs['alt']}]({picture.attrs['src']})\n\n*{inline(caption, anchors)}*"
	raise ValueError(f"<{node.tag}> is not an element the manual's page knows")


def page(source: str) -> str:
	"""The manual as a Markdown page."""
	tree = Tree()
	tree.feed(source[source.index("<body") : source.index("</body>") + len("</body>")])
	(main,) = tree.root.find("main")
	sections = [child for child in main.children if isinstance(child, Node)]
	anchors = {section.attrs["id"]: slug(next(section.find("h2")).text()) for section in sections}
	(title,) = main.find("h1")
	(subtitle,) = (p for p in main.find("p") if p.attrs.get("class") == "subtitle")
	out = [
		"<!-- Built from user-manual.src.html by docs/build_manual.py. Edit the source, then build. -->",
		f"# {inline(title, anchors)}",
		block(subtitle, anchors),
		"The same manual in one file, to read without the repository: [user-manual.html](user-manual.html).",
		"\n".join(f"- [{next(s.find('h2')).text()}](#{anchors[s.attrs['id']]})" for s in sections),
	]
	for section in sections:
		for child in section.children:
			if isinstance(child, str):
				if child.strip():
					raise ValueError(f"Text outside an element: {child.strip()[:40]}")
			elif child.tag != "header":
				out.append(block(child, anchors))
	return "\n\n".join(out) + "\n"


def one_file(source: str) -> str:
	"""The manual as one HTML file, each picture inside it."""

	def inside(match) -> str:
		picture = base64.b64encode((DOCS / match.group(1)).read_bytes()).decode()
		return f'src="data:image/png;base64,{picture}"'

	return PICTURE.sub(inside, source)


def build() -> dict:
	source = SOURCE.read_text()
	return {PAGE: page(source), FILE: one_file(source)}


def main(argv: list[str]) -> int:
	stale = [path for path, text in build().items() if not path.is_file() or path.read_text() != text]
	if "--check" in argv:
		for path in stale:
			print(f"{path.name} is not what its source builds: run python docs/build_manual.py")
		return 1 if stale else 0
	for path, text in build().items():
		path.write_text(text)
	print("Built", ", ".join(path.name for path in build()))
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
