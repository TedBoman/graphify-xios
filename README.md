# graphify-xios

Knowledge graph extractor with native AST support and Claude Code integration for **XIOS/3 applications**.

Maps XIOS XML views, processes, triggers, steps, operations, component skins (XSL/XSLT), catalogs, and custom UI components into a single, queryable knowledge graph.

---

## Quickstart

### 1. Install

Clone the repository and install in editable mode:

```bash
git clone https://github.com/TedBoman/graphify-xios.git
cd graphify-xios
pip install -e .
```

### 2. Configure Claude Code (One Command)

From anywhere on your machine (or inside your XIOS project folder):

```bash
graphify install
```

This single command automatically:
* Installs the `/graphify` skill (`~/.claude/skills/graphify/SKILL.md`).
* Registers active **PreToolUse hooks** (`~/.claude/settings.json` and `.claude/settings.json`) that intercept blind `Grep`/`Read` commands and route Claude to the knowledge graph.
* Sets up project `CLAUDE.md` with strict token-guard rules (safely preserving your existing instructions).

---

## Building the Graph

Open Claude Code in your XIOS project:

```bash
cd /path/to/your/xios-project
claude
```

And run:

```
/graphify .
```

*(Alternatively, run `graphify extract . --code-only` directly from your terminal).*

Graphify outputs three artifacts in `graphify-out/`:
* `graph.html`: Interactive browser visualization of your codebase and detected communities.
* `graph.json`: The complete queryable knowledge graph with canonical node IDs and relations.
* `GRAPH_REPORT.md`: Architecture report showing hub nodes, subsystems, and entry points.

---

## How to Query (Save 95%+ Tokens)

> [!IMPORTANT]
> **Never tell Claude to read `graph.json` or `GRAPH_REPORT.md` directly.**
> `graph.json` is a multi-megabyte file (10–15 MB on typical projects) and `GRAPH_REPORT.md` is 100+ KB. Reading them directly into Claude's context window burns **30,000 to 100,000+ tokens** in a single prompt!
>
> Always use the query commands:

* **In Claude Code:**
  ```text
  /graphify query "How does user authentication work?"
  /graphify path "LoginProcess" "DatabaseView"
  /graphify explain "CA_Pipeline"
  ```

* **From Terminal:**
  ```bash
  graphify query "How does user authentication work?"
  graphify path "service_processes_login" "service_views_database"
  graphify explain "service_processes_projects"
  ```

These commands run locally in milliseconds and return only a compact 20–40 line subgraph (**~300 tokens**), giving Claude precise architectural context at virtually zero token cost.

---

## What We Built for XIOS/3

Standard Graphify only parses standard programming languages. This edition adds full native AST extraction for the XIOS ecosystem:

### 1. View & UI Component Hierarchy
* Parses all layout panels, grids, groups, trees, iconlists, buttons, and inputs.
* Natively detects custom UI tags (`<plotly>`, `<pipeline>`, `<diagram>`, `<filepicker>`, `<chatrender>`) even when unnamed, mapping them to their implementing JS classes and stylesheets.

### 2. Process Controller & Navigation Flow
* Extracts all `<trigger>` events (`Click`, `DoubleClick`, `Select`, `Opened`, `Closing`, etc.).
* Maps `<step>` execution sequences and control flow.
* Parses all step `<operation>` tags:
  * `open`: Resolves views and child processes.
  * `bind`: Resolves target data documents and view component bindings.
  * `callMethod` / `action`: Resolves cross-file target view components and method invocations.
  * `save` / `change` / `database`: Tracks data persistence.
* Tracks `<alias>` variables and `<store>` XSLT transformers.

### 3. Boot & Published Application Linking
* Extracts `window.boot = { startup: "...", system: "..." }` and `<script type="xios" name="addConfig">` in `index.html`.
* Links published application packages (`published/published.xml`, `published/index.html`) directly into the core application graph.
* Traverses and links widget catalog configurations (`widgets.xml`).

### 4. Canonical Cross-File Resolution (`_xios_file_nid`)
* Translates all `host://`, `xios://`, and relative URIs directly into Graphify's canonical project-relative node IDs.
* Unifies 99%+ of disparate views, operations, and processes into a single giant core component, eliminating disconnected periphery clusters.

### 5. Component Skins (XSL/XSLT)
* Extracts templates, `xsl:call-template`, `xsl:apply-templates`, and skin include dependencies.

---

## CLI Reference

| Command | Description |
|---|---|
| `graphify extract <path> [--code-only]` | Build knowledge graph from scratch (code AST only or full semantic). |
| `graphify update <path>` | Incremental update on modified files (zero API cost, milliseconds). |
| `graphify cluster-only <path>` | Re-cluster and regenerate `graph.html` and `GRAPH_REPORT.md` without re-extracting. |
| `graphify query "<question>"` | Search the graph and return an answering subgraph. |
| `graphify path "<from>" "<to>"` | Find shortest dependency/execution path between two nodes. |
| `graphify explain "<concept>"` | Explain a specific node and its neighbors. |
| `graphify install [--project]` | Register Claude Code skill, PreToolUse hooks, and project rules in one command. |
| `graphify claude install [--project]` | Re-install Claude Code integration and hooks. |
