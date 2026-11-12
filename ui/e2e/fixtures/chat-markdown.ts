// Independently constructed museum catalog, not an operational architecture.
export const diagram = `flowchart LR
  subgraph Sources
    SCAN[Exhibit scans] --&gt;|JSON files| INTAKE[Catalog intake]
    FORM[Collection forms] --&gt;|JSONL| INTAKE
    NOTE[Curator notes] --&gt; LEDGER[Review ledger]
  end

  INTAKE --&gt; DRAFT[Draft descriptions]
  DRAFT --&gt; CHECK[Validate sample records]
  CHECK --&gt;|needs review| LEDGER
  CHECK --&gt;|accepted| CATALOG[(Published catalog)]
  LEDGER --&gt;|approved| CATALOG

  CATALOG --&gt; ARCHIVE[(Museum archive)]
  CATALOG --&gt; EXHIBIT[Exhibit pages&lt;br/&gt;public collection]
  ARCHIVE --&gt; BACKUP[(Offline sample copy)]

  EXHIBIT --&gt; SEARCH[Collection search]
  SEARCH --&gt; VISITOR[Visitor display]`;

export const messageMarkdown = `## Data pipeline

**Inspect** the \`pipeline\` before running:

\`\`\`python
def load_rows(bucket: str) -> list:
    return ["JSON files", bucket]
\`\`\`

> Keep the source data.

| Source | Format |
| --- | --- |
| Inventory feeds | JSONL |

- Read input
- Update tables

\`\`\`mermaid
${diagram}
\`\`\``;
