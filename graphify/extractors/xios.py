"""XIOS/3 extractor — XML views, processes, component skins, and application manifest.

Parses the declarative XML artifacts of an XIOS/3 application:
  - Application manifest (index.xml): component registrations, resources,
    view/process xlink declarations.
  - Process files (processes/*.xml): triggers, steps, operations, aliases,
    data bindings, method calls, navigation, and step-to-step flows.
  - View files (views/*.xml): layout panels, component instantiations,
    xlink-embedded sub-views, grids, groups.
  - Component skins (*.xsl): XSLT templates, call-template / apply-templates
    references, xsl:include / xsl:import dependencies.

No tree-sitter dependency — pure xml.etree.ElementTree parsing.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

from graphify.extractors.base import _file_stem, _make_id, _read_source_bytes


# ── Safety ──────────────────────────────────────────────────────────────────
_XML_MAX_BYTES = 4 * 1024 * 1024  # 4 MiB — generous for XIOS descriptors


def _xml_is_safe(raw: bytes) -> bool:
    """Reject files that carry DTD entity declarations (XXE vector)."""
    lowered = raw[:8192].lower()
    return b"<!entity" not in lowered


# ── Namespace helpers ───────────────────────────────────────────────────────
_XSL_NS = "http://www.w3.org/1999/XSL/Transform"
_XLINK_NS = "http://www.w3.org/1999/xlink"

# Common XIOS element tag patterns (namespace-unaware, matched after strip).
_XIOS_PROCESS_ROOT = "process"
_XIOS_VIEW_ROOT = "view"
_XIOS_APP_ROOT = "application"

_NON_COMPONENT_TAGS = {
    "view", "rule", "when", "otherwise", "description", "param", "method",
    "operation", "operations", "trigger", "step", "listener", "listen",
    "script", "render", "skin", "style", "dependency",
}

_CUSTOM_TAGS = {
    "scorechart", "sessioneditor", "sessiontrainer", "thememanager",
    "navmenu", "tutorial", "alarm", "statsboard", "prefseditor",
    "courseeditor", "pipeline", "diagram", "plotly", "filepicker", "chatrender",
}


def _strip_ns(tag: str) -> str:
    """Strip the ``{namespace}`` prefix from an ElementTree tag name."""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _attr(el: ET.Element, local: str, ns: str | None = None) -> str:
    """Read an attribute, trying namespaced then bare local name."""
    if ns:
        val = el.get(f"{{{ns}}}{local}")
        if val is not None:
            return val
    return el.get(local, "")


# ── Path & URI Resolution ──────────────────────────────────────────────────

def _find_xios_root(start: Path) -> Path:
    """Find the host/application root directory for an XIOS project."""
    p = start.resolve()
    dirs = [p] if p.is_dir() else [p.parent]
    dirs.extend(dirs[0].parents)
    for candidate in dirs:
        if (
            (candidate / "index.html").exists()
            or (candidate / "package.json").exists()
            or (candidate / ".git").exists()
            or (candidate / "config").is_dir()
            or (candidate / "service").is_dir()
        ):
            return candidate
    return start.parent


def _resolve_xios_uri(base_file: Path, uri: str, root: Path | None = None) -> Path:
    """Resolve an XIOS URI (host://, xios://, or relative) to a canonical Path."""
    if not uri:
        return base_file
    host_root = root if root is not None else _find_xios_root(base_file)

    if uri.startswith("host://"):
        rel = uri[7:].lstrip("/\\")
        cand = host_root / rel
        if cand.exists():
            return cand.resolve()
        p_cand = base_file.parent / rel
        if p_cand.exists():
            return p_cand.resolve()
        if rel.startswith("service/") and (host_root / rel[8:]).exists():
            return (host_root / rel[8:]).resolve()
        if not rel.startswith("service/") and (host_root / "service" / rel).exists():
            return (host_root / "service" / rel).resolve()
        return Path(os.path.normpath(str(cand)))

    elif uri.startswith("xios://"):
        rel = uri[7:].lstrip("/\\")
        cand_xios = host_root / "xios" / rel
        if cand_xios.exists():
            return cand_xios.resolve()
        rel_no_core = re.sub(r'^core/', '', rel)
        cand_core = host_root / "xios" / rel_no_core
        if cand_core.exists():
            return cand_core.resolve()
        cand_rel = host_root / rel
        if cand_rel.exists():
            return cand_rel.resolve()
        return Path(os.path.normpath(str(cand_xios)))

    else:
        clean = uri.lstrip("/\\") if uri.startswith("/") else uri
        cand = base_file.parent / clean
        if cand.exists():
            return cand.resolve()
        if (host_root / clean).exists():
            return (host_root / clean).resolve()
        return Path(os.path.normpath(str(cand)))


def _xios_file_nid(target_path: Path, host_root: Path | None = None) -> str:
    """Canonical graphify file node ID matching _file_node_id."""
    try:
        t_res = target_path.resolve()
    except (OSError, RuntimeError):
        t_res = target_path
    if host_root is not None:
        try:
            h_res = host_root.resolve()
        except (OSError, RuntimeError):
            h_res = host_root
        try:
            rel = t_res.relative_to(h_res)
            return _make_id(_file_stem(rel))
        except ValueError:
            pass
    return _make_id(_file_stem(t_res))



# ── Sniffers ────────────────────────────────────────────────────────────────

def is_xios_manifest(path: Path, head: bytes | None = None) -> bool:
    """True if *path* is an XIOS application manifest (index.xml, manifests/*.xml, or <application root)."""
    if head is None:
        try:
            head = path.read_bytes()[:4096]
        except OSError:
            return False
    lowered = head.lower()
    return b"<application" in lowered


def is_xios_process(path: Path, head: bytes | None = None) -> bool:
    """True if *path* is an XIOS process definition."""
    if head is None:
        try:
            head = path.read_bytes()[:4096]
        except OSError:
            return False
    lowered = head.lower()
    return b"<process" in lowered and (b"<step" in lowered or b"<trigger" in lowered)


def is_xios_view(path: Path, head: bytes | None = None) -> bool:
    """True if *path* is an XIOS view file."""
    if head is None:
        try:
            head = path.read_bytes()[:4096]
        except OSError:
            return False
    lowered = head.lower()
    # Must have a <view element but NOT be an xsl:stylesheet or an <application
    return (
        b"<view" in lowered
        and b"<application" not in lowered
        and b"xsl:stylesheet" not in lowered
        and b"xsl:transform" not in lowered
    )


def is_xios_types(path: Path, head: bytes | None = None) -> bool:
    """True if *path* is an XIOS types definition (types.xml)."""
    if path.name.lower() != "types.xml":
        return False
    if head is None:
        try:
            head = path.read_bytes()[:4096]
        except OSError:
            return False
    lowered = head.lower()
    return b"<types" in lowered


def is_xios_system(path: Path, head: bytes | None = None) -> bool:
    """True if *path* is an XIOS system descriptor (<system)."""
    if head is None:
        try:
            head = path.read_bytes()[:4096]
        except OSError:
            return False
    lowered = head.lower()
    return b"<system" in lowered


def is_xios_component(path: Path, head: bytes | None = None) -> bool:
    """True if *path* is a standalone XIOS component descriptor (<component)."""
    if head is None:
        try:
            head = path.read_bytes()[:4096]
        except OSError:
            return False
    lowered = head.lower()
    return b"<component" in lowered and b"<application" not in lowered


def is_xios_catalog(path: Path, head: bytes | None = None) -> bool:
    """True if *path* is an XIOS catalog (<applications or <widgets)."""
    if head is None:
        try:
            head = path.read_bytes()[:4096]
        except OSError:
            return False
    lowered = head.lower()
    return b"<applications" in lowered or b"<widgets" in lowered


def is_xios_xml(path: Path) -> bool:
    """True if *path* looks like any XIOS XML artifact (.xml with XIOS markers)."""
    if path.suffix.lower() != ".xml":
        return False
    try:
        head = path.read_bytes()[:4096]
    except OSError:
        return False
    return (
        is_xios_manifest(path, head)
        or is_xios_process(path, head)
        or is_xios_view(path, head)
        or is_xios_types(path, head)
        or is_xios_system(path, head)
        or is_xios_component(path, head)
        or is_xios_catalog(path, head)
    )


# ── XSL Skin Extractor ─────────────────────────────────────────────────────

def extract_xios_xsl(path: Path) -> dict:
    """Extract structure from an XSL component skin file."""
    try:
        raw = _read_source_bytes(path)
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    if len(raw) > _XML_MAX_BYTES:
        return {"nodes": [], "edges": [], "error": "xsl file too large to index"}
    if not _xml_is_safe(raw):
        return {"nodes": [], "edges": [], "error": "xsl file contains entity declarations"}

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        return {"nodes": [], "edges": [], "error": f"xml parse error: {e}"}

    stem = _file_stem(path)
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()

    def add_node(nid: str, label: str, line: int, file_type: str = "code",
                 node_kind: str = "component", extra: dict | None = None) -> None:
        if nid and nid not in seen_ids:
            seen_ids.add(nid)
            node = {"id": nid, "label": label, "file_type": file_type,
                    "node_kind": node_kind,
                    "source_file": str_path, "source_location": f"L{line}"}
            if extra:
                node.update(extra)
            nodes.append(node)

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", weight: float = 1.0,
                 context: str | None = None) -> None:
        if not src or not tgt or src == tgt:
            return
        edge = {"source": src, "target": tgt, "relation": relation,
                "confidence": confidence, "source_file": str_path,
                "source_location": f"L{line}", "weight": weight}
        if context:
            edge["context"] = context
        edges.append(edge)

    # File node
    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1, node_kind="file")

    # Walk xsl:template elements
    for tpl in root.iter(f"{{{_XSL_NS}}}template"):
        tpl_name = tpl.get("name", "") or ""
        tpl_match = tpl.get("match", "") or ""
        label = tpl_name or f"match={tpl_match}" if tpl_match else "anonymous"
        tpl_nid = _make_id(stem, "tpl", tpl_name or tpl_match or "anon")
        add_node(tpl_nid, label, 1, node_kind="template")
        add_edge(file_nid, tpl_nid, "contains", 1)

        # xsl:call-template → calls edge
        for ct in tpl.iter(f"{{{_XSL_NS}}}call-template"):
            called = ct.get("name", "")
            if called:
                target_nid = _make_id(stem, "tpl", called)
                add_edge(tpl_nid, target_nid, "calls", 1, context="call-template")

        # xsl:apply-templates → uses edge
        for at in tpl.iter(f"{{{_XSL_NS}}}apply-templates"):
            sel = at.get("select", "")
            if sel:
                target_nid = _make_id(stem, "apply", sel)
                add_node(target_nid, f"apply({sel})", 1, node_kind="template_target")
                add_edge(tpl_nid, target_nid, "uses", 1, context="apply-templates")

    # xsl:include / xsl:import → imports edge
    for inc_tag in ("include", "import"):
        for inc in root.iter(f"{{{_XSL_NS}}}{inc_tag}"):
            href = inc.get("href", "")
            if href:
                target_path = path.parent / href
                target_nid = _make_id(str(target_path))
                add_edge(file_nid, target_nid, "imports", 1,
                         context=f"xsl:{inc_tag}")

    return {"nodes": nodes, "edges": edges}


# ── XIOS XML Extractor (views, processes, manifest) ────────────────────────

def extract_xios(path: Path, root: Path | None = None) -> dict:
    """Extract structure from an XIOS XML file (manifest, process, view, system, component, or catalog)."""
    try:
        raw = _read_source_bytes(path)
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    if len(raw) > _XML_MAX_BYTES:
        return {"nodes": [], "edges": [], "error": "xios xml file too large to index"}
    if not _xml_is_safe(raw):
        return {"nodes": [], "edges": [], "error": "xios xml file contains entity declarations"}

    try:
        root_el = ET.fromstring(raw)
    except ET.ParseError as e:
        return {"nodes": [], "edges": [], "error": f"xml parse error: {e}"}

    host_root = root if root is not None else _find_xios_root(path)
    root_tag = _strip_ns(root_el.tag)

    if root_tag == _XIOS_APP_ROOT:
        return _extract_manifest(path, root_el, raw, host_root=host_root)
    elif root_tag == _XIOS_PROCESS_ROOT:
        return _extract_process(path, root_el, raw, host_root=host_root)
    elif root_tag == _XIOS_VIEW_ROOT:
        return _extract_view(path, root_el, raw, host_root=host_root)
    elif root_tag == "types":
        return _extract_types(path, root_el, raw)
    elif root_tag == "system":
        return _extract_system(path, root_el, raw, host_root=host_root)
    elif root_tag == "component":
        return _extract_component_config(path, root_el, raw, host_root=host_root)
    elif root_tag in ("applications", "widgets"):
        return _extract_catalog(path, root_el, raw, host_root=host_root)
    else:
        # Unknown XIOS XML shape — produce a bare file node
        file_nid = _make_id(str(path))
        return {
            "nodes": [{"id": file_nid, "label": path.name, "file_type": "code",
                        "source_file": str(path), "source_location": "L1"}],
            "edges": [],
        }


def _extract_component_config(path: Path, root: ET.Element, raw: bytes, host_root: Path | None = None) -> dict:
    stem = _file_stem(path)
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()

    def add_node(nid, label, line, file_type="code", node_kind="component", extra=None):
        if nid and nid not in seen_ids:
            seen_ids.add(nid)
            node = {"id": nid, "label": label, "file_type": file_type,
                    "node_kind": node_kind,
                    "source_file": str_path, "source_location": f"L{line}"}
            if extra:
                node.update(extra)
            nodes.append(node)

    def add_edge(src, tgt, relation, line, confidence="EXTRACTED",
                 weight=1.0, context=None, target_file=None):
        if not src or not tgt or src == tgt:
            return
        edge = {"source": src, "target": tgt, "relation": relation,
                "confidence": confidence, "source_file": str_path,
                "source_location": f"L{line}", "weight": weight}
        if context:
            edge["context"] = context
        if target_file:
            edge["target_file"] = target_file
        edges.append(edge)

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1, node_kind="file")

    comp_name = root.get("name", path.parent.name)
    comp_tag = root.get("tag", "")
    comp_class = root.get("class", "")
    comp_href = root.get("href", "")

    comp_nid = _make_id(stem, "comp", comp_name)
    label = comp_name
    if comp_tag:
        label = f"{comp_name} <{comp_tag}>"
    add_node(comp_nid, label, 1, node_kind="component_definition",
             extra={"tag": comp_tag, "class_name": comp_class, "_xios_comp_def": True})
    add_edge(file_nid, comp_nid, "contains", 1)

    if comp_href:
        js_path = _resolve_xios_uri(path, comp_href, host_root)
        js_nid = _xios_file_nid(js_path, host_root)
        add_edge(comp_nid, js_nid, "references", 1,
                 context="implemented_by", target_file=str(js_path))

    for child in root:
        ctag = _strip_ns(child.tag)
        href = child.get("href", "")
        if not href:
            continue
        tgt_path = _resolve_xios_uri(path, href, host_root)
        tgt_nid = _xios_file_nid(tgt_path, host_root)
        if ctag == "skin":
            add_edge(comp_nid, tgt_nid, "references", 1,
                     context="skinned_by", target_file=str(tgt_path))
        elif ctag == "style":
            add_edge(comp_nid, tgt_nid, "references", 1,
                     context="styled_by", target_file=str(tgt_path))
        elif ctag == "dependency":
            sym = child.get("symbol", "")
            add_edge(comp_nid, tgt_nid, "references", 1,
                     context=f"depends_on({sym})" if sym else "depends_on",
                     target_file=str(tgt_path))

    return {"nodes": nodes, "edges": edges}


def _extract_system(path: Path, root: ET.Element, raw: bytes, host_root: Path | None = None) -> dict:
    stem = _file_stem(path)
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()

    def add_node(nid, label, line, file_type="code", node_kind="component", extra=None):
        if nid and nid not in seen_ids:
            seen_ids.add(nid)
            node = {"id": nid, "label": label, "file_type": file_type,
                    "node_kind": node_kind,
                    "source_file": str_path, "source_location": f"L{line}"}
            if extra:
                node.update(extra)
            nodes.append(node)

    def add_edge(src, tgt, relation, line, confidence="EXTRACTED",
                 weight=1.0, context=None, target_file=None):
        if not src or not tgt or src == tgt:
            return
        edge = {"source": src, "target": tgt, "relation": relation,
                "confidence": confidence, "source_file": str_path,
                "source_location": f"L{line}", "weight": weight}
        if context:
            edge["context"] = context
        if target_file:
            edge["target_file"] = target_file
        edges.append(edge)

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1, node_kind="file")

    startup = root.get("startup", "")
    if startup:
        s_path = _resolve_xios_uri(path, startup, host_root)
        s_nid = _xios_file_nid(s_path, host_root)
        add_edge(file_nid, s_nid, "references", 1, context="startup", target_file=str(s_path))

    for param in root.iter("param"):
        pname = param.get("name", "")
        pval = (param.text or "").strip()
        if pval and (pval.endswith(".xml") or pval.endswith(".js") or pval.startswith("host://") or pval.startswith("xios://")):
            p_path = _resolve_xios_uri(path, pval, host_root)
            p_nid = _xios_file_nid(p_path, host_root)
            add_edge(file_nid, p_nid, "references", 1,
                     context=f"configures({pname})" if pname else "configures",
                     target_file=str(p_path))

    for cls in root.iter("class"):
        cname = cls.get("name", "")
        chref = cls.get("href", "")
        if chref:
            c_path = _resolve_xios_uri(path, chref, host_root)
            c_nid = _xios_file_nid(c_path, host_root)
            add_edge(file_nid, c_nid, "references", 1,
                     context=f"registers_class({cname})" if cname else "registers_class",
                     target_file=str(c_path))

    # 1. Themes: <theme name="..." xlink:href="..."/>
    for th in root.iter("theme"):
        th_name = th.get("name", "")
        th_href = _attr(th, "href", _XLINK_NS) or th.get("href", "")
        if th_href:
            th_path = _resolve_xios_uri(path, th_href, host_root)
            th_nid = _xios_file_nid(th_path, host_root)
            add_edge(file_nid, th_nid, "references", 1,
                     context=f"registers_theme({th_name})" if th_name else "registers_theme",
                     target_file=str(th_path))

    # 2. Components: <component name="..." class="..." href="...">
    for comp in root.iter("component"):
        comp_name = comp.get("name", "")
        comp_tag = comp.get("tag", "")
        comp_class = comp.get("class", "")
        comp_href = comp.get("href", "")
        if not comp_name:
            continue

        comp_nid = _make_id(stem, "comp", comp_name)
        label = comp_name
        if comp_tag:
            label = f"{comp_name} <{comp_tag}>"
        add_node(comp_nid, label, 1, node_kind="component_definition",
                 extra={"tag": comp_tag, "class_name": comp_class, "_xios_comp_def": True})
        add_edge(file_nid, comp_nid, "contains", 1)

        if comp_href:
            js_path = _resolve_xios_uri(path, comp_href, host_root)
            js_nid = _xios_file_nid(js_path, host_root)
            add_edge(comp_nid, js_nid, "references", 1,
                     context="implemented_by", target_file=str(js_path))

        for child in comp:
            ctag = _strip_ns(child.tag)
            href = child.get("href", "")
            if not href:
                continue
            tgt_path = _resolve_xios_uri(path, href, host_root)
            tgt_nid = _xios_file_nid(tgt_path, host_root)
            if ctag == "skin":
                add_edge(comp_nid, tgt_nid, "references", 1,
                         context="skinned_by", target_file=str(tgt_path))
            elif ctag == "style":
                add_edge(comp_nid, tgt_nid, "references", 1,
                         context="styled_by", target_file=str(tgt_path))
            elif ctag == "dependency":
                sym = child.get("symbol", "")
                add_edge(comp_nid, tgt_nid, "references", 1,
                         context=f"depends_on({sym})" if sym else "depends_on",
                         target_file=str(tgt_path))

    # 3. Item URLs: <item ... url="host://apps/upload/upload.xml"/>
    for el in root.iter():
        url = el.get("url", "")
        if url and (url.startswith("host://") or url.endswith(".xml")):
            target_path = _resolve_xios_uri(path, url, host_root)
            target_nid = _xios_file_nid(target_path, host_root)
            add_edge(file_nid, target_nid, "references", 1,
                     context="registers_app", target_file=str(target_path))

    # 4. Embedded process
    for proc in root.iter("process"):
        proc_name = proc.get("name", "systemProcess")
        proc_nid = _make_id(stem, "proc", proc_name)
        add_node(proc_nid, proc_name, 1, node_kind="process",
                 extra={"_xios_proc_name": proc_name, "_xios_stem": stem, "_xios_file": str_path})
        add_edge(file_nid, proc_nid, "contains", 1)
        _extract_process_body(path, proc, stem, str_path, proc_nid,
                              nodes, edges, seen_ids, None,
                              add_node=add_node, add_edge=add_edge,
                              host_root=host_root)

    return {"nodes": nodes, "edges": edges}


def _extract_catalog(path: Path, root: ET.Element, raw: bytes, host_root: Path | None = None) -> dict:
    stem = _file_stem(path)
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()

    def add_node(nid, label, line, file_type="code", node_kind="component", extra=None):
        if nid and nid not in seen_ids:
            seen_ids.add(nid)
            node = {"id": nid, "label": label, "file_type": file_type,
                    "node_kind": node_kind,
                    "source_file": str_path, "source_location": f"L{line}"}
            if extra:
                node.update(extra)
            nodes.append(node)

    def add_edge(src, tgt, relation, line, confidence="EXTRACTED",
                 weight=1.0, context=None, target_file=None):
        if not src or not tgt or src == tgt:
            return
        edge = {"source": src, "target": tgt, "relation": relation,
                "confidence": confidence, "source_file": str_path,
                "source_location": f"L{line}", "weight": weight}
        if context:
            edge["context"] = context
        if target_file:
            edge["target_file"] = target_file
        edges.append(edge)

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1, node_kind="file")

    for entry in root:
        name = entry.get("name", "") or entry.get("id", "")
        src_el = entry.find("source")
        src_val = (src_el.text or "").strip() if src_el is not None else ""
        if src_val:
            tgt_path = _resolve_xios_uri(path, src_val, host_root)
            tgt_nid = _xios_file_nid(tgt_path, host_root)
            add_edge(file_nid, tgt_nid, "references", 1,
                     context=f"catalogs({name})" if name else "catalogs",
                     target_file=str(tgt_path))

    return {"nodes": nodes, "edges": edges}


# ── Types ───────────────────────────────────────────────────────────────────

def _extract_types(path: Path, root: ET.Element, raw: bytes) -> dict:
    stem = _file_stem(path)
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()

    def add_node(nid, label, line, file_type="code", node_kind="datatype", extra=None):
        if nid and nid not in seen_ids:
            seen_ids.add(nid)
            node = {"id": nid, "label": label, "file_type": file_type,
                    "node_kind": node_kind,
                    "source_file": str_path, "source_location": f"L{line}"}
            if extra:
                node.update(extra)
            nodes.append(node)

    def add_edge(src, tgt, relation, line, confidence="EXTRACTED",
                 weight=1.0, context=None, target_file=None):
        if not src or not tgt or src == tgt:
            return
        edge = {"source": src, "target": tgt, "relation": relation,
                "confidence": confidence, "source_file": str_path,
                "source_location": f"L{line}", "weight": weight}
        if context:
            edge["context"] = context
        if target_file:
            edge["target_file"] = target_file
        edges.append(edge)

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1, node_kind="file")

    for type_el in root.iter():
        if _strip_ns(type_el.tag) == "type":
            name = type_el.get("name", "")
            if name:
                desc = ""
                for child in type_el:
                    if _strip_ns(child.tag) == "description":
                        desc = (child.text or "").strip()
                        break
                type_nid = _make_id(stem, "type", name)
                label = f"type:{name}"
                if desc:
                    label += f" ({desc})"
                add_node(type_nid, label, 1, node_kind="datatype", extra={"description": desc})
                add_edge(file_nid, type_nid, "contains", 1)

    return {"nodes": nodes, "edges": edges}


# ── Manifest ────────────────────────────────────────────────────────────────

def _extract_manifest(path: Path, root: ET.Element, raw: bytes, host_root: Path | None = None) -> dict:
    stem = _file_stem(path)
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()

    def add_node(nid, label, line, file_type="code", node_kind="module", extra=None):
        if nid and nid not in seen_ids:
            seen_ids.add(nid)
            node = {"id": nid, "label": label, "file_type": file_type,
                    "node_kind": node_kind,
                    "source_file": str_path, "source_location": f"L{line}"}
            if extra:
                node.update(extra)
            nodes.append(node)

    def add_edge(src, tgt, relation, line, confidence="EXTRACTED",
                 weight=1.0, context=None, target_file=None):
        if not src or not tgt or src == tgt:
            return
        edge = {"source": src, "target": tgt, "relation": relation,
                "confidence": confidence, "source_file": str_path,
                "source_location": f"L{line}", "weight": weight}
        if context:
            edge["context"] = context
        if target_file:
            edge["target_file"] = target_file
        edges.append(edge)

    app_name = root.get("name", path.stem)
    app_nid = _make_id(str(path))
    add_node(app_nid, app_name, 1, node_kind="application")

    # Component declarations
    for comp in root.iter("component"):
        comp_name = comp.get("name", "")
        comp_tag = comp.get("tag", "")
        comp_class = comp.get("class", "")
        comp_href = comp.get("href", "")
        if not comp_name:
            continue

        comp_nid = _make_id(stem, "comp", comp_name)
        label = comp_name
        if comp_tag:
            label = f"{comp_name} <{comp_tag}>"
        add_node(comp_nid, label, 1, node_kind="component_definition",
                 extra={"tag": comp_tag, "class_name": comp_class, "_xios_comp_def": True})
        add_edge(app_nid, comp_nid, "contains", 1, context="declares_component")

        # Link to implementation JS
        if comp_href:
            js_path = _resolve_xios_uri(path, comp_href, host_root)
            js_nid = _xios_file_nid(js_path, host_root)
            add_edge(comp_nid, js_nid, "references", 1,
                     context="implemented_by", target_file=str(js_path))

        # Link to skin XSL
        for skin in comp:
            if _strip_ns(skin.tag) == "skin":
                skin_href = skin.get("href", "")
                if skin_href:
                    skin_path = _resolve_xios_uri(path, skin_href, host_root)
                    skin_nid = _xios_file_nid(skin_path, host_root)
                    add_edge(comp_nid, skin_nid, "references", 1,
                     context="skinned_by", target_file=str(skin_path))

        # Link to style CSS
        for style in comp:
            if _strip_ns(style.tag) == "style":
                style_href = style.get("href", "")
                if style_href:
                    style_path = _resolve_xios_uri(path, style_href, host_root)
                    style_nid = _xios_file_nid(style_path, host_root)
                    add_edge(comp_nid, style_nid, "references", 1,
                     context="styled_by", target_file=str(style_path))

    # Resource declarations
    res_section = root.find("resources")
    if res_section is not None:
        for res in res_section.iter("item"):
            res_name = res.get("name", "")
            if res_name:
                res_nid = _make_id(stem, "res", res_name)
                add_node(res_nid, f"#{res_name}", 1, node_kind="resource")
                add_edge(app_nid, res_nid, "contains", 1, context="declares_resource")
    for child in root:
        if _strip_ns(child.tag) == "item":
            res_name = child.get("name", "")
            if res_name:
                res_nid = _make_id(stem, "res", res_name)
                add_node(res_nid, f"#{res_name}", 1, node_kind="resource")
                add_edge(app_nid, res_nid, "contains", 1, context="declares_resource")

    # View declarations (both xlink mounts and inline views)
    for view_el in root.iter("view"):
        v_name = view_el.get("name", "")
        v_href = _attr(view_el, "href", _XLINK_NS) or view_el.get("href", "")
        actuate = _attr(view_el, "actuate", _XLINK_NS) or "onRequest"

        if v_name:
            v_nid = _make_id(stem, "view", v_name)
            add_node(v_nid, v_name, 1, node_kind="view",
                     extra={"_xios_view_name": v_name, "_xios_stem": stem, "_xios_file": str_path})
            add_edge(app_nid, v_nid, "contains", 1, context="declares_view")
        else:
            v_nid = app_nid

        if v_href and v_href.endswith(".xml"):
            view_path = _resolve_xios_uri(path, v_href, host_root)
            view_nid = _xios_file_nid(view_path, host_root)
            add_edge(v_nid, view_nid, "references", 1,
                     context=f"mounts_view({actuate})",
                     target_file=str(view_path))

        # Extract components and embedded mounts inside view_el
        for child in view_el.iter():
            if child == view_el:
                continue
            c_href = _attr(child, "href", _XLINK_NS) or child.get("href", "")
            if c_href and not c_href.startswith("http"):
                ext = Path(c_href.split("?")[0]).suffix.lower()
                if ext in (".xml", ".xsl", ".xslt", ".js"):
                    child_path = _resolve_xios_uri(path, c_href, host_root)
                    child_nid = _xios_file_nid(child_path, host_root)
                    ctx = "mounts_view" if ext == ".xml" else ("skinned_by" if ext in (".xsl", ".xslt") else "imports_script")
                    add_edge(v_nid, child_nid, "references", 1,
                             context=ctx, target_file=str(child_path))
            c_name = child.get("name", "") or child.get("id", "")
            c_tag = _strip_ns(child.tag).lower()
            if c_name and c_tag not in ("view", "panel", "rule", "param", "method", "operation"):
                comp_nid = _make_id(stem, c_tag, c_name)
                add_node(comp_nid, f"{c_tag}:{c_name}", 1, node_kind="ui_element",
                         extra={
                             "_xios_comp_name": c_name,
                             "_xios_view_name": v_name,
                             "_xios_view_nid": v_nid,
                             "_xios_tag": c_tag,
                         })
                add_edge(v_nid, comp_nid, "contains", 1)

    # Process declarations (both xlink registrations and inline processes)
    for proc_el in root.iter("process"):
        proc_name = proc_el.get("name", "")
        href = _attr(proc_el, "href", _XLINK_NS) or proc_el.get("href", "")
        if href:
            actuate = _attr(proc_el, "actuate", _XLINK_NS) or "onRequest"
            proc_path = _resolve_xios_uri(path, href, host_root)
            proc_nid = _xios_file_nid(proc_path, host_root)
            add_edge(app_nid, proc_nid, "references", 1,
                     context=f"registers_process({actuate})",
                     target_file=str(proc_path))
        elif proc_name:
            inline_proc_nid = _make_id(stem, "proc", proc_name)
            add_node(inline_proc_nid, proc_name, 1, node_kind="process",
                     extra={"_xios_proc_name": proc_name, "_xios_stem": stem, "_xios_file": str_path})
            add_edge(app_nid, inline_proc_nid, "contains", 1, context="declares_process")
            _extract_process_body(path, proc_el, stem, str_path, inline_proc_nid,
                                  nodes, edges, seen_ids, lines=None,
                                  add_node=add_node, add_edge=add_edge,
                                  host_root=host_root)

    return {"nodes": nodes, "edges": edges}


# ── Process ─────────────────────────────────────────────────────────────────

def _extract_process(path: Path, root: ET.Element, raw: bytes, host_root: Path | None = None) -> dict:
    stem = _file_stem(path)
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()

    lines = raw.decode("utf-8", errors="replace").splitlines()

    def add_node(nid, label, line, file_type="code", node_kind="function", extra=None):
        if nid and nid not in seen_ids:
            seen_ids.add(nid)
            node = {"id": nid, "label": label, "file_type": file_type,
                    "node_kind": node_kind,
                    "source_file": str_path, "source_location": f"L{line}"}
            if extra:
                node.update(extra)
            nodes.append(node)

    seen_edge_keys: set[tuple] = set()

    def add_edge(src, tgt, relation, line, confidence="EXTRACTED",
                 weight=1.0, context=None, target_file=None):
        if not src or not tgt or src == tgt:
            return
        edge_key = (src, tgt, relation, context)
        if edge_key in seen_edge_keys:
            return
        seen_edge_keys.add(edge_key)
        edge = {"source": src, "target": tgt, "relation": relation,
                "confidence": confidence, "source_file": str_path,
                "source_location": f"L{line}", "weight": weight}
        if context:
            edge["context"] = context
        if target_file:
            edge["target_file"] = target_file
        edges.append(edge)

    proc_name = root.get("name", path.stem)
    proc_nid = _make_id(str(path))
    add_node(proc_nid, proc_name, 1, node_kind="process",
             extra={"_xios_proc_name": proc_name, "_xios_stem": stem, "_xios_file": str_path})

    _extract_process_body(path, root, stem, str_path, proc_nid,
                          nodes, edges, seen_ids, lines,
                          add_node=add_node, add_edge=add_edge,
                          host_root=host_root)

    return {"nodes": nodes, "edges": edges}


def _extract_process_body(path: Path, root: ET.Element, stem: str, str_path: str,
                          proc_nid: str, nodes: list[dict], edges: list[dict],
                          seen_ids: set[str], lines: list[str] | None,
                          add_node: Callable, add_edge: Callable,
                          host_root: Path | None = None) -> None:
    """Extract operations, triggers, steps, and flows for a process."""

    def _approx_line(el: ET.Element, attr_hint: str = "") -> int:
        if not lines:
            return 1
        tag = _strip_ns(el.tag)
        hint = attr_hint or el.get("id", "") or el.get("name", "") or tag
        for i, line in enumerate(lines, 1):
            if hint and hint in line and f"<{tag}" in line.lower():
                return i
        for i, line in enumerate(lines, 1):
            if hint and hint in line:
                return i
        return 1

    # Extract script href imports
    for sc in root.iter("script"):
        s_href = _attr(sc, "href", _XLINK_NS) or sc.get("href", "")
        if s_href and not s_href.startswith("http"):
            s_path = _resolve_xios_uri(path, s_href, host_root)
            s_nid = _xios_file_nid(s_path, host_root)
            add_edge(proc_nid, s_nid, "imports", 1,
                     context="imports_script", target_file=str(s_path))

    # Custom operations declared with href at process level
    for op_el in root.iter("operation"):
        op_href = _attr(op_el, "href", _XLINK_NS) or op_el.get("href", "")
        if op_href:
            op_name = op_el.get("name", "")
            op_path = _resolve_xios_uri(path, op_href, host_root)
            op_nid = _xios_file_nid(op_path, host_root)
            add_edge(proc_nid, op_nid, "imports", 1,
                     context=f"declares_operation({op_name})" if op_name else "declares_operation",
                     target_file=str(op_path))

    # Triggers → connect process to views/components/events
    for trigger in root.iter("trigger"):
        t_view = trigger.get("view", "")
        t_comp = trigger.get("component", "")
        t_event = trigger.get("event", "")
        t_step = trigger.get("step", "")
        t_comm = trigger.get("communicator", "")
        line = _approx_line(trigger, t_step or t_event)

        label_parts = []
        if t_view:
            label_parts.append(f"view={t_view}")
        if t_comp:
            label_parts.append(f"comp={t_comp}")
        if t_comm:
            label_parts.append(f"comm={t_comm}")
        label_parts.append(f"event={t_event}")

        trigger_nid = _make_id(stem, "trigger", t_view or t_comm, t_comp, t_event)
        add_node(trigger_nid, f"on {t_event}({', '.join(label_parts)})", line,
                 node_kind="trigger",
                 extra={
                     "_xios_trigger": True,
                     "_xios_target_view": t_view,
                     "_xios_target_comp": t_comp,
                     "_xios_event": t_event,
                     "_xios_step": t_step,
                     "_xios_proc_nid": proc_nid,
                 })
        add_edge(proc_nid, trigger_nid, "contains", line)

        # Edge from trigger to the step it fires
        if t_step:
            step_nid = _make_id(stem, "step", t_step)
            add_edge(trigger_nid, step_nid, "calls", line,
                     context="trigger_fires_step")

    # Steps
    for step_el in root.iter("step"):
        step_id = step_el.get("id", "")
        step_name = step_el.get("name", step_id)
        error_step = step_el.get("error", "")
        if not step_id:
            continue

        step_nid = _make_id(stem, "step", step_id)
        line = _approx_line(step_el, step_id)
        add_node(step_nid, f"{step_id}: {step_name}", line, node_kind="step",
                 extra={"_xios_step_id": step_id, "_xios_proc_nid": proc_nid})
        add_edge(proc_nid, step_nid, "contains", line)

        # Error handler step reference
        if error_step:
            error_nid = _make_id(stem, "step", error_step)
            add_edge(step_nid, error_nid, "references", line,
                     context="error_handler")

        # Walk operations inside the step
        _walk_step_operations(step_el, step_nid, proc_nid, stem, str_path, path,
                              nodes, edges, seen_ids, lines, add_node, add_edge,
                              host_root=host_root)


def _walk_step_operations(step_el, step_nid, proc_nid, stem, str_path, file_path,
                           nodes, edges, seen_ids, lines, add_node, add_edge,
                           host_root: Path | None = None):
    """Extract operation references inside a step."""

    def _approx_line_op(hint: str) -> int:
        if not lines:
            return 1
        for i, line in enumerate(lines, 1):
            if hint and hint in line:
                return i
        return 1

    for op in step_el.iter("operation"):
        op_name = op.get("name", "")
        op_value = op.get("value", "")
        line = _approx_line_op(op_name or op_value or "operation")

        if op_name == "call":
            # Calls another step
            if op_value:
                target_nid = _make_id(stem, "step", op_value)
                add_edge(step_nid, target_nid, "calls", line,
                         context="step_call")

        elif op_name == "callMethod":
            # Calls a method on a component: value="#View#Component"
            if op_value:
                parts = op_value.strip("#").split("#")
                v_part = parts[0] if len(parts) >= 2 else ""
                c_part = parts[1] if len(parts) >= 2 else (parts[0] if parts else "")
                method_el = op.find("method")
                method_name = method_el.get("name", "") if method_el is not None else ""
                comp_label = op_value
                if method_name:
                    comp_label = f"{op_value}.{method_name}()"

                ref_nid = _make_id(stem, "methodcall", op_value.replace("#", "_"),
                                   method_name)
                add_node(ref_nid, comp_label, line, node_kind="method_call",
                         extra={
                             "_xios_op": "callMethod",
                             "_xios_call_view": v_part,
                             "_xios_call_comp": c_part,
                             "_xios_method": method_name,
                             "_xios_step_nid": step_nid,
                             "_xios_proc_nid": proc_nid,
                         })
                add_edge(step_nid, ref_nid, "calls", line,
                         context="callMethod", confidence="EXTRACTED")

        elif op_name == "open":
            # Opens a view or process
            if op_value:
                clean_val = op_value.strip()
                open_nid = _make_id(stem, "open", clean_val.replace("://", "_").replace("/", "_").replace("#", "_"))
                add_node(open_nid, f"open({clean_val})", line, node_kind="view_action",
                         extra={
                             "_xios_op": "open",
                             "_xios_target": clean_val,
                             "_xios_step_nid": step_nid,
                             "_xios_proc_nid": proc_nid,
                         })
                add_edge(step_nid, open_nid, "references", line, context="opens_view")

                if clean_val.endswith(".xml") or clean_val.startswith("host://") or clean_val.startswith("xios://"):
                    view_path = _resolve_xios_uri(file_path, clean_val, host_root)
                    view_nid = _xios_file_nid(view_path, host_root)
                    add_edge(step_nid, view_nid, "references", line,
                             context="opens_view", target_file=str(view_path))

        elif op_name == "close":
            # Closes a view
            if op_value:
                close_nid = _make_id(stem, "close", op_value)
                add_node(close_nid, f"close({op_value})", line,
                         node_kind="view_action",
                         extra={
                             "_xios_op": "close",
                             "_xios_target_view": op_value,
                             "_xios_step_nid": step_nid,
                             "_xios_proc_nid": proc_nid,
                         })
                add_edge(step_nid, close_nid, "references", line,
                         context="closes_view")

        elif op_name == "bind":
            # Data binding
            if op_value:
                bind_comps = []
                for comp_el in op:
                    if _strip_ns(comp_el.tag) == "component":
                        c_name = comp_el.get("name", "")
                        c_view = comp_el.get("view", "")
                        if c_name:
                            bind_comps.append((c_view, c_name))
                bind_nid = _make_id(stem, "bind", op_value.replace("#", "_").replace("$", ""))
                add_node(bind_nid, f"bind({op_value})", line,
                         node_kind="data_binding",
                         extra={
                             "_xios_op": "bind",
                             "_xios_bind_comps": bind_comps,
                             "_xios_step_nid": step_nid,
                             "_xios_proc_nid": proc_nid,
                         })
                add_edge(step_nid, bind_nid, "references", line,
                         context="binds_data")

                if op_value.endswith(".xml") or op_value.startswith("host://") or op_value.startswith("xios://"):
                    bind_path = _resolve_xios_uri(file_path, op_value, host_root)
                    bind_doc_nid = _xios_file_nid(bind_path, host_root)
                    add_edge(step_nid, bind_doc_nid, "references", line,
                             context="binds_document", target_file=str(bind_path))

        elif op_name == "action":
            # UI actions on components in a view
            if op_value:
                for comp_el in op:
                    if _strip_ns(comp_el.tag) == "component":
                        comp_name = comp_el.get("name", "")
                        comp_action = comp_el.get("action", "")
                        if comp_name and comp_action:
                            action_nid = _make_id(
                                stem, "action", op_value.replace("#", "_"),
                                comp_name, comp_action)
                            add_node(action_nid,
                                     f"{op_value}.{comp_name}.{comp_action}()",
                                     line, node_kind="ui_action",
                                     extra={
                                         "_xios_op": "action",
                                         "_xios_action_view": op_value,
                                         "_xios_action_comp": comp_name,
                                         "_xios_action_act": comp_action,
                                         "_xios_step_nid": step_nid,
                                         "_xios_proc_nid": proc_nid,
                                     })
                            add_edge(step_nid, action_nid, "references", line,
                                     context=f"action:{comp_action}")

        elif op_name in ("change", "save", "database"):
            # Data mutation / persistence
            if op_value:
                data_nid = _make_id(stem, "data", op_value.replace("://", "_")
                                    .replace("/", "_"))
                add_node(data_nid, op_value, line, node_kind="data_document",
                         file_type="concept")
                add_edge(step_nid, data_nid, "references", line,
                         context=f"{op_name}_document")

                if op_value.endswith(".xml") or op_value.startswith("host://") or op_value.startswith("xios://"):
                    doc_path = _resolve_xios_uri(file_path, op_value, host_root)
                    doc_nid = _xios_file_nid(doc_path, host_root)
                    add_edge(step_nid, doc_nid, "references", line,
                             context=f"{op_name}_document", target_file=str(doc_path))

        elif op_name == "decision":
            # Decision branches — walk when/otherwise for nested step refs
            for when in op.iter("when"):
                when_step = when.get("step", "")
                if when_step:
                    target_nid = _make_id(stem, "step", when_step)
                    add_edge(step_nid, target_nid, "calls", line,
                             context="decision_branch")

        elif op_name == "setSelection":
            pass  # UI state management — no structural edge needed

        elif op_name in ("tenant", "system", "confirm"):
            # Platform operations — mint a concept node
            platform_nid = _make_id(stem, "platform", op_name)
            add_node(platform_nid, f"platform:{op_name}", line,
                     node_kind="platform_operation", file_type="concept")
            add_edge(step_nid, platform_nid, "references", line,
                     context=f"platform:{op_name}")

        # Named custom operations (declared in <operations> at top)
        elif op_name and op_name not in (
            "call", "callMethod", "open", "close", "bind", "action",
            "change", "save", "database", "decision", "setSelection",
            "tenant", "system", "confirm",
        ):
            custom_nid = _make_id(stem, "customop", op_name)
            add_node(custom_nid, f"op:{op_name}", line,
                     node_kind="custom_operation")
            add_edge(step_nid, custom_nid, "calls", line,
                     context="custom_operation")

    # Stores (XSLT transformer pipelines)
    for store_el in step_el.iter("store"):
        tf = store_el.get("transformer", "")
        if tf and (tf.startswith("host://") or tf.startswith("xios://") or tf.endswith(".xslt") or tf.endswith(".xsl")):
            tf_path = _resolve_xios_uri(file_path, tf, host_root)
            tf_nid = _xios_file_nid(tf_path, host_root)
            add_edge(step_nid, tf_nid, "references", 1, context="transforms_with", target_file=str(tf_path))

    # Aliases (process variable assignments referencing views/resources)
    for alias_el in step_el.iter("alias"):
        alias_value = alias_el.get("value", "")
        refs = re.findall(r'#(\w+)#(\w+)', alias_value)
        for view_name, comp_name in refs:
            ref_nid = _make_id(stem, "ref", view_name, comp_name)
            add_node(ref_nid, f"#{view_name}#{comp_name}", 1,
                     node_kind="component_reference",
                     extra={
                         "_xios_op": "alias",
                         "_xios_ref_view": view_name,
                         "_xios_ref_comp": comp_name,
                         "_xios_step_nid": step_nid,
                         "_xios_proc_nid": proc_nid,
                     })
            add_edge(step_nid, ref_nid, "references", 1,
                     context="alias_reads_component",
                     confidence="INFERRED")
        m_uri = re.search(r'(?:host|xios)://[^\s\}\"\']+', alias_value)
        if m_uri:
            a_path = _resolve_xios_uri(file_path, m_uri.group(0), host_root)
            a_nid = _xios_file_nid(a_path, host_root)
            add_edge(step_nid, a_nid, "references", 1, context="alias_resource", target_file=str(a_path))

    # Listen elements (event listeners on data documents)
    for listen_el in step_el.iter("listen"):
        listen_step = listen_el.get("step", "")
        if listen_step:
            target_nid = _make_id(stem, "step", listen_step)
            add_edge(step_nid, target_nid, "calls", 1,
                     context="listen_fires_step")


# ── View ────────────────────────────────────────────────────────────────────

def _extract_view(path: Path, root: ET.Element, raw: bytes, host_root: Path | None = None) -> dict:
    stem = _file_stem(path)
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()

    lines = raw.decode("utf-8", errors="replace").splitlines()

    def _approx_line(hint: str) -> int:
        for i, line in enumerate(lines, 1):
            if hint and hint in line:
                return i
        return 1

    def add_node(nid, label, line, file_type="code", node_kind="component", extra=None):
        if nid and nid not in seen_ids:
            seen_ids.add(nid)
            node = {"id": nid, "label": label, "file_type": file_type,
                    "node_kind": node_kind,
                    "source_file": str_path, "source_location": f"L{line}"}
            if extra:
                node.update(extra)
            nodes.append(node)

    seen_edge_keys: set[tuple] = set()

    def add_edge(src, tgt, relation, line, confidence="EXTRACTED",
                 weight=1.0, context=None, target_file=None):
        if not src or not tgt or src == tgt:
            return
        edge_key = (src, tgt, relation, context)
        if edge_key in seen_edge_keys:
            return
        seen_edge_keys.add(edge_key)
        edge = {"source": src, "target": tgt, "relation": relation,
                "confidence": confidence, "source_file": str_path,
                "source_location": f"L{line}", "weight": weight}
        if context:
            edge["context"] = context
        if target_file:
            edge["target_file"] = target_file
        edges.append(edge)

    # View node
    view_name = root.get("name", path.stem)
    view_nid = _make_id(str(path))
    add_node(view_nid, view_name, 1, node_kind="view",
             extra={"_xios_view_name": view_name, "_xios_stem": stem, "_xios_file": str_path})

    # Extract script href imports
    for sc in root.iter("script"):
        s_href = _attr(sc, "href", _XLINK_NS) or sc.get("href", "")
        if s_href and not s_href.startswith("http"):
            s_path = _resolve_xios_uri(path, s_href, host_root)
            s_nid = _xios_file_nid(s_path, host_root)
            line = _approx_line(s_href)
            add_edge(view_nid, s_nid, "imports", line,
                     context="imports_script", target_file=str(s_path))

    def walk_view(el: ET.Element, parent_nid: str, depth: int = 0) -> None:
        if depth > 20:
            return
        for child in el:
            tag = _strip_ns(child.tag).lower()
            name = child.get("name", "") or child.get("id", "")
            if not name and tag in _CUSTOM_TAGS:
                name = child.get("pipeline", "") or child.get("diagram", "") or child.get("plotly", "") or f"{tag}_{_approx_line(tag)}"

            # Check for xlink / href / source embedded references
            sub_href = _attr(child, "href", _XLINK_NS) or child.get("href", "") or child.get("source", "")
            if sub_href and not sub_href.startswith("http"):
                clean_ext = Path(sub_href.split("?")[0]).suffix.lower()
                if clean_ext in (".xml", ".xsl", ".xslt", ".js"):
                    sub_path = _resolve_xios_uri(path, sub_href, host_root)
                    sub_nid = _xios_file_nid(sub_path, host_root)
                    line = _approx_line(sub_href)
                    ctx = "embeds_view" if clean_ext == ".xml" else ("skinned_by" if clean_ext in (".xsl", ".xslt") else "imports_script")
                    rel = "imports" if clean_ext == ".js" else "references"
                    add_edge(view_nid, sub_nid, rel, line,
                             context=ctx, target_file=str(sub_path))
                    if parent_nid != view_nid:
                        add_edge(parent_nid, sub_nid, rel, line,
                                 context=ctx, target_file=str(sub_path))

            if tag == "view":
                # Nested view declaration
                nested_name = child.get("name", "") or child.get("id", "")
                if nested_name:
                    nested_nid = _make_id(stem, "view", nested_name)
                    line = _approx_line(nested_name)
                    add_node(nested_nid, nested_name, line, node_kind="view",
                             extra={"_xios_view_name": nested_name, "_xios_stem": stem, "_xios_file": str_path})
                    add_edge(parent_nid, nested_nid, "contains", line)
                    walk_view(child, nested_nid, depth + 1)
                else:
                    walk_view(child, parent_nid, depth + 1)
            elif name and tag not in _NON_COMPONENT_TAGS:
                line = _approx_line(name)
                comp_nid = _make_id(stem, tag, name)
                is_custom = tag in _CUSTOM_TAGS
                comp_label = f"{name}" if is_custom else f"{tag}:{name}"
                add_node(comp_nid, comp_label, line,
                         node_kind="custom_component" if is_custom else "ui_element",
                         extra={
                             "_xios_comp_name": name,
                             "_xios_view_name": view_name,
                             "_xios_view_nid": view_nid,
                             "_xios_tag": tag,
                         })
                add_edge(parent_nid, comp_nid, "contains", line)

                if is_custom:
                    def_nid = _make_id(stem, "compdef", tag)
                    add_node(def_nid, f"<{tag}>", line,
                             node_kind="component_tag", file_type="concept")
                    add_edge(comp_nid, def_nid, "references", line,
                             context="instantiates_component",
                             confidence="INFERRED")

                walk_view(child, comp_nid, depth + 1)
            else:
                walk_view(child, parent_nid, depth + 1)

    walk_view(root, view_nid)

    return {"nodes": nodes, "edges": edges}


# ── Cross-File Resolution ───────────────────────────────────────────────────

def resolve_xios_view_process_links(
    _per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]
) -> None:
    """Connect XIOS triggers, steps, and operations to target views and components across files."""
    has_xios = any(
        n.get("node_kind") in ("view", "process", "trigger") or "_xios_trigger" in n
        for n in all_nodes
    )
    if not has_xios:
        return

    existing_edges = {(e["source"], e["target"], e.get("relation")): e for e in all_edges}

    node_by_id: dict[str, dict] = {n["id"]: n for n in all_nodes}

    def add_edge(src: str, tgt: str, relation: str, line: int = 1,
                 context: str | None = None, weight: float = 1.0,
                 confidence: str = "EXTRACTED", source_file: str | None = None,
                 target_file: str | None = None) -> None:
        if not src or not tgt or src == tgt:
            return
        key = (src, tgt, relation)
        if key in existing_edges:
            return
        sf = source_file or node_by_id.get(src, {}).get("source_file", "")
        edge = {
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": confidence,
            "source_file": sf,
            "source_location": f"L{line}",
            "weight": weight,
        }
        if context:
            edge["context"] = context
        if target_file:
            edge["target_file"] = target_file
        existing_edges[key] = edge
        all_edges.append(edge)

    # 0. Detect host_root and parse index.html if present
    host_root: Path | None = None
    for n in all_nodes:
        sf = n.get("source_file")
        if sf:
            cand = _find_xios_root(Path(sf))
            if (cand / "index.html").is_file():
                host_root = cand
                break
    if host_root is None:
        cand = Path.cwd()
        if (cand / "index.html").is_file():
            host_root = cand
        else:
            cand_xios = _find_xios_root(cand)
            if (cand_xios / "index.html").is_file():
                host_root = cand_xios

    comp_def_impl_js: dict[str, str] = {}

    if host_root and (host_root / "index.html").is_file():
        index_file = host_root / "index.html"
        try:
            html_text = index_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            html_text = ""

        index_nid = None
        for n in all_nodes:
            sf = n.get("source_file", "")
            if sf and Path(sf).resolve() == index_file.resolve():
                index_nid = n["id"]
                break
            if n.get("label") in ("index.html", "index.xml", "index") and n.get("node_kind") in ("file", "application"):
                index_nid = n["id"]
                break

        if not index_nid:
            index_nid = "index" if "index" in node_by_id else _xios_file_nid(index_file, host_root)
        if index_nid not in node_by_id:
            idx_node = {
                "id": index_nid,
                "label": "index.html",
                "file_type": "code",
                "node_kind": "file",
                "source_file": str(index_file),
                "source_location": "L1",
            }
            node_by_id[index_nid] = idx_node
            all_nodes.append(idx_node)

        # Boot startup / system links
        m_startup = re.search(r'startup\s*:\s*["\']([^"\']+)["\']', html_text)
        if m_startup:
            s_path = _resolve_xios_uri(index_file, m_startup.group(1), host_root)
            s_nid = _xios_file_nid(s_path, host_root)
            add_edge(index_nid, s_nid, "references", line=1, context="boot_startup", target_file=str(s_path))
        m_system = re.search(r'system\s*:\s*["\']([^"\']+)["\']', html_text)
        if m_system:
            sys_path = _resolve_xios_uri(index_file, m_system.group(1), host_root)
            sys_nid = _xios_file_nid(sys_path, host_root)
            add_edge(index_nid, sys_nid, "references", line=1, context="boot_system", target_file=str(sys_path))

        script_match = re.search(
            r'<script\b[^>]*\btype=["\']xios["\'][^>]*\bname=["\']addConfig["\'][^>]*>(.*?)</script>',
            html_text,
            re.DOTALL | re.IGNORECASE,
        ) or re.search(
            r'<script\b[^>]*\bname=["\']addConfig["\'][^>]*\btype=["\']xios["\'][^>]*>(.*?)</script>',
            html_text,
            re.DOTALL | re.IGNORECASE,
        )

        if script_match:
            config_xml = f"<root>{script_match.group(1)}</root>"
            try:
                cfg_root = ET.fromstring(config_xml)
            except ET.ParseError:
                cfg_root = None

            if cfg_root is not None:
                stem = _file_stem(index_file)
                for comp in cfg_root.iter("component"):
                    comp_name = comp.get("name", "")
                    comp_class = comp.get("class", "")
                    comp_tag = comp.get("tag", "")
                    comp_href = comp.get("href", "")
                    if not comp_name and not comp_tag:
                        continue

                    comp_nid = _make_id(stem, "comp", comp_name or comp_tag)
                    label = f"{comp_name} <{comp_tag}>" if comp_tag and comp_name else (comp_name or comp_tag)
                    if comp_nid not in node_by_id:
                        c_node = {
                            "id": comp_nid,
                            "label": label,
                            "file_type": "code",
                            "node_kind": "component_definition",
                            "source_file": str(index_file),
                            "source_location": "L1",
                            "tag": comp_tag,
                            "class_name": comp_class,
                            "_xios_comp_def": True,
                        }
                        node_by_id[comp_nid] = c_node
                        all_nodes.append(c_node)

                    if index_nid:
                        add_edge(index_nid, comp_nid, "contains", line=1,
                                 context="declares_component", source_file=str(index_file))

                    if comp_href:
                        js_path = _resolve_xios_uri(index_file, comp_href, host_root)
                        js_nid = _xios_file_nid(js_path, host_root)
                        add_edge(comp_nid, js_nid, "references", line=1,
                                 context="implemented_by", source_file=str(index_file),
                                 target_file=str(js_path))
                        comp_def_impl_js[comp_nid] = js_nid

                    for dep in comp.iter("dependency"):
                        dep_href = dep.get("href", "")
                        dep_sym = dep.get("symbol", "")
                        if dep_href:
                            dep_path = _resolve_xios_uri(index_file, dep_href, host_root)
                            dep_nid = _xios_file_nid(dep_path, host_root)
                            ctx = f"depends_on({dep_sym})" if dep_sym else "depends_on"
                            add_edge(comp_nid, dep_nid, "references", line=1,
                                     context=ctx, source_file=str(index_file),
                                     target_file=str(dep_path))

                    for skin in comp.iter("skin"):
                        skin_href = skin.get("href", "")
                        if skin_href:
                            skin_path = _resolve_xios_uri(index_file, skin_href, host_root)
                            skin_nid = _xios_file_nid(skin_path, host_root)
                            add_edge(comp_nid, skin_nid, "references", line=1,
                                     context="skinned_by", source_file=str(index_file),
                                     target_file=str(skin_path))

                    for style in comp.iter("style"):
                        style_href = style.get("href", "")
                        if style_href:
                            style_path = _resolve_xios_uri(index_file, style_href, host_root)
                            style_nid = _xios_file_nid(style_path, host_root)
                            add_edge(comp_nid, style_nid, "references", line=1,
                                     context="styled_by", source_file=str(index_file),
                                     target_file=str(style_path))

    # Published application package linking
    if host_root and (host_root / "published" / "index.html").is_file():
        pub_idx_file = host_root / "published" / "index.html"
        pub_idx_nid = "published_index" if "published_index" in node_by_id else _xios_file_nid(pub_idx_file, host_root)
        if pub_idx_nid not in node_by_id:
            p_node = {
                "id": pub_idx_nid,
                "label": "published/index.html",
                "file_type": "code",
                "node_kind": "file",
                "source_file": str(pub_idx_file),
                "source_location": "L1",
            }
            node_by_id[pub_idx_nid] = p_node
            all_nodes.append(p_node)

        pub_xml = host_root / "published" / "published.xml"
        if pub_xml.is_file():
            pub_nid = _xios_file_nid(pub_xml, host_root)
            add_edge(pub_idx_nid, pub_nid, "references", line=1,
                     context="boot_startup", target_file=str(pub_xml))

        sys_cbe = host_root / "config" / "system_cbe.xml"
        if sys_cbe.is_file():
            sys_nid = _xios_file_nid(sys_cbe, host_root)
            add_edge(pub_idx_nid, sys_nid, "references", line=1,
                     context="boot_system", target_file=str(sys_cbe))

    # 1. Build View indexes
    views_by_name: dict[str, list[str]] = {}
    views_by_lower: dict[str, list[str]] = {}
    views_by_stem: dict[str, list[str]] = {}
    views_by_source_file: dict[str, str] = {}
    manifest_mounts: dict[str, list[str]] = {}

    for n in all_nodes:
        if n.get("node_kind") == "view":
            nid = n["id"]
            vname = n.get("_xios_view_name") or n.get("label", "")
            sfile = n.get("source_file", "")
            stem = Path(sfile).stem if sfile else ""

            if sfile and sfile not in views_by_source_file:
                views_by_source_file[sfile] = nid
            if vname:
                views_by_name.setdefault(vname, []).append(nid)
                views_by_lower.setdefault(vname.lower(), []).append(nid)
            if stem:
                views_by_stem.setdefault(stem.lower(), []).append(nid)
                views_by_lower.setdefault(stem.lower(), []).append(nid)

    # Index manifest mounts and embedded view links
    for e in all_edges:
        ctx = e.get("context", "")
        if "mounts_view" in ctx or "embeds_view" in ctx:
            manifest_mounts.setdefault(e["source"], []).append(e["target"])

    def resolve_view(target_str: str) -> list[str]:
        if not target_str:
            return []
        clean = target_str.strip("#").strip()
        clean = re.sub(r'^(host|xios)://', '', clean).lstrip('/\\')
        clean_norm = os.path.normpath(clean).replace('\\', '/').lower()
        for sf, v_nid in views_by_source_file.items():
            sf_norm = os.path.normpath(sf).replace('\\', '/').lower()
            if sf_norm.endswith(clean_norm) or clean_norm.endswith(sf_norm):
                return [v_nid]
        if clean.endswith(".xml"):
            stem = Path(clean).stem.lower()
            if stem in views_by_stem:
                return views_by_stem[stem]
        lower = clean.lower()
        if lower in views_by_lower:
            return views_by_lower[lower]
        if lower.endswith("view"):
            cand = lower[:-4]
            if cand in views_by_stem:
                return views_by_stem[cand]
            if cand in views_by_lower:
                return views_by_lower[cand]
        stem = Path(clean).stem.lower()
        if stem in views_by_stem:
            return views_by_stem[stem]
        if lower in views_by_stem:
            return views_by_stem[lower]
        return []

    # 2. Build Component indexes
    comps_by_view_nid: dict[tuple[str, str], str] = {}
    comps_by_view_name: dict[tuple[str, str], list[str]] = {}
    comps_by_stem: dict[tuple[str, str], list[str]] = {}
    comps_by_name: dict[str, list[str]] = {}

    view_ids = {n["id"] for n in all_nodes if n.get("node_kind") == "view"}
    comp_parent_view: dict[str, str] = {}
    for e in all_edges:
        if e.get("relation") == "contains" and e["source"] in view_ids:
            comp_parent_view[e["target"]] = e["source"]

    for n in all_nodes:
        if n.get("node_kind") in ("component", "ui_element", "custom_component") or "_xios_comp_name" in n:
            nid = n["id"]
            cname = n.get("_xios_comp_name")
            if not cname:
                lbl = n.get("label", "")
                cname = lbl.split(":")[-1] if ":" in lbl else lbl
            c_lower = cname.lower()
            comps_by_name.setdefault(c_lower, []).append(nid)

            v_nid = views_by_source_file.get(n.get("source_file", "")) or comp_parent_view.get(nid)
            v_name = n.get("_xios_view_name")
            sfile = n.get("source_file", "")
            stem = Path(sfile).stem if sfile else ""

            if v_nid:
                comps_by_view_nid[(v_nid, c_lower)] = nid
            if v_name:
                comps_by_view_name.setdefault((v_name.lower(), c_lower), []).append(nid)
            if stem:
                comps_by_stem.setdefault((stem.lower(), c_lower), []).append(nid)

    def resolve_comp(target_view: str, target_comp: str) -> str | None:
        if not target_comp:
            return None
        c_lower = target_comp.lower()
        v_nids = resolve_view(target_view) if target_view else []
        for vn in v_nids:
            if (vn, c_lower) in comps_by_view_nid:
                return comps_by_view_nid[(vn, c_lower)]
            for m_vn in manifest_mounts.get(vn, []):
                if (m_vn, c_lower) in comps_by_view_nid:
                    return comps_by_view_nid[(m_vn, c_lower)]

        if target_view:
            tv_lower = target_view.strip("#").lower()
            if (tv_lower, c_lower) in comps_by_view_name:
                return comps_by_view_name[(tv_lower, c_lower)][0]
            if tv_lower.endswith("view") and (tv_lower[:-4], c_lower) in comps_by_view_name:
                return comps_by_view_name[(tv_lower[:-4], c_lower)][0]
            if tv_lower.endswith("view") and (tv_lower[:-4], c_lower) in comps_by_stem:
                return comps_by_stem[(tv_lower[:-4], c_lower)][0]
            if (tv_lower, c_lower) in comps_by_stem:
                return comps_by_stem[(tv_lower, c_lower)][0]

        if c_lower in comps_by_name and len(comps_by_name[c_lower]) == 1:
            return comps_by_name[c_lower][0]

        return None

    # 3. Process indexing
    procs_by_stem: dict[str, str] = {}
    procs_by_name: dict[str, str] = {}
    procs_by_source_file: dict[str, str] = {}
    for n in all_nodes:
        if n.get("node_kind") == "process" or "_xios_proc_name" in n:
            nid = n["id"]
            pname = n.get("_xios_proc_name") or n.get("label", "")
            sfile = n.get("source_file", "")
            stem = Path(sfile).stem if sfile else ""
            if sfile:
                procs_by_source_file[sfile] = nid
            if pname:
                procs_by_name[pname.lower()] = nid
            if stem:
                procs_by_stem[stem.lower()] = nid

    # Find step -> proc and trigger -> proc
    step_to_proc: dict[str, str] = {}
    trigger_to_proc: dict[str, str] = {}
    proc_ids = {n["id"] for n in all_nodes if n.get("node_kind") == "process" or "_xios_proc_name" in n}

    for e in all_edges:
        if e.get("relation") == "contains" and e["source"] in proc_ids:
            tgt_node = node_by_id.get(e["target"])
            if tgt_node:
                if tgt_node.get("node_kind") == "step" or "_xios_step_id" in tgt_node:
                    step_to_proc[tgt_node["id"]] = e["source"]
                elif tgt_node.get("node_kind") == "trigger" or "_xios_trigger" in tgt_node:
                    trigger_to_proc[tgt_node["id"]] = e["source"]

    op_to_step: dict[str, str] = {}
    for e in all_edges:
        if e.get("relation") in ("references", "calls"):
            src_node = node_by_id.get(e["source"])
            if src_node and src_node.get("node_kind") == "step":
                tgt_node = node_by_id.get(e["target"])
                if tgt_node and tgt_node.get("node_kind") in (
                    "ui_action", "method_call", "view_action", "data_binding", "component_reference"
                ):
                    op_to_step[tgt_node["id"]] = src_node["id"]

    # 4. Connect Triggers
    for n in all_nodes:
        if n.get("node_kind") == "trigger" or n.get("_xios_trigger"):
            trig_nid = n["id"]
            lbl = n.get("label", "")
            t_view = n.get("_xios_target_view")
            t_comp = n.get("_xios_target_comp")

            if not t_view and "view=" in lbl:
                m = re.search(r'view=([^,\)]+)', lbl)
                if m:
                    t_view = m.group(1).strip()
            if not t_comp and "comp=" in lbl:
                m = re.search(r'comp=([^,\)]+)', lbl)
                if m:
                    t_comp = m.group(1).strip()

            proc_nid = (
                n.get("_xios_proc_nid")
                or trigger_to_proc.get(trig_nid)
                or procs_by_source_file.get(n.get("source_file", ""))
            )

            v_nids = resolve_view(t_view) if t_view else []
            for vn in v_nids:
                add_edge(trig_nid, vn, "references", context="listens_to", weight=0.8)
                if proc_nid:
                    add_edge(proc_nid, vn, "references", context="operates_on", weight=0.8)
                if not t_comp:
                    add_edge(vn, trig_nid, "calls", context="fires_trigger", weight=1.0)

            if t_comp:
                c_nid = resolve_comp(t_view, t_comp)
                if c_nid:
                    add_edge(c_nid, trig_nid, "calls", context="fires_trigger", weight=1.0)
                    add_edge(trig_nid, c_nid, "references", context="triggers_on", weight=0.8)
                    if proc_nid:
                        add_edge(proc_nid, c_nid, "references", context="handles_component", weight=0.8)

    # 5. Connect Operations
    for n in all_nodes:
        nid = n["id"]
        nkind = n.get("node_kind", "")
        step_nid = n.get("_xios_step_nid") or op_to_step.get(nid)
        proc_nid = (
            n.get("_xios_proc_nid")
            or (step_to_proc.get(step_nid) if step_nid else None)
            or procs_by_source_file.get(n.get("source_file", ""))
        )

        if nkind == "ui_action" or n.get("_xios_op") == "action":
            v_name = n.get("_xios_action_view")
            c_name = n.get("_xios_action_comp")
            act = n.get("_xios_action_act", "action")
            if not v_name or not c_name:
                m = re.match(r'#?([^.]+)\.([^.]+)\.([^\(]+)\(\)', n.get("label", ""))
                if m:
                    v_name, c_name, act = m.group(1), m.group(2), m.group(3)
            c_nid = resolve_comp(v_name, c_name)
            if c_nid:
                add_edge(nid, c_nid, "references", context="targets_component", weight=0.8)
                if step_nid:
                    add_edge(step_nid, c_nid, "calls", context=f"ui_action:{act}", weight=1.0)

        elif nkind == "method_call" or n.get("_xios_op") == "callMethod":
            v_name = n.get("_xios_call_view")
            c_name = n.get("_xios_call_comp")
            method = n.get("_xios_method", "")
            if not v_name or not c_name:
                m = re.match(r'#?([^#]+)#([^.\(]+)(?:\.([^\(]+)\(\))?', n.get("label", ""))
                if m:
                    v_name, c_name = m.group(1), m.group(2)
                    method = m.group(3) or ""
            c_nid = resolve_comp(v_name, c_name)
            if c_nid:
                add_edge(nid, c_nid, "references", context="targets_component", weight=0.8)
                if step_nid:
                    add_edge(step_nid, c_nid, "calls", context=f"callMethod:{method}", weight=1.0)

        elif nkind == "data_binding" or n.get("_xios_op") == "bind":
            bind_comps = n.get("_xios_bind_comps", [])
            for c_view, c_name in bind_comps:
                c_nid = resolve_comp(c_view, c_name)
                if c_nid:
                    add_edge(nid, c_nid, "references", context="targets_component", weight=0.8)
                    if step_nid:
                        add_edge(step_nid, c_nid, "references", context="binds_data", weight=0.8)

        elif nkind == "component_reference" or n.get("_xios_op") == "alias":
            v_name = n.get("_xios_ref_view")
            c_name = n.get("_xios_ref_comp")
            if not v_name or not c_name:
                m = re.match(r'#(\w+)#(\w+)', n.get("label", ""))
                if m:
                    v_name, c_name = m.group(1), m.group(2)
            c_nid = resolve_comp(v_name, c_name)
            if c_nid:
                add_edge(nid, c_nid, "references", context="targets_component", weight=0.8)
                if step_nid:
                    add_edge(step_nid, c_nid, "references", context="alias_reads_component", weight=0.8)

        elif nkind == "view_action" or n.get("_xios_op") in ("open", "close"):
            op_kind = n.get("_xios_op")
            target = n.get("_xios_target") or n.get("_xios_target_view")
            if not target:
                lbl = n.get("label", "")
                m = re.match(r'(?:open|close)\((.+)\)', lbl)
                if m:
                    target = m.group(1)
            if not op_kind:
                op_kind = "open" if "open(" in n.get("label", "") else "close"

            v_nids = resolve_view(target) if target else []
            for vn in v_nids:
                add_edge(nid, vn, "references", context=f"{op_kind}s_view", weight=0.8)
                if step_nid:
                    add_edge(step_nid, vn, "calls", context=f"{op_kind}s_view", weight=1.0)
                if proc_nid:
                    add_edge(proc_nid, vn, "references", context=f"{op_kind}s_view", weight=0.8)

            if target and not v_nids:
                clean_target = target.strip("#").strip()
                p_stem = Path(clean_target).stem.lower()
                target_proc = procs_by_stem.get(p_stem) or procs_by_name.get(clean_target.lower())
                if target_proc:
                    add_edge(nid, target_proc, "references", context=f"{op_kind}s_process", weight=0.8)
                    if step_nid:
                        add_edge(step_nid, target_proc, "calls", context=f"{op_kind}s_process", weight=1.0)

    # 6. Map custom components in views to their definitions and implementations
    for e in all_edges:
        if e.get("context") == "implemented_by" and e.get("relation") == "references":
            comp_def_impl_js[e["source"]] = e["target"]

    comp_defs_by_tag: dict[str, list[dict]] = {}
    comp_defs_by_name: dict[str, list[dict]] = {}
    comp_defs_by_class: dict[str, list[dict]] = {}

    for n in all_nodes:
        if n.get("node_kind") == "component_definition" or n.get("_xios_comp_def"):
            tag = (n.get("tag") or "").lower()
            cname = (n.get("label") or "").split("<")[0].strip().lower()
            ccls = (n.get("class_name") or "").lower()
            if tag:
                comp_defs_by_tag.setdefault(tag, []).append(n)
            if cname:
                comp_defs_by_name.setdefault(cname, []).append(n)
            if ccls:
                comp_defs_by_class.setdefault(ccls, []).append(n)

    for n in all_nodes:
        if n.get("node_kind") in ("custom_component", "ui_element") or "_xios_tag" in n:
            nid = n["id"]
            tag = (n.get("_xios_tag") or "").lower()
            cname = (n.get("_xios_comp_name") or n.get("label", "")).lower()

            matched_defs: list[dict] = []
            if tag and tag in comp_defs_by_tag:
                matched_defs.extend(comp_defs_by_tag[tag])
            elif cname and cname in comp_defs_by_name:
                matched_defs.extend(comp_defs_by_name[cname])
            elif tag and tag in comp_defs_by_class:
                matched_defs.extend(comp_defs_by_class[tag])

            for c_def in matched_defs:
                c_def_nid = c_def["id"]
                add_edge(nid, c_def_nid, "references", line=1, context="references")
                js_nid = comp_def_impl_js.get(c_def_nid)
                if js_nid:
                    add_edge(nid, js_nid, "references", line=1, context="implemented_by")

