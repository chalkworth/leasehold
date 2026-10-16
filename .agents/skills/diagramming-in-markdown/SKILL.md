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

Save the diagram body without its fence as `diagram.mmd` in a scratch directory and parse it:

```bash
PUPPETEER_SKIP_DOWNLOAD=true PUPPETEER_EXECUTABLE_PATH=/usr/bin/google-chrome \
  npx -y @mermaid-js/mermaid-cli -i diagram.mmd -o diagram.svg
```

`Parse error on line N` means fix the diagram. Parse every diagram in the page. Delete the SVG afterwards unless the page embeds it. Without node, say the diagram is unchecked.

→ Choosing a diagram type, quoting special characters, the Mermaid version in the destination, or Chrome setup for mermaid-cli: read `references/diagram-reference.md`.

→ Polished, branded diagrams for presentations are out of scope here; use a dedicated diagram skill if you have one.
