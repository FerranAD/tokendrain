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

Content pages use relative links and can be served at a domain root or under a path prefix. `site_url` defaults to `https://ferranad.github.io/tokendrain/`. Override it with `TOKENDRAIN_DOCS_SITE_URL` for another host or path so canonical URLs, the sitemap, and 404-page asset paths match the deployment. The Pages workflow sets this from GitHub's configured site metadata, including custom domains.

## Publish to GitHub Pages

The [Documentation workflow](https://github.com/FerranAD/tokendrain/actions/workflows/docs-pages.yml) builds and publishes the website using GitHub's Pages artifact deployment actions.

For the first deployment, a repository administrator must select **Settings → Pages → Build and deployment → Source → GitHub Actions**. If the `github-pages` environment has deployment protection rules, allow deployments from `main`. This one-time repository setup is required before the workflow can publish; the workflow uses the repository's standard `GITHUB_TOKEN` and does not need a personal access token.

- Pull requests affecting documentation or its build inputs run the strict build without publishing.
- Pushes to `main` affecting `docs/`, `mkdocs.yml`, the README, frontend/API docs, locked dependencies, or the workflow build and publish the website.
- To publish again without changing source files, open the **Documentation** workflow in Actions, choose **Run workflow**, and select `main`. Manual runs on other branches only build.

The build installs only the locked documentation dependency group with Python 3.12. Publishing is a separate job with Pages write and OIDC permissions; it deploys the artifact from the successful build through the `github-pages` environment. Deployments are serialized, and workflow actions are pinned to commit SHAs.

After merging a documentation change, check the workflow's **Publish documentation** job for the deployed URL. The default repository URL is `https://ferranad.github.io/tokendrain/`; a configured custom domain takes precedence. A strict-build failure leaves the existing published site in place. A failure at **Read GitHub Pages URL** usually means Pages has not been configured to use Actions or its settings are unavailable to the workflow.

## Edit the content

- `docs/index.md` is the landing page. `docs/workflow.md` explains projects and Runs.
- Existing guides under `docs/` are the source for authentication, GitHub, security, storage, architecture, and development pages.
- `docs/site_hooks.py` generates installation, usage limits, and operations pages from the current README, and frontend/API pages from `web/README.md` and `web/API.md`. Edit those source files to update the generated pages. The hook also adapts repository-relative links for the site.
- `mkdocs.yml` defines navigation and Markdown extensions. `docs/stylesheets/site.css` adds the tokendrain colors and landing-page layout.
- Internal task logs and `RESUME.md` are excluded from the public site. Build hooks and theme templates are also excluded from its output.

Run the strict build before committing. It checks documentation links and rejects build warnings. Refer to [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/) for authoring features and [MkDocs configuration](https://www.mkdocs.org/user-guide/configuration/) for hosting options.
