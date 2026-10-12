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
