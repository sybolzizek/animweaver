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
        return scene_package.export(work, work.load(id), output)

    @mcp.tool()
    def import_animweaver_scene(path: str) -> dict:
        """Verify and import a portable .animweaver scene package.

        The importer rejects unsafe paths, undeclared files, checksum failures,
        missing dependencies and material-ID conflicts before installing new records.
        It returns the editable root scene and a separate import receipt. Existing
        byte-identical materials are reused without being rewritten.
        """
        return scene_package.import_package(work, path)
