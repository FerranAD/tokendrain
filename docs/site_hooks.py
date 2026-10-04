"""Publish existing source docs without copying instructions into a second tree."""

from pathlib import Path

from mkdocs.config.defaults import MkDocsConfig
from mkdocs.structure.files import File, Files

ROOT = Path(__file__).resolve().parents[1]


def section(markdown: str, heading: str, until: str) -> str:
    """Fail the build if README headings change, rather than silently publishing stale docs."""
    start = markdown.index(heading) + len(heading)
    end = markdown.index(until, start)
    return markdown[start:end]


def site_links(markdown: str) -> str:
    return (
        markdown.replace("(docs/", "(")
        .replace("(../docs/", "(")
        .replace("(../web/API.md", "(api.md")
        .replace("(../web/README.md", "(frontend.md")
        .replace("(API.md", "(api.md")
    )


def on_files(files: Files, config: MkDocsConfig) -> Files:
    readme = (ROOT / "README.md").read_text()
    generated = {
        "getting-started.md": "# Install & first Run\n\n"
        + section(readme, "## Install on NixOS", "## The VM is the security boundary"),
        "usage-limits.md": "# Usage limits & stopping\n\n"
        + section(readme, "## Usage limits", "## Install on NixOS"),
        "operations.md": "# Operations\n\n" + section(readme, "## Operations", "## Under the hood"),
        "api.md": (ROOT / "web/API.md").read_text(),
        "frontend.md": (ROOT / "web/README.md").read_text(),
    }
    for name, markdown in generated.items():
        files.append(File.generated(config, name, content=site_links(markdown)))
    # Build infrastructure and theme templates are not public documentation assets.
    for file in list(files):
        if file.src_uri == "site_hooks.py" or file.src_uri.startswith("theme/"):
            files.remove(file)
    if (ROOT / "docs/notifications.md").exists():
        for item in config.nav:
            if "Connect" in item and {"ntfy reminders": "notifications.md"} not in item["Connect"]:
                item["Connect"].append({"ntfy reminders": "notifications.md"})
    return files


def on_page_markdown(markdown: str, **kwargs: object) -> str:
    return site_links(markdown)
