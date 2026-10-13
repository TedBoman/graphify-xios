"""Regression tests for the CloudBackend PaaS graph-defect report.

Covers parallel-edge merging (#3), EXTRACTED edge grounding and deterministic
XIOS boot-page edges (#4), minified/icon-asset detection (#5) and path-seeded,
grouped query output (#6). The XIOS resolver (#1) and line-number (#2) cases
live in test_xios.py.
"""
from __future__ import annotations

from pathlib import Path

import networkx as nx

from graphify.build import build_from_json
from graphify.detect import _icon_asset_dirs, _is_minified_bundle, detect
from graphify.extractors.xios import resolve_xios_view_process_links
from graphify.semantic_cleanup import ground_extracted_edges
from graphify.serve import _path_seed_nodes, _subgraph_to_text


def _node(nid, label, sf="a.xml", loc="L1"):
    return {"id": nid, "label": label, "file_type": "code",
            "source_file": sf, "source_location": loc, "_origin": "ast"}


def _edge(src, tgt, relation, context, loc):
    return {"source": src, "target": tgt, "relation": relation, "context": context,
            "confidence": "EXTRACTED", "source_file": "a.xml",
            "source_location": loc, "_origin": "ast"}


# ── #3 parallel edges ──────────────────────────────────────────────────────

def test_parallel_edges_keep_count_and_contexts():
    kpis = ["MRR today", "Total users", "Churn", "ARPU"]
    extraction = {
        "nodes": [_node("cat", "statistics.xml"), _node("w", "statoverview.xml")],
        "edges": [_edge("cat", "w", "references", f"catalogs({k})", f"L{i + 2}")
                  for i, k in enumerate(kpis)],
    }
    G = build_from_json(extraction)
    d = G["cat"]["w"]
    assert d["count"] == 4
    assert d["contexts"] == [f"catalogs({k})" for k in kpis]


def test_exact_duplicate_edge_is_not_counted_twice():
    e = _edge("a", "b", "calls", "step_call", "L5")
    extraction = {"nodes": [_node("a", "A"), _node("b", "B")], "edges": [e, dict(e)]}
    G = build_from_json(extraction)
    assert "count" not in G["a"]["b"]


def test_parallel_edges_with_different_relations_keep_both():
    extraction = {
        "nodes": [_node("a", "A"), _node("b", "B")],
        "edges": [_edge("a", "b", "calls", "step_call", "L3"),
                  _edge("a", "b", "references", "opens_view", "L4")],
    }
    G = build_from_json(extraction)
    d = G["a"]["b"]
    assert d["relation"] == "calls"  # specific relation still wins the single slot
    assert d["count"] == 2
    assert set(d["relations"]) == {"calls", "references"}
    assert set(d["contexts"]) == {"step_call", "opens_view"}


# ── #4 grounding + boot pages ──────────────────────────────────────────────

def test_ungrounded_extracted_edge_is_downgraded(tmp_path):
    (tmp_path / "test.html").write_text(
        "<script>var boot = {startup: 'x'}</script><script src='/xios/boot.js'></script>",
        encoding="utf-8")
    frag = {
        "nodes": [
            {"id": "playwright_test", "label": "@playwright/test", "source_file": "package.json"},
            {"id": "xios_boot_config", "label": "XIOS boot config (var boot + /xios/boot.js)",
             "source_file": "index.html"},
            {"id": "test_page", "label": "test.html", "source_file": "test.html"},
        ],
        "edges": [
            {"source": "playwright_test", "target": "xios_boot_config", "relation": "implements",
             "confidence": "EXTRACTED", "source_file": "test.html"},
            {"source": "test_page", "target": "xios_boot_config", "relation": "uses",
             "confidence": "EXTRACTED", "source_file": "test.html"},
        ],
    }
    assert ground_extracted_edges(frag, root=tmp_path) == 1
    bad, good = frag["edges"]
    assert bad["confidence"] == "INFERRED"
    assert "playwright_test" in bad["grounding"]
    assert good["confidence"] == "EXTRACTED"


def test_every_page_that_boots_xios_gets_an_edge(tmp_path):
    boot_page = "<script>var boot = {}</script>\n<script src=\"/xios/boot.js\"></script>"
    for name in ("index.html", "test.html", "test-db.html"):
        (tmp_path / name).write_text(boot_page, encoding="utf-8")
    (tmp_path / "about.html").write_text("<p>no boot here</p>", encoding="utf-8")
    nodes = [{"id": "v", "label": "V", "node_kind": "view",
              "source_file": str(tmp_path / "views" / "v.xml")}]
    edges: list[dict] = []
    resolve_xios_view_process_links([], nodes, edges)
    booted = {Path(e["source_file"]).name for e in edges if e.get("context") == "boots_xios"}
    assert booted == {"index.html", "test.html", "test-db.html"}


# ── #5 minified bundles / icon folders ─────────────────────────────────────

def test_minified_bundles_are_detected(tmp_path):
    named = tmp_path / "plotly-basic.min.js"
    named.write_text("function a(){}", encoding="utf-8")
    long_lines = tmp_path / "bundle.js"
    long_lines.write_text("var a=1;" * 5000, encoding="utf-8")
    normal = tmp_path / "app.js"
    normal.write_text("function main() {\n  return 1;\n}\n" * 800, encoding="utf-8")
    assert _is_minified_bundle(named)
    assert _is_minified_bundle(long_lines)
    assert not _is_minified_bundle(normal)


def test_icon_folders_are_assets_not_vision_inputs(tmp_path):
    icons = tmp_path / "icons"
    icons.mkdir()
    svgs = []
    for i in range(30):
        p = icons / f"i{i}.svg"
        p.write_text("<svg/>", encoding="utf-8")
        svgs.append(str(p))
    diagram = tmp_path / "docs" / "architecture.png"
    diagram.parent.mkdir()
    diagram.write_bytes(b"\x89PNG" + b"0" * 100)
    assert _icon_asset_dirs(svgs + [str(diagram)]) == {str(icons)}

    result = detect(tmp_path)
    assert result["files"]["image"] == [str(diagram)]
    assert sum(1 for s in result["skipped_generated"] if s.endswith("[icon asset]")) == 30


# ── #6 query seeding + grouping ────────────────────────────────────────────

def _manifest_graph(n=27):
    G = nx.Graph()
    G.add_node("proc", label="Shell", source_file="service/processes/dashboard.xml",
               source_location="L1")
    G.add_node("view", label="DE", source_file="service/views/dashboard.xml",
               source_location="L1")
    G.add_node("decoy", label="viewLicense.xsl", source_file="ui/viewLicense.xsl",
               source_location="L1")
    for i in range(n):
        nid = f"open{i}"
        G.add_node(nid, label="open(host://service/processes/dashboard.xml)",
                   _xios_target="host://service/processes/dashboard.xml",
                   source_file=f"service/manifests/m{i}.xml", source_location=f"L{30 + i}")
        G.add_edge(nid, "proc", relation="references", confidence="EXTRACTED",
                   context="opens_process", source_file=f"service/manifests/m{i}.xml",
                   source_location=f"L{30 + i}")
    return G


def test_exact_path_in_question_seeds_from_that_path():
    G = _manifest_graph()
    seeds = _path_seed_nodes(G, "which files open processes/dashboard.xml")
    assert "view" not in seeds and "decoy" not in seeds
    assert "proc" in seeds
    assert sum(1 for s in seeds if s.startswith("open")) == 27


def test_same_label_nodes_render_as_one_group_within_budget():
    G = _manifest_graph()
    nodes = set(G.nodes) - {"view", "decoy"}
    edges = list(G.edges())
    out = _subgraph_to_text(G, nodes, edges, token_budget=300, seeds=["proc"])
    assert "TRUNCATED" not in out
    assert "x27 nodes in service/manifests/*" in out
    assert out.count("NODE open(") == 1
    assert "x27" in [ln for ln in out.splitlines() if ln.startswith("EDGE")][0]
