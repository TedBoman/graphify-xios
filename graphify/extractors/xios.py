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

import re
import xml.etree.ElementTree as ET
from pathlib import Path

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


# ── Sniffers ────────────────────────────────────────────────────────────────

def is_xios_manifest(path: Path, head: bytes | None = None) -> bool:
    """True if *path* is an XIOS application manifest (index.xml)."""
    if path.name.lower() != "index.xml":
        return False
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

def extract_xios(path: Path) -> dict:
    """Extract structure from an XIOS XML file (manifest, process, or view)."""
    try:
        raw = _read_source_bytes(path)
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    if len(raw) > _XML_MAX_BYTES:
        return {"nodes": [], "edges": [], "error": "xios xml file too large to index"}
    if not _xml_is_safe(raw):
        return {"nodes": [], "edges": [], "error": "xios xml file contains entity declarations"}

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        return {"nodes": [], "edges": [], "error": f"xml parse error: {e}"}

    root_tag = _strip_ns(root.tag)

    if root_tag == _XIOS_APP_ROOT:
        return _extract_manifest(path, root, raw)
    elif root_tag == _XIOS_PROCESS_ROOT:
        return _extract_process(path, root, raw)
    elif root_tag == _XIOS_VIEW_ROOT:
        return _extract_view(path, root, raw)
    elif root_tag == "types":
        return _extract_types(path, root, raw)
    else:
        # Unknown XIOS XML shape — produce a bare file node
        file_nid = _make_id(str(path))
        return {
            "nodes": [{"id": file_nid, "label": path.name, "file_type": "code",
                        "source_file": str(path), "source_location": "L1"}],
            "edges": [],
        }


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

def _extract_manifest(path: Path, root: ET.Element, raw: bytes) -> dict:
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
                 extra={"tag": comp_tag, "class_name": comp_class})
        add_edge(app_nid, comp_nid, "contains", 1, context="declares_component")

        # Link to implementation JS
        if comp_href:
            js_path = path.parent / comp_href
            js_nid = _make_id(str(js_path))
            add_edge(comp_nid, js_nid, "references", 1,
                     context="implemented_by", target_file=str(js_path))

        # Link to skin XSL
        for skin in comp:
            if _strip_ns(skin.tag) == "skin":
                skin_href = skin.get("href", "")
                if skin_href:
                    skin_path = path.parent / skin_href
                    skin_nid = _make_id(str(skin_path))
                    add_edge(comp_nid, skin_nid, "references", 1,
                             context="skinned_by", target_file=str(skin_path))

        # Link to style CSS
        for style in comp:
            if _strip_ns(style.tag) == "style":
                style_href = style.get("href", "")
                if style_href:
                    style_path = path.parent / style_href
                    style_nid = _make_id(str(style_path))
                    add_edge(comp_nid, style_nid, "references", 1,
                             context="styled_by", target_file=str(style_path))

    # Resource declarations
    for res in root.iter("item"):
        res_name = res.get("name", "")
        if res_name:
            res_nid = _make_id(stem, "res", res_name)
            add_node(res_nid, f"#{res_name}", 1, node_kind="resource")
            add_edge(app_nid, res_nid, "contains", 1, context="declares_resource")

    # View xlink declarations
    for view_el in root.iter("view"):
        href = _attr(view_el, "href", _XLINK_NS)
        if href:
            actuate = _attr(view_el, "actuate", _XLINK_NS)
            view_path = path.parent / href
            view_nid = _make_id(str(view_path))
            add_edge(app_nid, view_nid, "references", 1,
                     context=f"mounts_view({actuate})",
                     target_file=str(view_path))

    # Process xlink declarations
    for proc_el in root.iter("process"):
        href = _attr(proc_el, "href", _XLINK_NS)
        if href:
            actuate = _attr(proc_el, "actuate", _XLINK_NS)
            proc_path = path.parent / href
            proc_nid = _make_id(str(proc_path))
            add_edge(app_nid, proc_nid, "references", 1,
                     context=f"registers_process({actuate})",
                     target_file=str(proc_path))

    return {"nodes": nodes, "edges": edges}


# ── Process ─────────────────────────────────────────────────────────────────

def _extract_process(path: Path, root: ET.Element, raw: bytes) -> dict:
    stem = _file_stem(path)
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()

    # Track line numbers via byte scanning for better source locations
    lines = raw.decode("utf-8", errors="replace").splitlines()
    # Build a quick lookup: element text snippet -> approximate line
    def _approx_line(el: ET.Element, attr_hint: str = "") -> int:
        """Best-effort line number from an element's text representation."""
        tag = _strip_ns(el.tag)
        hint = attr_hint or el.get("id", "") or el.get("name", "") or tag
        for i, line in enumerate(lines, 1):
            if hint and hint in line and f"<{tag}" in line.lower():
                return i
        for i, line in enumerate(lines, 1):
            if hint and hint in line:
                return i
        return 1

    def add_node(nid, label, line, file_type="code", node_kind="function", extra=None):
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

    # Process node
    proc_name = root.get("name", path.stem)
    proc_nid = _make_id(str(path))
    add_node(proc_nid, proc_name, 1, node_kind="process")

    # Custom operations declared at process level
    ops_section = root.find("operations")
    if ops_section is not None:
        for op_el in ops_section:
            if _strip_ns(op_el.tag) == "operation":
                op_name = op_el.get("name", "")
                op_href = op_el.get("href", "")
                if op_name and op_href:
                    op_path = path.parent / op_href
                    op_nid = _make_id(str(op_path))
                    add_edge(proc_nid, op_nid, "imports", 1,
                             context=f"declares_operation({op_name})",
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
                 node_kind="trigger")
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
        add_node(step_nid, f"{step_id}: {step_name}", line, node_kind="step")
        add_edge(proc_nid, step_nid, "contains", line)

        # Error handler step reference
        if error_step:
            error_nid = _make_id(stem, "step", error_step)
            add_edge(step_nid, error_nid, "references", line,
                     context="error_handler")

        # Walk operations inside the step
        _walk_step_operations(step_el, step_nid, stem, str_path, path,
                              nodes, edges, seen_ids, lines, add_node, add_edge)

    return {"nodes": nodes, "edges": edges}


def _walk_step_operations(step_el, step_nid, stem, str_path, file_path,
                           nodes, edges, seen_ids, lines, add_node, add_edge):
    """Extract operation references inside a step."""

    def _approx_line_op(hint: str) -> int:
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
                # Parse #View#Component pattern
                parts = op_value.strip("#").split("#")
                method_el = op.find("method")
                method_name = method_el.get("name", "") if method_el is not None else ""
                comp_label = op_value
                if method_name:
                    comp_label = f"{op_value}.{method_name}()"

                ref_nid = _make_id(stem, "methodcall", op_value.replace("#", "_"),
                                   method_name)
                add_node(ref_nid, comp_label, line, node_kind="method_call")
                add_edge(step_nid, ref_nid, "calls", line,
                         context="callMethod", confidence="EXTRACTED")

        elif op_name == "open":
            # Opens a view
            if op_value:
                view_path = file_path.parent / op_value
                view_nid = _make_id(str(view_path))
                add_edge(step_nid, view_nid, "references", line,
                         context="opens_view", target_file=str(view_path))

        elif op_name == "close":
            # Closes a view
            if op_value:
                close_nid = _make_id(stem, "close", op_value)
                add_node(close_nid, f"close({op_value})", line,
                         node_kind="view_action")
                add_edge(step_nid, close_nid, "references", line,
                         context="closes_view")

        elif op_name == "bind":
            # Data binding: value is a resource URI
            if op_value:
                bind_nid = _make_id(stem, "bind", op_value.replace("#", "_"))
                add_node(bind_nid, f"bind({op_value})", line,
                         node_kind="data_binding")
                add_edge(step_nid, bind_nid, "references", line,
                         context="binds_data")

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
                                     line, node_kind="ui_action")
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

    # Aliases (process variable assignments referencing views/resources)
    for alias_el in step_el.iter("alias"):
        alias_value = alias_el.get("value", "")
        # Extract view/component references from {#View#Component} patterns
        refs = re.findall(r'#(\w+)#(\w+)', alias_value)
        for view_name, comp_name in refs:
            ref_nid = _make_id(stem, "ref", view_name, comp_name)
            add_node(ref_nid, f"#{view_name}#{comp_name}", 1,
                     node_kind="component_reference")
            add_edge(step_nid, ref_nid, "references", 1,
                     context="alias_reads_component",
                     confidence="INFERRED")

    # Listen elements (event listeners on data documents)
    for listen_el in step_el.iter("listen"):
        listen_step = listen_el.get("step", "")
        if listen_step:
            target_nid = _make_id(stem, "step", listen_step)
            add_edge(step_nid, target_nid, "calls", 1,
                     context="listen_fires_step")


# ── View ────────────────────────────────────────────────────────────────────

def _extract_view(path: Path, root: ET.Element, raw: bytes) -> dict:
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

    # View node
    view_name = root.get("name", path.stem)
    view_nid = _make_id(str(path))
    add_node(view_nid, view_name, 1, node_kind="view")

    # Walk all elements looking for named components and xlink references
    _NAMED_TAGS = {
        "panel", "label", "button", "grid", "image", "group",
        "iconlist", "textfield", "checkbox", "slider", "progress",
        "scorechart", "sessioneditor", "sessiontrainer", "thememanager",
        "navmenu", "tutorial", "alarm", "statsboard", "prefseditor",
        "courseeditor",
    }

    # Known XIOS custom component tags (from index.xml declarations)
    _CUSTOM_TAGS = {
        "scorechart", "sessioneditor", "sessiontrainer", "thememanager",
        "navmenu", "tutorial", "alarm", "statsboard", "prefseditor",
        "courseeditor",
    }

    def walk_view(el: ET.Element, parent_nid: str, depth: int = 0) -> None:
        if depth > 20:
            return
        for child in el:
            tag = _strip_ns(child.tag).lower()
            name = child.get("name", "")

            # Check for xlink embedded sub-views (lazy-loaded panels)
            xlink_href = _attr(child, "href", _XLINK_NS)
            if xlink_href and xlink_href.endswith(".xml"):
                sub_path = path.parent / xlink_href
                sub_nid = _make_id(str(sub_path))
                line = _approx_line(xlink_href)
                add_edge(view_nid, sub_nid, "references", line,
                         context="embeds_view", target_file=str(sub_path))

            if tag in _NAMED_TAGS and name:
                line = _approx_line(name)
                comp_nid = _make_id(stem, tag, name)
                comp_label = f"{name}" if tag in _CUSTOM_TAGS else f"{tag}:{name}"
                add_node(comp_nid, comp_label, line,
                         node_kind="custom_component" if tag in _CUSTOM_TAGS else "ui_element")
                add_edge(parent_nid, comp_nid, "contains", line)

                # Custom component instantiation → link to definition
                if tag in _CUSTOM_TAGS:
                    # Emit INFERRED edge — the exact definition node is in the
                    # manifest which may be extracted separately
                    def_nid = _make_id(stem, "compdef", tag)
                    add_node(def_nid, f"<{tag}>", line,
                             node_kind="component_tag", file_type="concept")
                    add_edge(comp_nid, def_nid, "references", line,
                             context="instantiates_component",
                             confidence="INFERRED")

                walk_view(child, comp_nid, depth + 1)
            elif tag == "view":
                # Nested view declaration (rare)
                nested_name = child.get("name", "")
                if nested_name:
                    nested_nid = _make_id(stem, "view", nested_name)
                    line = _approx_line(nested_name)
                    add_node(nested_nid, nested_name, line, node_kind="view")
                    add_edge(parent_nid, nested_nid, "contains", line)
                    walk_view(child, nested_nid, depth + 1)
                else:
                    walk_view(child, parent_nid, depth + 1)
            else:
                walk_view(child, parent_nid, depth + 1)

    walk_view(root, view_nid)

    return {"nodes": nodes, "edges": edges}
