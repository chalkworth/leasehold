---
name: diagramming-in-markdown
description: Use when adding, editing or reviewing a Mermaid text diagram (state, sequence, flow, architecture, ER, timeline) in GitHub-flavoured Markdown such as a README, wiki page, ADR, design document or PR description. Rich, branded or HTML/SVG diagrams are outside this skill
---

# Diagramming in Markdown

Draw the diagram as Mermaid text, so it diffs in review and agents can read it.

## Before writing

1. **Find the destination's fence.** Open a neighbouring page that already has a diagram and copy its fence. GitHub-flavoured Markdown (files, issues, pull requests, wikis) renders a ```` ```mermaid ```` fence. Other renderers may need a different fence; with no neighbour to copy, ask where the page is shown.
2. **State what will render.** GitHub renders the fence inline. In a viewer that does not, the reader sees the source as a code block, so keep the source readable and say so in your answer when the destination is not GitHub.

## Drawing

- One idea per diagram, 15 nodes at most; split anything larger and link the parts.
- Put one sentence above the diagram saying what it shows.
- Start the body with `accTitle:` (3 to 8 words) and `accDescr:` (one sentence).
- Use short labels and meaningful node IDs (`run_tests`, not `A`).
- In sequence diagrams, write a semicolon in a message or Note as `#59;`; a bare `;` fails to parse.
- The diagram must match the code or system it describes: read the source, then draw.

## Check before committing

Parse every diagram in the page with mermaid-cli:

```bash
python3 scripts/check_mermaid.py path/to/page.md
```

It prints `page.md:LINE: ok` or the parse error for each fence, and exits 1 if any diagram fails. It uses a Chrome that is already installed or cached (Playwright's and puppeteer's caches count) and never downloads one. Exit 2 means there is no Chrome or no `npx`: say the diagram is unchecked rather than installing a browser.

→ Choosing a diagram type, quoting special characters, the Mermaid version in the destination, or Chrome setup for mermaid-cli: read `references/diagram-reference.md`.

→ Polished, branded diagrams for presentations are out of scope here; use a dedicated diagram skill if you have one.
