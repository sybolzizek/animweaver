"""MCP entry points for whole-frame image decomposition."""

import asyncio
from typing import Annotated

import image_split
import cutout_scene
import cutout_tree
import scene_export
import scene_appearance
from scene_seams import Seam
from pydantic import Field, FiniteFloat


def register_image_tools(mcp, work):
    @mcp.tool()
    async def split_image(id: str) -> dict:
        """Partition an image by pixel boundaries, not object semantics. Inspect or preview the result.

        Use explicit polygons or import external masks when the image needs semantic parts.
        """
        source = work.load(id)
        record = await asyncio.to_thread(image_split.build, work, source)
        return {
            key: record[key] for key in ("id", "kind", "width", "height", "inference")
        } | {
            "part_count": len(record["parts"]),
            "overview": record["files"]["preview.png"],
        }

    @mcp.tool()
    async def select_image_split(
        id: str,
        regions: list[str] | None = None,
        include: list[tuple[int, int]] | None = None,
        exclude: list[tuple[int, int]] | None = None,
    ) -> dict:
        """Extract a hard RGBA crop from stored regions, optionally using include/exclude points."""
        return await asyncio.to_thread(
            image_split.group,
            work,
            work.load(id),
            regions or [],
            include or [],
            exclude or [],
        )

    @mcp.tool()
    async def register_image_split_mask(
        id: str,
        mask_path: str,
        box: tuple[int, int, int, int] | None = None,
        threshold: int = 128,
    ) -> dict:
        """Turn a corrected PNG mask into an ordinary hard selection using only the split's original image pixels.

        Use a workspace-relative path, 8-bit grayscale (white is foreground) or an alpha channel.
        Without a box the mask must match the source size. For a crop give [left, top, right, bottom]
        in source pixels, with an exclusive right and bottom. ID can be a split or its previous selection.
        This creates a new selection; pass its ID into split_image_region or prepare_cutout_scene.
        """
        if not 1 <= threshold <= 255:
            raise ValueError("Mask threshold must be 1–255")
        return await asyncio.to_thread(
            image_split.import_mask, work, work.load(id), mask_path, box, threshold
        )

    @mcp.tool()
    async def split_image_region(
        id: str,
        cuts: list[cutout_tree.ChildCut],
        parent: str = "root",
        replace: bool = False,
    ) -> dict:
        """Create or subdivide a cutout tree from an image split, hard selection, or previous tree.

        Supply deliberate child cuts as scene-coordinate polygons or hard selection IDs from the
        original split. All child pixels are clipped to the parent. Later cuts claim overlaps;
        remaining visible pixels form a selectable @rest child. A child can be partitioned by path.
        Use inspect_material to read the whole tree. The tree describes cuts, not animation.
        """
        if len(cuts) > 65534:
            raise ValueError("Too many sibling cuts for the preview")
        return await asyncio.to_thread(
            cutout_tree.split_branch, work, work.load(id), parent, cuts, replace
        )

    @mcp.tool()
    async def export_cutout_region(
        id: str,
        branch: str,
        padding: int = 16,
        box: tuple[int, int, int, int] | None = None,
    ) -> dict:
        """Export unmodified source pixels and current branch mask at the same original scale.

        Both PNGs share source_box. Use padding for surrounding context or give an exact source box.
        The result's split ID and source_box can register an externally edited mask at that scale.
        """
        if padding < 0:
            raise ValueError("Padding must be nonnegative")
        return await asyncio.to_thread(
            cutout_tree.export_region, work, work.load(id), branch, padding, box
        )

    @mcp.tool()
    async def replace_cutout_region(
        id: str,
        branch: str,
        selection: str | None = None,
        box: tuple[int, int, int, int] | None = None,
        preview_only: bool = True,
    ) -> dict:
        """Preview or apply a hard selection patch to an explicit cutout branch of the same split.

        Edits are limited to box (the imported mask's input box by default, otherwise the full source).
        Omit selection with an explicit box to clear just that region.
        Other explicit siblings keep their pixels. Added/removed pixels move to/from the parent's @rest.
        The preview shows additions in green and removals in red; applying returns a new tree ID.
        Descendants retain pixels still inside the branch, and any emptied descendants are reported.
        """
        return await asyncio.to_thread(
            cutout_tree.replace_region,
            work,
            work.load(id),
            branch,
            selection,
            box,
            preview_only,
        )

    @mcp.tool()
    async def prepare_cutout_scene(
        id: str,
        layers: list[cutout_scene.Cutout],
        groups: list[cutout_scene.Node] | None = None,
        duration: cutout_scene.Positive = 4,
        backing: str | None = None,
        shadows: list[cutout_scene.SoftShadow] | None = None,
        seams: list[Seam] | None = None,
        mattes: list[cutout_scene.LocalMatte] | None = None,
    ) -> dict:
        """Build a browser scene. ID supplies the canvas image (or its split). List layers back to front.

        A layer uses a hard selection or an image source, from the base or another image. Offset places
        source pixel (0,0) on the canvas. Pivots, bone tips and translations use canvas pixels.
        A selection layer can use edge_matte from refine_image_split_edges on that same selection.
        Use one continuous mesh for soft joints; weight selections guide bones without creating visible seams.
        A rigid attachment may instead follow an unparented group with mount={layer, point, towards}.
        Mount points use uncropped layer source pixels; the group's pivot lands on point and follows
        the deformed direction from point to towards. Bone tips use canvas pixels.
        blend sets the transition in source pixels; cell sets the size of mesh triangles.
        Use the same explicit mesh.domain, cell, offset and bone-tip bindings for separate
        textures on one continuous surface. This aligns the deformation grids independently of
        alpha bounds; different weight selections are not automatically welded.
        For a grip, keep the object between rear and front cutouts in paint order (layer.depth
        supports step changes). For a newly exposed joint, underlap adds aligned RGBA patches behind
        that layer without claiming source pixels. Its 'at' is the patch's top-left in host source pixels.
        Use layer.paintover to replace an incorrect edge or texture on the face of that cutout.
        A matte keeps or erases one target's alpha using a moving mask layer, optionally only in
        designated time windows. For a mask that must not itself be visible, mark it matte_only.
        Plain foreground overlap usually does not need a second matte.
        When two separately animated cutouts must share a seam, supply two matching rails inside
        their opaque material. A small strip follows both edges, underneath both layers.
        max_span limits the strip at stretched poses in canvas pixels; it must not bridge an intentional opening.
        An optional aligned backing image supplies what is revealed; without it gaps stay transparent.
        Optional appearance and receiver-clipped shadows change compositing, not geometry or ownership.
        Render the furthest pose or a contact frame before exporting the full sequence.
        """
        return await asyncio.to_thread(
            cutout_scene.build,
            work,
            id,
            layers,
            groups or [],
            duration,
            backing,
            shadows=shadows,
            seams=seams,
            mattes=mattes,
        )

    @mcp.tool()
    async def revise_cutout_scene(
        id: str,
        replacements: dict[str, str] | None = None,
        backing: str | None = None,
        depth: dict[str, list[cutout_scene.DepthKey]] | None = None,
        meshes: dict[str, cutout_scene.Skin | None] | None = None,
        mounts: dict[str, cutout_scene.SurfaceMount | None] | None = None,
        underlaps: dict[str, list[cutout_scene.Underlap]] | None = None,
        appearances: dict[str, scene_appearance.Appearance | None] | None = None,
        shadows: list[cutout_scene.SoftShadow] | None = None,
        seams: list[Seam] | None = None,
        paintovers: dict[str, list[cutout_scene.Underlap]] | None = None,
        mattes: list[cutout_scene.LocalMatte] | None = None,
        edge_mattes: dict[cutout_scene.Key, str | None] | None = None,
        motions: dict[cutout_scene.Key, list[cutout_scene.Pose]] | None = None,
        duration: cutout_scene.Positive | None = None,
    ) -> dict:
        """Create a new scene; revise only the layer or group implicated by the rendered frame.

        Mounts use group keys; selections, meshes, depth, paint patches and appearance use layer keys.
        Omitted keys are kept. Null removes a mount, mesh, appearance or refined edge matte.
        Edge mattes affect rendering only and must refine that layer's exact selection.
        motions replaces the supplied groups' or layers' complete tracks, [] clears one track.
        duration optionally changes the shot length; keyframes and windows are validated, not retimed.
        Shadows, seams and mattes replace their respective lists. Pivots stay unchanged.
        """
        return await asyncio.to_thread(
            cutout_scene.revise,
            work,
            work.load(id),
            replacements=replacements,
            backing=backing,
            depth=depth,
            meshes=meshes,
            mounts=mounts,
            underlaps=underlaps,
            appearances=appearances,
            shadows=shadows,
            seams=seams,
            paintovers=paintovers,
            mattes=mattes,
            edge_mattes=edge_mattes,
            motions=motions,
            duration=duration,
        )

    @mcp.tool()
    async def match_cutout_appearance(
        source: str,
        source_box: tuple[int, int, int, int],
        reference: str,
        reference_box: tuple[int, int, int, int],
        strength: Annotated[FiniteFloat, Field(ge=0, le=1)] = 0.75,
    ) -> dict:
        """Suggest an RGB/exposure/contrast grade from two comparable interior surface samples.

        Sample the same material under comparable lighting, avoiding contours and strong highlights.
        Boxes use each registered image's coordinates. Apply the returned grade to a layer's appearance or an underlap;
        this tool does not alter a file and cannot fix silhouettes, misplaced cutouts or missing shadows.
        """
        return await asyncio.to_thread(
            scene_appearance.match,
            work,
            source,
            source_box,
            reference,
            reference_box,
            strength,
        )

    @mcp.tool()
    async def render_cutout_scene(
        id: str, output: str, time: FiniteFloat | None = None, fps: int = 24
    ) -> dict:
        """Render a PNG at time (seconds), or submit an MP4 export at fps.

        PNG returns the finished file and retains transparency. MP4 returns a job ID:
        use inspect_cutout_video to read status and the finished file. Repeating the
        same MP4 request returns the same job. MP4 rejects unfilled frames.
        """
        if not 1 <= fps <= 120:
            raise ValueError("FPS must be 1–120")
        scene = work.load(id)
        if work.path(output).suffix.lower() == ".mp4":
            if time is not None:
                raise ValueError("time applies only to PNG")
            return scene_export.start(work, scene, output, fps)
        return await asyncio.to_thread(
            scene_export.render, work, scene, output, time, fps
        )

    @mcp.tool()
    def inspect_cutout_video(id: str) -> dict:
        """Read a submitted MP4 job without restarting it. Completed jobs include the render record."""
        return scene_export.status(work, id)

    @mcp.tool()
    async def refine_image_split_edges(id: str, band_px: int = 4) -> dict:
        """Refine an image split selection for compositing. Does not change the source or partition.

        Returns an RGBA crop and its alpha matte with a source-coordinate box. The band is measured
        in original pixels. Pass the result ID as edge_matte on a layer or via revise_cutout_scene.edge_mattes;
        the original selection still owns its pixels. An aligned backing is needed to move its exterior halo
        off the base image; a mesh can only feather inside its pre-existing lattice or an explicit mesh.domain.
        """
        if not 1 <= band_px <= 24:
            raise ValueError("band_px must be 1–24 source pixels")
        return await asyncio.to_thread(
            image_split.refine_edges, work, work.load(id), band_px
        )

    @mcp.tool()
    async def recompose_image_split(id: str) -> dict:
        """Reassemble unmodified split parts and report the RGBA mismatch relative to the original."""
        return await asyncio.to_thread(image_split.recompose, work, work.load(id))
