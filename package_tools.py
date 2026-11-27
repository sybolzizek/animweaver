"""MCP entry points for portable AnimWeaver scene packages."""

import scene_package


def register_package_tools(mcp, work):
    @mcp.tool()
    def export_animweaver_scene(id: str, output: str) -> dict:
        """Export an editable scene and its complete material dependency graph.

        The new .animweaver ZIP contains the compiled scene, source snapshots,
        selections, supplements, motion references and provenance records required
        by that scene. Every entry is checksummed in the package manifest. Existing
        output files are never overwritten.
        """
        scene = work.load(id)
        if scene.get("kind") != "cutout_scene":
            raise ValueError("Choose an editable cutout scene")
        return scene_package.export(work, scene, output)

    @mcp.tool()
    def export_animweaver_project(id: str, output: str) -> dict:
        """Export a film and its complete scene, image, motion and audio closure.

        The new .animweaver package is deterministic and integrity checked. It
        retains the frame-exact film timeline plus every editable scene and source
        material needed to continue production in another workspace.
        """
        project = work.load(id)
        if project.get("kind") != "film_project":
            raise ValueError("Choose an editable film project")
        return scene_package.export(work, project, output)

    @mcp.tool()
    def import_animweaver_scene(path: str) -> dict:
        """Verify and import a portable .animweaver scene package.

        The importer rejects unsafe paths, undeclared files, checksum failures,
        missing dependencies and material-ID conflicts before installing new records.
        It returns the editable root scene or film project and a separate import
        receipt. Existing byte-identical materials are reused without being rewritten.
        """
        return scene_package.import_package(work, path, expected_kind="cutout_scene")

    @mcp.tool()
    def import_animweaver_project(path: str) -> dict:
        """Verify and import a portable film project package.

        This is the film-oriented alias of import_animweaver_scene. The package
        root must be a film project; all scene, image, motion and audio dependencies
        are verified before installation.
        """
        return scene_package.import_package(work, path, expected_kind="film_project")
