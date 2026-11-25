// Skin a textured, indexed grid in source-pixel space. A single draw keeps adjacent
// triangles watertight; feathering individual Canvas 2D triangles would leave seams.
export function createMeshRenderer(width, height) {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const gl = canvas.getContext("webgl2", {alpha:true, antialias:false, premultipliedAlpha:true});
  if (!gl) throw new Error("WebGL 2 is required for deformable cutouts");
  const program = gl.createProgram();
  for (const [type, source] of [[gl.VERTEX_SHADER, `#version 300 es
    in vec2 position;
    in vec2 uv;
    out vec2 texCoord;
    uniform vec2 size;
    void main() {
      gl_Position = vec4(position / size * vec2(2., -2.) + vec2(-1., 1.), 0., 1.);
      texCoord = uv;
    }`], [gl.FRAGMENT_SHADER, `#version 300 es
    precision highp float;
    in vec2 texCoord;
    uniform sampler2D source;
    uniform vec4 light;
    out vec4 color;
    void main() {
      color = texture(source, texCoord);
      if (color.a == 0.) discard;
      if (light.z != 0. || light.w != 0.) {
        vec3 rgb = clamp(color.rgb / color.a, 0., 1.);
        vec3 linear = mix(rgb / 12.92, pow((rgb + .055) / 1.055, vec3(2.4)),
                          step(vec3(.04045), rgb));
        linear = clamp(linear * exp2(light.z + light.w * dot(texCoord - .5, light.xy)), 0., 1.);
        rgb = mix(linear * 12.92, 1.055 * pow(linear, vec3(1. / 2.4)) - .055,
                  step(vec3(.0031308), linear));
        color.rgb = rgb * color.a;
      }
    }`]]) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader));
    gl.attachShader(program, shader);
    gl.deleteShader(shader);
  }
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
  gl.useProgram(program);
  gl.uniform2f(gl.getUniformLocation(program, "size"), width, height);
  const lightUniform = gl.getUniformLocation(program, "light");
  const position = gl.getAttribLocation(program, "position"), uv = gl.getAttribLocation(program, "uv");
  gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
  gl.viewport(0, 0, width, height);

  return (node, image) => {
    const [left, top, right, bottom] = node.box;
    const [cols, rows] = node.mesh.grid, weights = node.mesh.weights;
    const vertices = new Float32Array(weights.length * 4), triangles = new Uint32Array(cols * rows * 6);
    for (let y = 0; y <= rows; y++) for (let x = 0; x <= cols; x++) {
      const i = y * (cols + 1) + x;
      vertices[i * 4 + 2] = x / cols;
      vertices[i * 4 + 3] = y / rows;
      if (x < cols && y < rows) triangles.set(
        [i, i + 1, i + cols + 1, i + 1, i + cols + 2, i + cols + 1], (y * cols + x) * 6
      );
    }
    const texture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    const vao = gl.createVertexArray();
    gl.bindVertexArray(vao);
    const vertexBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, vertexBuffer);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 16, 0);
    gl.vertexAttribPointer(uv, 2, gl.FLOAT, false, 16, 8);
    gl.enableVertexAttribArray(position);
    gl.enableVertexAttribArray(uv);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, triangles, gl.STATIC_DRAW);

    function vertex(i, matrices) {
      const j = i * 4;
      const x = left + vertices[j + 2] * (right - left), y = top + vertices[j + 3] * (bottom - top);
      let px = 0, py = 0;
      for (let b = 0; b < matrices.length; b++) {
        const w = weights[i][b], m = matrices[b];
        px += w * (m.a * x + m.c * y + m.e);
        py += w * (m.b * x + m.d * y + m.f);
      }
      return [px, py];
    }
    function paint(light = [0, 0, 0, 0]) {
      gl.uniform4fv(lightUniform, light);
      gl.bindVertexArray(vao);
      gl.bindBuffer(gl.ARRAY_BUFFER, vertexBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.DYNAMIC_DRAW);
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.drawElements(gl.TRIANGLES, triangles.length, gl.UNSIGNED_INT, 0);
      return canvas;
    }
    const draw = (matrices, light) => {
      for (let i = 0; i < weights.length; i++) {
        const [x, y] = vertex(i, matrices);
        vertices[i * 4] = x;
        vertices[i * 4 + 1] = y;
      }
      return paint(light);
    };
    draw.ribbon = data => {
      gl.uniform4f(lightUniform, 0, 0, 0, 0);
      gl.bindVertexArray(vao);
      gl.bindBuffer(gl.ARRAY_BUFFER, vertexBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, data, gl.DYNAMIC_DRAW);
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.drawArrays(gl.TRIANGLES, 0, data.length / 4);
      return canvas;
    };
    draw.point = ([x, y], matrices) => {
      const u = Math.max(0, Math.min(cols, (x - left) * cols / (right - left)));
      const v = Math.max(0, Math.min(rows, (y - top) * rows / (bottom - top)));
      const col = Math.min(cols - 1, Math.floor(u)), row = Math.min(rows - 1, Math.floor(v));
      const a = u - col, b = v - row, i = row * (cols + 1) + col;
      const triangle = a + b <= 1
        ? [[i, 1-a-b], [i+1, a], [i+cols+1, b]]
        : [[i+1, 1-b], [i+cols+2, a+b-1], [i+cols+1, 1-a]];
      let px = 0, py = 0;
      for (const [id, w] of triangle) {
        const [vx, vy] = vertex(id, matrices);
        px += w * vx; py += w * vy;
      }
      return [px, py];
    };
    return draw;
  };
}
