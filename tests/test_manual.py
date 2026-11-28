"""The user manual has one source. The page the repository shows and the one-file manual are built from it, and
neither is edited by hand."""

import importlib.util
import re
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"
spec = importlib.util.spec_from_file_location("build_manual", DOCS / "build_manual.py")
build_manual = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_manual)


def test_both_manuals_are_what_the_source_builds():
	for path, text in build_manual.build().items():
		assert path.read_text() == text, f"{path.name}: run python docs/build_manual.py"


def test_every_picture_of_the_page_is_in_the_repository():
	page = build_manual.PAGE.read_text()
	pictures = re.findall(r"!\[[^\]]+\]\(([^)]+)\)", page)
	assert len(pictures) == 17 and len(set(pictures)) == 17
	assert all((DOCS / picture).is_file() for picture in pictures)
	# and the one-file manual holds them inside it, with no link to a file beside it
	assert "images/user-guide/" not in build_manual.FILE.read_text()


def test_every_link_inside_the_page_leads_to_one_of_its_headings():
	page = build_manual.PAGE.read_text()
	headings = {build_manual.slug(heading) for heading in re.findall(r"^#{1,3} (.+)$", page, re.M)}
	links = set(re.findall(r"\]\(#([^)]+)\)", page))
	assert links and links <= headings


def test_an_element_the_page_does_not_know_stops_the_build():
	source = build_manual.SOURCE.read_text().replace("</h2>", "</h2><aside>A note</aside>", 1)
	try:
		build_manual.page(source)
	except ValueError as refused:
		assert "aside" in str(refused)
	else:
		raise AssertionError("an unknown element was left out of the page without a word")
