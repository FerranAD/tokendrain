# Documentation website

The documentation lives in this repository and builds into a static website with Material for MkDocs. It includes search, light/dark themes, navigation, code-copy buttons, and layouts for phones and desktops. Fonts and site assets are served locally; there are no analytics or external font requests.

## Preview locally

From the repository root:

```sh
nix develop
uv sync --locked --group docs
uv run --group docs mkdocs serve --dev-addr 127.0.0.1:8000
```

Open `http://127.0.0.1:8000`. The preview rebuilds when documentation changes. When editing generated pages, restart the preview after changing `README.md` or files under `web/`.

## Build for hosting

```sh
uv run --locked --group docs mkdocs build --strict
```

The output is `site/`, which is ignored by Git. Serve it with a static web server. For a local check of the built output:

```sh
python3 -m http.server 8000 --bind 127.0.0.1 --directory site
```

Content pages use relative links and can be served at a domain root or under a path prefix. For a published deployment, set `site_url` in `mkdocs.yml` to the real public URL (including its prefix) so canonical URLs, the sitemap, and 404-page asset paths match the host. The repository does not assume a published URL or deploy the website automatically.

## Edit the content

- `docs/index.md` is the landing page. `docs/workflow.md` explains projects and Runs.
- Existing guides under `docs/` are the source for authentication, GitHub, security, storage, architecture, and development pages.
- `docs/site_hooks.py` generates installation, usage limits, and operations pages from the current README, and frontend/API pages from `web/README.md` and `web/API.md`. Edit those source files to update the generated pages. The hook also adapts repository-relative links for the site.
- `mkdocs.yml` defines navigation and Markdown extensions. `docs/stylesheets/site.css` adds the tokendrain colors and landing-page layout.
- Internal task logs and `RESUME.md` are excluded from the public site. Build hooks and theme templates are also excluded from its output.

Run the strict build before committing. It checks documentation links and rejects build warnings. Refer to [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/) for authoring features and [MkDocs configuration](https://www.mkdocs.org/user-guide/configuration/) for hosting options.
