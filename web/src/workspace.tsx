import { Icon } from './icons';
import { useEffect, useMemo, useState } from 'react';
import DOMPurify from 'dompurify';
import { marked } from 'marked';
import hljs from 'highlight.js/lib/core';
import javascript from 'highlight.js/lib/languages/javascript';
import typescript from 'highlight.js/lib/languages/typescript';
import python from 'highlight.js/lib/languages/python';
import json from 'highlight.js/lib/languages/json';
import yaml from 'highlight.js/lib/languages/yaml';
import ini from 'highlight.js/lib/languages/ini';
import nix from 'highlight.js/lib/languages/nix';
import bash from 'highlight.js/lib/languages/bash';
import css from 'highlight.js/lib/languages/css';
import xml from 'highlight.js/lib/languages/xml';
import { useResource } from './api';
import type { Execution, Project } from './types';
import { bytes, date, Empty, ErrorNotice, Link, Loading, shortId } from './ui';

const AUTO_LIMIT = 2 * 1024 ** 2;
const HARD_LIMIT = 32 * 1024 ** 2;
const languages = { javascript, typescript, python, json, yaml, ini, nix, bash, css, xml };
for (const [name, language] of Object.entries(languages)) hljs.registerLanguage(name, language);
const extensions: Record<string, string> = {
  js: 'javascript',
  jsx: 'javascript',
  mjs: 'javascript',
  ts: 'typescript',
  tsx: 'typescript',
  py: 'python',
  json: 'json',
  yaml: 'yaml',
  yml: 'yaml',
  toml: 'ini',
  ini: 'ini',
  nix: 'nix',
  sh: 'bash',
  bash: 'bash',
  css: 'css',
  html: 'xml',
  xml: 'xml',
  svg: 'xml',
};
interface Entry {
  name: string;
  path: string;
  kind: 'directory' | 'file';
  size: number | null;
  modified_at?: string;
  mime_type: string;
}
interface Directory {
  path: string;
  entries: Entry[];
}

function extension(entry: Entry) {
  return entry.name.split('.').pop()?.toLowerCase() || '';
}
function textLike(entry: Entry) {
  return (
    entry.mime_type.startsWith('text/') ||
    extensions[extension(entry)] ||
    /^(md|mdx|markdown|txt|log|csv|env|conf|cfg|lock|rs|go|c|h|cpp|hpp|rb|java|kt|sql|r|vue|svelte|graphql|gitignore|dockerignore)$/i.test(
      extension(entry),
    ) ||
    !entry.name.includes('.') ||
    entry.name.startsWith('.env')
  );
}
function imageLike(entry: Entry) {
  return /^image\/(png|jpeg|gif|webp|avif|bmp|x-icon)$/.test(entry.mime_type);
}
function kindLabel(entry: Entry) {
  if (entry.kind === 'directory') return 'Folder';
  if (imageLike(entry)) return 'Image';
  return extension(entry).toUpperCase() || 'File';
}
async function copy(value: string) {
  if (navigator.clipboard) return navigator.clipboard.writeText(value);
  const area = document.createElement('textarea');
  area.value = value;
  area.style.position = 'fixed';
  area.style.opacity = '0';
  document.body.append(area);
  area.select();
  const success = document.execCommand('copy');
  area.remove();
  if (!success) throw new Error('Your browser could not copy to the clipboard.');
}

export function WorkspacePanel({
  project,
  executions,
}: {
  project: Project;
  executions: Execution[];
}) {
  const [path, setPath] = useState('');
  const [filter, setFilter] = useState('');
  const [selected, setSelected] = useState<Entry>();
  const active = executions.find(
    (execution) =>
      !['completed', 'blocked', 'failed', 'cancelled', 'stopped'].includes(execution.status),
  );
  const inUse =
    !!active ||
    !['idle', 'completed', 'blocked', 'failed', 'cancelled', 'stopped'].includes(project.status);
  const base = `/api/v1/projects/${project.id}/workspace`;
  const url = (operation: string, relative = path) =>
    `${base}/${operation}?path=${encodeURIComponent(relative)}`;
  const directory = useResource<Directory>(
    inUse ? null : `/projects/${project.id}/workspace/tree?path=${encodeURIComponent(path)}`,
  );
  const entries =
    directory.data?.entries.filter((entry) =>
      entry.name.toLowerCase().includes(filter.toLowerCase()),
    ) ?? [];
  function navigate(next: string) {
    setPath(next);
    setFilter('');
    setSelected(undefined);
  }
  if (inUse)
    return (
      <section className="panel">
        <h2>Workspace currently in use</h2>
        <p className="muted">
          {active
            ? `Run ${shortId(active.run_id)} is working on this project.`
            : 'This project has active or queued work.'}{' '}
          Browsing and downloads will be available when the Run stops.
        </p>
        {active && (
          <Link className="button" href={`/runs/${active.run_id}`}>
            Open Run
          </Link>
        )}
      </section>
    );
  return (
    <section className="panel workspace-panel">
      <div className="row between workspace-heading">
        <div>
          <h2>Workspace</h2>
          <p className="tiny muted">Read-only · /workspace</p>
        </div>
        <a className="button" href={url('archive', '')} download>
          <Icon name="download" /> Download workspace as ZIP
        </a>
      </div>
      <nav className="workspace-breadcrumbs" aria-label="Workspace path">
        <button onClick={() => navigate('')}>Workspace</button>
        {path
          .split('/')
          .filter(Boolean)
          .map((part, index, parts) => (
            <span key={index}>
              {' '}
              /{' '}
              <button onClick={() => navigate(parts.slice(0, index + 1).join('/'))}>{part}</button>
            </span>
          ))}
      </nav>
      <div className={`workspace-browser ${selected ? 'has-preview' : ''}`}>
        <div className="workspace-directory">
          <div className="workspace-directory-tools">
            <input
              aria-label="Filter filenames"
              placeholder="Filter filenames…"
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
            />
            {path && (
              <a className="text-link" href={url('archive')} download>
                Download folder as ZIP
              </a>
            )}
          </div>
          <ErrorNotice error={directory.error} />
          {directory.error && <button onClick={directory.reload}>Try again</button>}
          {directory.loading ? (
            <Loading />
          ) : (
            directory.data && (
              <>
                <div className="workspace-file-list" role="list" aria-label="Workspace files">
                  {entries.map((entry) => (
                    <div
                      role="listitem"
                      key={entry.path}
                      className={`workspace-entry ${selected?.path === entry.path ? 'selected' : ''}`}
                    >
                      <button
                        className="workspace-open"
                        title={entry.name}
                        onClick={() =>
                          entry.kind === 'directory' ? navigate(entry.path) : setSelected(entry)
                        }
                      >
                        <span className="workspace-file-icon" aria-hidden="true">
                          <Icon name={entry.kind === 'directory' ? 'folder' : 'file'} />
                        </span>
                        <span className="workspace-file-name">
                          {entry.name}
                          <small>
                            {kindLabel(entry)}
                            {entry.size != null ? ` · ${bytes(entry.size)}` : ''}
                          </small>
                        </span>
                        <time className="workspace-modified" dateTime={entry.modified_at}>
                          {date(entry.modified_at)}
                        </time>
                      </button>
                      <a
                        className="workspace-entry-download"
                        href={url(entry.kind === 'directory' ? 'archive' : 'download', entry.path)}
                        download
                        title={
                          entry.kind === 'directory' ? 'Download folder as ZIP' : 'Download file'
                        }
                        aria-label={`Download ${entry.name}${entry.kind === 'directory' ? ' as ZIP' : ''}`}
                      >
                        <Icon name="download" />
                      </a>
                    </div>
                  ))}
                </div>
                {!entries.length && (
                  <Empty title={filter ? 'No matching filenames' : 'This folder is empty'}>
                    {filter
                      ? 'Try another filename.'
                      : 'Files created by your Runs will appear here.'}
                  </Empty>
                )}
              </>
            )
          )}
        </div>
        <div className="workspace-preview">
          {selected ? (
            <FilePreview
              key={selected.path}
              entry={selected}
              base={base}
              onBack={() => setSelected(undefined)}
            />
          ) : (
            <Empty title="Select a file">Open a file to preview its contents.</Empty>
          )}
        </div>
      </div>
    </section>
  );
}

function FilePreview({ entry, base, onBack }: { entry: Entry; base: string; onBack: () => void }) {
  const [allowLarge, setAllowLarge] = useState(false);
  const [text, setText] = useState<string>();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(false);
  const [binary, setBinary] = useState(false);
  const [wrap, setWrap] = useState(false);
  const [source, setSource] = useState(false);
  const [copied, setCopied] = useState<string>();
  const [retry, setRetry] = useState(0);
  const image = imageLike(entry);
  const supported = !!textLike(entry) || image;
  const size = entry.size ?? 0;
  const tooLarge = size > HARD_LIMIT;
  const needsApproval = size > AUTO_LIMIT && !allowLarge;
  const previewUrl = `${base}/file?path=${encodeURIComponent(entry.path)}&allow_large=${allowLarge}`;
  const downloadUrl = `${base}/download?path=${encodeURIComponent(entry.path)}`;
  useEffect(() => {
    if (!supported || image || tooLarge || needsApproval) return;
    const controller = new AbortController();
    setLoading(true);
    setError(undefined);
    void fetch(previewUrl, { credentials: 'same-origin', signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error((await response.json()).detail || 'Preview failed');
        // The server enforces this limit before returning any file contents.
        const content = await response.text();
        if (controller.signal.aborted) return;
        if (content.includes('\0')) setBinary(true);
        else setText(content);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setError(reason instanceof Error ? reason.message : 'Preview failed');
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [previewUrl, supported, image, tooLarge, needsApproval, retry]);
  const markdown = /^(md|markdown)$/.test(extension(entry));
  // Expensive parsers are only used for small files. Larger text remains a single text node.
  const rendered = useMemo(
    () =>
      markdown && text != null && text.length <= 512 * 1024
        ? DOMPurify.sanitize(marked.parse(text, { async: false }), {
            ALLOWED_TAGS: [
              'h1',
              'h2',
              'h3',
              'h4',
              'h5',
              'h6',
              'p',
              'a',
              'ul',
              'ol',
              'li',
              'table',
              'thead',
              'tbody',
              'tr',
              'td',
              'th',
              'hr',
              'br',
              'blockquote',
              'pre',
              'code',
              'em',
              'strong',
              'del',
              's',
              'span',
              'div',
            ],
            ALLOWED_ATTR: ['href', 'title', 'colspan', 'rowspan'],
          })
        : undefined,
    [markdown, text],
  );
  const highlighted = useMemo(
    () =>
      text != null && text.length <= 200 * 1024 && extensions[extension(entry)]
        ? hljs.highlight(text, { language: extensions[extension(entry)] }).value
        : undefined,
    [text, entry],
  );
  const numbers = useMemo(() => {
    if (text == null || text.length > 512 * 1024) return undefined;
    const count = text.split('\n').length;
    return count <= 5000
      ? Array.from({ length: count }, (_, index) => index + 1).join('\n')
      : undefined;
  }, [text]);
  async function copyValue(value: string, label: string) {
    try {
      await copy(value);
      setCopied(label);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Copy failed');
    }
  }
  return (
    <>
      <button className="workspace-back" onClick={onBack}>
        <Icon name="back" /> Back to files
      </button>
      <div className="workspace-preview-heading">
        <h3>{entry.path}</h3>
        <p className="tiny muted">
          {bytes(size)} · {kindLabel(entry)} · Modified {date(entry.modified_at)}
        </p>
        <div className="workspace-preview-actions">
          <a className="button" href={downloadUrl} download>
            <Icon name="download" /> Download file
          </a>
          <button onClick={() => void copyValue(`/workspace/${entry.path}`, 'Path copied')}>
            <Icon name="copy" /> Copy path
          </button>
          {text != null && (
            <button onClick={() => void copyValue(text, 'Contents copied')}>
              <Icon name="copy" /> Copy contents
            </button>
          )}
          {text != null && (
            <button aria-pressed={wrap} onClick={() => setWrap(!wrap)}>
              Line wrap
            </button>
          )}
          {rendered != null && (
            <div className="segmented" role="group" aria-label="Markdown preview">
              <button
                className={!source ? 'selected' : ''}
                aria-pressed={!source}
                onClick={() => setSource(false)}
              >
                Rendered
              </button>
              <button
                className={source ? 'selected' : ''}
                aria-pressed={source}
                onClick={() => setSource(true)}
              >
                Source
              </button>
            </div>
          )}
        </div>
        {copied && (
          <span className="tiny muted" role="status">
            {copied}
          </span>
        )}
      </div>
      <ErrorNotice error={error} />
      {error && (
        <button
          onClick={() => {
            setError(undefined);
            setRetry(retry + 1);
          }}
        >
          Retry preview
        </button>
      )}
      {tooLarge ? (
        <Empty title="Download only">
          This file exceeds the {bytes(HARD_LIMIT)} preview safety limit.
        </Empty>
      ) : needsApproval && supported ? (
        <Empty title="Large file">
          <p>This file is larger than the normal {bytes(AUTO_LIMIT)} preview limit.</p>
          <button onClick={() => setAllowLarge(true)}>Load anyway</button>
        </Empty>
      ) : !supported || binary ? (
        <Empty title="Preview unavailable">
          Download this file to open it in another application.
        </Empty>
      ) : loading ? (
        <Loading />
      ) : image ? (
        <div className="workspace-image">
          <img
            key={retry}
            src={previewUrl}
            alt={entry.name}
            onError={() =>
              setError(
                'Could not load this image. It may be invalid, or the workspace is now in use.',
              )
            }
          />
        </div>
      ) : (
        text != null &&
        (rendered != null && !source ? (
          <div className="workspace-markdown" dangerouslySetInnerHTML={{ __html: rendered }} />
        ) : (
          <div className={`workspace-source ${wrap ? 'wrap' : ''}`}>
            {!wrap && numbers && (
              <pre className="workspace-line-numbers" aria-hidden="true">
                {numbers}
              </pre>
            )}
            <pre className="workspace-code">
              {highlighted != null ? (
                <code dangerouslySetInnerHTML={{ __html: highlighted }} />
              ) : (
                <code>{text}</code>
              )}
            </pre>
          </div>
        ))
      )}
    </>
  );
}
