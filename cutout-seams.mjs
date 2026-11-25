import {isActive} from './cutout-timeline.mjs';

// Fit the strip to the actual projected pixels; do not change the cutouts themselves.
export function linkSeam(spec, image, makeMesh) {
  const mesh = makeMesh({
    box: [0, 0, image.width, image.height],
    mesh: {grid: [1, spec.rows], weights: Array.from({length:(spec.rows + 1) * 2}, () => [1])},
  }, image);
  function area(p, q, r) {
    return (q.x-p.x)*(r.y-p.y) - (q.y-p.y)*(r.x-p.x);
  }
  function intersection(p, q, r, s) {
    const ax=q.x-p.x, ay=q.y-p.y, bx=s.x-r.x, by=s.y-r.y;
    const den=ax*by-ay*bx;
    if (Math.abs(den) < 1e-8) return null;
    const x=r.x-p.x, y=r.y-p.y;
    const u=(x*by-y*bx)/den, v=(x*ay-y*ax)/den;
    if (u <= 0 || u >= 1 || v <= 0 || v >= 1) return null;
    const axis = key => (p[key]+u*(q[key]-p[key])+r[key]+v*(s[key]-r[key]))/2;
    return {x:p.x+u*ax, y:p.y+u*ay, u:axis("u"), v:axis("v")};
  }
  function evaluate(frame) {
    if (!isActive(spec.windows, frame.time))
      return {status:"inactive"};
    const first = spec.rails[0].map(xy => frame.point(spec.a.layer, xy));
    const second = spec.rails[1].map(xy => frame.point(spec.b.layer, xy));
    const span = Math.max(...first.map((p,i) => Math.hypot(p.x-second[i].x,p.y-second[i].y)));
    if (span > spec.max_span) return {status:"too_wide", span};
    const vertices = [];
    let crossings = 0;
    const tri = (p, q, r) => {
      if (Math.abs(area(p,q,r)) < 0.001) return;
      for (const a of [p,q,r]) vertices.push(a.x,a.y,a.u,a.v);
    };
    for (let i=0; i<first.length-1; i++) {
      const v=i/spec.rows, n=(i+1)/spec.rows;
      const vertex = ({x,y},u,v) => ({x,y,u,v});
      const p=vertex(first[i],0,v), q=vertex(second[i],1,v);
      const r=vertex(first[i+1],0,n), s=vertex(second[i+1],1,n);
      const edgeCross = intersection(p,r,q,s);
      if (edgeCross) {
        tri(p,q,edgeCross);
        tri(edgeCross,s,r);
        crossings++;
      } else {
        const connectorCross = intersection(p,q,r,s);
        if (connectorCross) {
          tri(p,connectorCross,r);
          tri(connectorCross,q,s);
          crossings++;
        } else if (area(p,q,r)*area(q,s,r) >= 0) {
          tri(p,q,r);
          tri(q,s,r);
        } else {
          tri(p,q,s);
          tri(p,s,r);
        }
      }
    }
    if (!vertices.length) return {status:"coincident", span};
    return {status:"drawn", span, crossings, vertices:Float32Array.from(vertices)};
  }
  return {spec, evaluate, draw(context, vertices) {
    context.resetTransform();
    context.drawImage(mesh.ribbon(vertices), 0, 0);
  }};
}
