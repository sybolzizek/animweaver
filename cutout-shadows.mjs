// Project animated cutout alpha onto a specific receiver without changing its alpha.
export function createShadowPainter(width, height) {
  function plane() {
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    return canvas.getContext("2d");
  }
  const receiver = plane(), silhouette = plane();
  const reset = ctx => {
    ctx.resetTransform();
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    ctx.filter = "none";
    ctx.clearRect(0, 0, width, height);
  };
  return (target, drawReceiver, projections) => {
    reset(receiver);
    drawReceiver(receiver);
    receiver.resetTransform();
    for (const {shadow, anchor, caster} of projections) {
      if (!shadow.opacity) continue;
      reset(silhouette);
      const project = new DOMMatrix().translate(anchor.x + shadow.offset[0], anchor.y + shadow.offset[1])
        .scale(...shadow.scale).translate(-anchor.x, -anchor.y);
      caster(silhouette, project);
      silhouette.resetTransform();
      silhouette.globalCompositeOperation = "source-in";
      silhouette.fillStyle = `rgb(${shadow.color.join(" ")})`;
      silhouette.fillRect(0, 0, width, height);

      receiver.globalCompositeOperation = "source-atop";
      receiver.globalAlpha = shadow.opacity;
      receiver.filter = shadow.blur ? `blur(${shadow.blur}px)` : "none";
      receiver.drawImage(silhouette.canvas, 0, 0);
    }
    target.resetTransform();
    target.drawImage(receiver.canvas, 0, 0);
  };
}
