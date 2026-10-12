"""Unit tests for XIOS extractor and view-process cross-file resolution."""
from __future__ import annotations

import tempfile
from pathlib import Path

from graphify.extractors.xios import (
    extract_xios,
    extract_xios_xsl,
    is_xios_manifest,
    is_xios_process,
    is_xios_view,
    resolve_xios_view_process_links,
)


def test_xios_view_component_extraction():
    """Verify that any named element in a view is extracted as a component."""
    xml_content = """<?xml version="1.0" encoding="utf-8"?>
<view name="Projects" height="100%" width="100%">
  <panel name="content_projects" type="rows">
    <toolbar name="projBar">
      <buttonbox name="tCreateProj" text="New Project"/>
    </toolbar>
    <bush name="projectTree" wait="Please Wait..."/>
    <filelist name="projects_list" layout="miniature"/>
    <input name="searchBox" placeholder="Search..."/>
    <radio name="optSystem" value="system"/>
  </panel>
</view>"""
    with tempfile.TemporaryDirectory() as td:
        view_path = Path(td) / "views" / "projects.xml"
        view_path.parent.mkdir(parents=True)
        view_path.write_text(xml_content, encoding="utf-8")

        res = extract_xios(view_path)
        nodes = res["nodes"]
        edges = res["edges"]

        v_nodes = [n for n in nodes if n["node_kind"] == "view"]
        assert len(v_nodes) == 1
        assert v_nodes[0]["label"] == "Projects"

        comp_names = {n.get("_xios_comp_name") for n in nodes if "_xios_comp_name" in n}
        expected = {
            "content_projects", "projBar", "tCreateProj", "projectTree",
            "projects_list", "searchBox", "optSystem"
        }
        assert expected.issubset(comp_names), f"Missing components: {expected - comp_names}"

        # Check edge hierarchy
        contains_edges = [e for e in edges if e["relation"] == "contains"]
        assert len(contains_edges) >= len(expected)


def test_xios_manifest_view_and_component_extraction():
    """Verify that views and menu items in manifests are extracted."""
    manifest_content = """<?xml version="1.0" encoding="utf-8"?>
<application name="CloudBackend PaaS - Projects" xmlns:xlink="http://www.w3.org/1999/xlink">
  <view name="projectsView" title="Projects">
    <menu name="projMenu">
      <item name="caNewProject" text="New Project..."/>
      <item name="caRun" text="Run"/>
    </menu>
    <panel name="shellContent" xlink:href="host://service/views/projects.xml"/>
  </view>
  <process name="Boot - Projects">
    <trigger view="projectsView" event="Opened" step="init"/>
    <step id="init">
      <operation name="open" value="host://service/processes/projects.xml"/>
    </step>
  </process>
</application>"""
    with tempfile.TemporaryDirectory() as td:
        m_path = Path(td) / "manifests" / "projects.xml"
        m_path.parent.mkdir(parents=True)
        m_path.write_text(manifest_content, encoding="utf-8")

        res = extract_xios(m_path)
        nodes = res["nodes"]
        edges = res["edges"]

        # View node extracted
        v_nodes = [n for n in nodes if n["node_kind"] == "view" and n.get("_xios_view_name") == "projectsView"]
        assert len(v_nodes) == 1

        # Menu items extracted under the view
        comp_names = {n.get("_xios_comp_name") for n in nodes if "_xios_comp_name" in n}
        assert "caNewProject" in comp_names
        assert "caRun" in comp_names

        # Mounts view edge
        mount_edges = [e for e in edges if "mounts_view" in e.get("context", "")]
        assert len(mount_edges) >= 1

        # Inline boot process extracted
        proc_nodes = [n for n in nodes if n["node_kind"] == "process"]
        assert any(n["label"] == "Boot - Projects" for n in proc_nodes)


def test_xios_view_process_link_resolution():
    """Verify end-to-end resolution connecting processes, triggers, steps to views and components."""
    view_xml = """<?xml version="1.0" encoding="utf-8"?>
<view name="AppBrowser" width="500">
  <group name="radioGrp"/>
  <input name="search"/>
  <iconlist name="applications_list"/>
  <label name="appPath"/>
  <button name="select"/>
</view>"""

    proc_xml = """<?xml version="1.0" encoding="utf-8"?>
<process name="App Browser">
  <trigger view="AppBrowser" component="radioGrp" event="Select" step="filter"/>
  <trigger view="AppBrowser" component="applications_list" event="DoubleClick" step="openApp"/>
  <trigger view="AppBrowser" event="Opened" step="start"/>

  <step id="start">
    <operation name="open" value="AppBrowser"/>
    <operation name="bind" value="$apps">
      <component name="applications_list" view="AppBrowser"/>
    </operation>
  </step>

  <step id="filter">
    <alias name="filterVal" value="{#AppBrowser#radioGrp.getValue()}"/>
    <operation name="action" value="#AppBrowser">
      <component name="select" action="disable"/>
    </operation>
  </step>

  <step id="openApp">
    <operation name="callMethod" value="#AppBrowser#appPath">
      <method name="setValue"/>
    </operation>
    <operation name="close" value="AppBrowser"/>
  </step>
</process>"""

    with tempfile.TemporaryDirectory() as td:
        v_path = Path(td) / "views" / "appBrowser.xml"
        v_path.parent.mkdir(parents=True)
        v_path.write_text(view_xml, encoding="utf-8")

        p_path = Path(td) / "processes" / "appBrowser.xml"
        p_path.parent.mkdir(parents=True)
        p_path.write_text(proc_xml, encoding="utf-8")

        v_res = extract_xios(v_path)
        p_res = extract_xios(p_path)

        all_nodes = v_res["nodes"] + p_res["nodes"]
        all_edges = v_res["edges"] + p_res["edges"]

        pre_edges_count = len(all_edges)

        # Run resolution pass
        resolve_xios_view_process_links([], all_nodes, all_edges)

        assert len(all_edges) > pre_edges_count

        # Inspect generated edges
        edge_contexts = [e.get("context", "") for e in all_edges]
        assert any("fires_trigger" in c for c in edge_contexts)
        assert any("listens_to" in c for c in edge_contexts)
        assert any("triggers_on" in c for c in edge_contexts)
        assert any("operates_on" in c for c in edge_contexts)
        assert any("ui_action" in c for c in edge_contexts)
        assert any("callMethod" in c for c in edge_contexts)
        assert any("binds_data" in c for c in edge_contexts)
        assert any("opens_view" in c for c in edge_contexts)
        assert any("closes_view" in c for c in edge_contexts)
        assert any("alias_reads_component" in c for c in edge_contexts)


def test_xios_manifest_mounted_view_cross_resolution():
    """Verify that triggers referencing a manifest view connect to components in the mounted view file."""
    manifest_xml = """<?xml version="1.0" encoding="utf-8"?>
<application name="ProjectsApp" xmlns:xlink="http://www.w3.org/1999/xlink">
  <view name="projectsView">
    <menu name="projMenu">
      <item name="caNewProject"/>
    </menu>
    <panel xlink:href="host://service/views/projects.xml"/>
  </view>
</application>"""

    view_xml = """<?xml version="1.0" encoding="utf-8"?>
<view name="Projects">
  <filelist name="projects_list"/>
  <bush name="projectTree"/>
</view>"""

    proc_xml = """<?xml version="1.0" encoding="utf-8"?>
<process name="ProjectsProc">
  <trigger view="projectsView" component="projects_list" event="DoubleClick" step="open"/>
  <trigger view="projectsView" component="caNewProject" event="Select" step="create"/>

  <step id="open"/>
  <step id="create"/>
</process>"""

    with tempfile.TemporaryDirectory() as td:
        m_path = Path(td) / "manifests" / "projects.xml"
        m_path.parent.mkdir(parents=True)
        m_path.write_text(manifest_xml, encoding="utf-8")

        v_path = Path(td) / "views" / "projects.xml"
        v_path.parent.mkdir(parents=True)
        v_path.write_text(view_xml, encoding="utf-8")

        p_path = Path(td) / "processes" / "projects.xml"
        p_path.parent.mkdir(parents=True)
        p_path.write_text(proc_xml, encoding="utf-8")

        m_res = extract_xios(m_path)
        v_res = extract_xios(v_path)
        p_res = extract_xios(p_path)

        all_nodes = m_res["nodes"] + v_res["nodes"] + p_res["nodes"]
        all_edges = m_res["edges"] + v_res["edges"] + p_res["edges"]

        resolve_xios_view_process_links([], all_nodes, all_edges)

        # Check that trigger for projects_list (from view file) is connected
        plist_edges = [
            e for e in all_edges
            if "fires_trigger" in e.get("context", "")
            and "projects_list" in e["source"]
        ]
        assert len(plist_edges) >= 1

        # Check that trigger for caNewProject (from manifest) is connected
        newproj_edges = [
            e for e in all_edges
            if "fires_trigger" in e.get("context", "")
            and "canewproject" in e["source"].lower()
        ]
        assert len(newproj_edges) >= 1


def test_xios_system_xml_extraction():
    """Verify system XML extracts startup, params, themes, classes, and components."""
    system_xml = """<?xml version="1.0" encoding="utf-8"?>
<system startup="config/startup.xml" xmlns:xlink="http://www.w3.org/1999/xlink">
  <param name="database">host://service/database.xml</param>
  <param name="controller">host://service/controller.js</param>
  <themes default="aqua">
    <theme name="aqua" xlink:href="host://themes/aqua.xml"/>
  </themes>
  <class name="Utils" href="host://lib/utils.js"/>
  <component name="SysWidget" class="CA_SysWidget" href="host://ui/syswidget.js" tag="syswidget">
    <skin href="host://ui/syswidget.xsl"/>
    <style href="host://ui/syswidget.css"/>
    <dependency href="host://lib/dep.js" symbol="Dep"/>
  </component>
</system>"""
    with tempfile.TemporaryDirectory() as td:
        sys_path = Path(td) / "config" / "system.xml"
        sys_path.parent.mkdir(parents=True)
        sys_path.write_text(system_xml, encoding="utf-8")

        res = extract_xios(sys_path)
        nodes = res["nodes"]
        edges = res["edges"]

        # Check startup edge
        startup_edges = [e for e in edges if e.get("context") == "startup"]
        assert len(startup_edges) == 1
        assert "startup.xml" in startup_edges[0]["target_file"]

        # Check param edges
        db_edges = [e for e in edges if e.get("context") == "configures(database)"]
        assert len(db_edges) == 1
        assert "database.xml" in db_edges[0]["target_file"]

        ctrl_edges = [e for e in edges if e.get("context") == "configures(controller)"]
        assert len(ctrl_edges) == 1
        assert "controller.js" in ctrl_edges[0]["target_file"]

        # Check themes & class edges
        theme_edges = [e for e in edges if "registers_theme(aqua)" in e.get("context", "")]
        assert len(theme_edges) == 1

        class_edges = [e for e in edges if "registers_class(Utils)" in e.get("context", "")]
        assert len(class_edges) == 1

        # Check component definition and links
        comp_nodes = [n for n in nodes if n.get("node_kind") == "component_definition"]
        assert len(comp_nodes) == 1
        assert comp_nodes[0].get("tag") == "syswidget"

        impl_edges = [e for e in edges if e.get("context") == "implemented_by"]
        assert len(impl_edges) == 1
        skin_edges = [e for e in edges if e.get("context") == "skinned_by"]
        assert len(skin_edges) == 1
        style_edges = [e for e in edges if e.get("context") == "styled_by"]
        assert len(style_edges) == 1
        dep_edges = [e for e in edges if "depends_on(Dep)" in e.get("context", "")]
        assert len(dep_edges) == 1


def test_xios_component_xml_extraction():
    """Verify standalone component config.xml extraction."""
    comp_xml = """<?xml version="1.0" encoding="utf-8"?>
<component name="Diagram" class="CA_Diagram" href="host://ui/Diagram/CA_Diagram.js" tag="diagram">
  <dependency href="host://ui/Plotly/plotly-basic.min.js" symbol="Plotly"/>
  <skin href="host://ui/Diagram/diagram.xsl"/>
  <style href="host://ui/Diagram/diagram.css"/>
</component>"""
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "ui" / "Diagram" / "config.xml"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text(comp_xml, encoding="utf-8")

        res = extract_xios(cfg_path)
        nodes = res["nodes"]
        edges = res["edges"]

        comp_nodes = [n for n in nodes if n.get("node_kind") == "component_definition"]
        assert len(comp_nodes) == 1
        assert comp_nodes[0]["tag"] == "diagram"
        assert comp_nodes[0]["_xios_comp_def"] is True

        edge_contexts = {e.get("context") for e in edges}
        assert "implemented_by" in edge_contexts
        assert "depends_on(Plotly)" in edge_contexts
        assert "skinned_by" in edge_contexts
        assert "styled_by" in edge_contexts


def test_xios_catalog_xml_extraction():
    """Verify widgets / applications catalog extraction."""
    widgets_xml = """<?xml version="1.0" encoding="utf-8"?>
<widgets>
  <widget name="clock">
    <source>host://widgets/clock/clock.xml</source>
  </widget>
  <widget name="weather">
    <source>host://widgets/weather/weather.xml</source>
  </widget>
</widgets>"""
    with tempfile.TemporaryDirectory() as td:
        cat_path = Path(td) / "config" / "widgets.xml"
        cat_path.parent.mkdir(parents=True)
        cat_path.write_text(widgets_xml, encoding="utf-8")

        res = extract_xios(cat_path)
        nodes = res["nodes"]
        edges = res["edges"]

        assert any(n["node_kind"] == "file" for n in nodes)
        cat_contexts = [e.get("context") for e in edges]
        assert "catalogs(clock)" in cat_contexts
        assert "catalogs(weather)" in cat_contexts


def test_xios_view_scripts_and_custom_component_resolution():
    """Verify view script imports and resolution of custom components defined in index.html addConfig."""
    index_html = """<!DOCTYPE html>
<html>
<head>
  <script type="xios" name="addConfig">
    <component name="Plotly" class="CA_Plotly" href="host://ui/Plotly/CA_Plotly.js" tag="plotly">
      <dependency href="host://ui/Plotly/plotly-basic.min.js" symbol="Plotly"/>
      <skin href="host://ui/Plotly/plotly.xsl"/>
    </component>
    <component name="Diagram" class="CA_Diagram" href="host://ui/Diagram/CA_Diagram.js" tag="diagram">
      <skin href="host://ui/Diagram/diagram.xsl"/>
    </component>
  </script>
</head>
<body></body>
</html>"""

    view_xml = """<?xml version="1.0" encoding="utf-8"?>
<view name="Analytics">
  <script href="host://scripts/analytics_helper.js"/>
  <panel name="chartContainer">
    <diagram name="myDiagram"/>
    <plotly name="myPlot"/>
  </panel>
</view>"""

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "index.html").write_text(index_html, encoding="utf-8")

        v_path = root / "views" / "analytics.xml"
        v_path.parent.mkdir(parents=True)
        v_path.write_text(view_xml, encoding="utf-8")

        v_res = extract_xios(v_path, root=root)
        nodes = v_res["nodes"]
        edges = v_res["edges"]

        # View imports helper script
        script_edges = [e for e in edges if e.get("context") == "imports_script"]
        assert len(script_edges) == 1
        assert "analytics_helper.js" in script_edges[0]["target_file"]

        # Custom component nodes created
        c_nodes = [n for n in nodes if n.get("node_kind") == "custom_component"]
        c_tags = {n.get("_xios_tag") for n in c_nodes}
        assert "diagram" in c_tags
        assert "plotly" in c_tags

        # Run resolution pass
        resolve_xios_view_process_links([], nodes, edges)

        # Components connected to their definitions and implementations
        diag_node = next(n for n in nodes if n.get("_xios_comp_name") == "myDiagram")
        diag_impl_edges = [e for e in edges if e["source"] == diag_node["id"] and e.get("context") == "implemented_by"]
        assert len(diag_impl_edges) == 1

        plot_node = next(n for n in nodes if n.get("_xios_comp_name") == "myPlot")
        plot_impl_edges = [e for e in edges if e["source"] == plot_node["id"] and e.get("context") == "implemented_by"]
        assert len(plot_impl_edges) == 1


def test_xios_process_operations_and_open_bind_paths():
    """Verify process operation imports and open/bind/change document target_file resolution."""
    proc_xml = """<?xml version="1.0" encoding="utf-8"?>
<process name="DataSync">
  <script href="host://scripts/sync_util.js"/>
  <operations>
    <operation name="customSync" href="host://scripts/custom_sync.js"/>
  </operations>
  <step id="init">
    <operation name="open" value="host://views/sync_view.xml"/>
    <operation name="bind" value="host://data/schema.xml"/>
    <operation name="save" value="host://data/output.xml"/>
  </step>
</process>"""

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        p_path = root / "processes" / "sync.xml"
        p_path.parent.mkdir(parents=True)
        p_path.write_text(proc_xml, encoding="utf-8")

        p_res = extract_xios(p_path, root=root)
        nodes = p_res["nodes"]
        edges = p_res["edges"]

        # Script import
        script_edges = [e for e in edges if e.get("context") == "imports_script"]
        assert len(script_edges) == 1
        assert "sync_util.js" in script_edges[0]["target_file"]

        # Custom operation import
        op_edges = [e for e in edges if "declares_operation(customSync)" in e.get("context", "")]
        assert len(op_edges) == 1
        assert "custom_sync.js" in op_edges[0]["target_file"]

        # Open view reference with target_file
        open_edges = [e for e in edges if e.get("context") == "opens_view" and e.get("target_file")]
        assert len(open_edges) == 1
        assert "sync_view.xml" in open_edges[0]["target_file"]

        # Bind document reference with target_file
        bind_edges = [e for e in edges if e.get("context") == "binds_document"]
        assert len(bind_edges) == 1
        assert "schema.xml" in bind_edges[0]["target_file"]

        # Save document reference with target_file
        save_edges = [e for e in edges if e.get("context") == "save_document" and e.get("target_file")]
        assert len(save_edges) == 1
        assert "output.xml" in save_edges[0]["target_file"]

