const $ = (id) => document.getElementById(id);
const svgNS = 'http://www.w3.org/2000/svg';
const map = $('map');
const dialog = $('import-dialog');
const layerList = $('layers');
const assetSelect = $('asset-select');
let project = null;
let sheet = null;
let busy = false;
let zoom = 1;

async function request(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || `Request failed (${response.status}).`);
  }
  return response;
}

function post(path, payload) {
  return request(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
}

function status(message, error = false) {
  $('assistant-message').textContent = message;
  $('assistant-message').classList.toggle('error', error);
}

function updateControls() {
  for (const id of ['run', 'download-pdf', 'download-dxf', 'asset-select', 'zoom-in', 'zoom-out', 'zoom-reset']) {
    $(id).disabled = busy || !sheet;
  }
  for (const id of ['show-import', 'load-demo', 'import-submit', 'close-import']) $(id).disabled = busy;
  for (const control of document.querySelectorAll('[data-command]')) {
    const required = (control.dataset.requires || '').split(',').filter(Boolean);
    control.disabled = busy || !sheet || required.some((key) => !project.layers[key]);
  }
  for (const checkbox of layerList.querySelectorAll('input')) checkbox.disabled = busy;
}

async function withBusy(action, onError = (error) => status(error.message, true)) {
  if (busy) return;
  busy = true;
  updateControls();
  try {
    await action();
  } catch (error) {
    onError(error);
  } finally {
    busy = false;
    updateControls();
  }
}

function createSVG(type, attributes) {
  const node = document.createElementNS(svgNS, type);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  return node;
}

function paint(geometry, styling, group) {
  if (geometry.type === 'GeometryCollection') {
    for (const child of geometry.geometries) paint(child, styling, group);
    return;
  }
  const local = ([easting, northing]) => [easting - sheet.center[0], sheet.center[1] - northing];
  const pathFor = (coordinates, close) => coordinates.map((coordinate, index) => {
    const [x, y] = local(coordinate);
    return `${index ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)}`;
  }).join('') + (close ? 'Z' : '');
  const coordinates = geometry.coordinates;
  if (!coordinates?.length) return;
  if (geometry.type === 'Point' || geometry.type === 'MultiPoint') {
    const points = geometry.type === 'Point' ? [coordinates] : coordinates;
    for (const point of points) {
      const [cx, cy] = local(point);
      group.append(createSVG('circle', { cx, cy, r: 0.7, fill: styling.stroke }));
    }
    return;
  }
  const polygons = geometry.type === 'Polygon' ? [coordinates] : geometry.type === 'MultiPolygon' ? coordinates : null;
  const lines = geometry.type === 'LineString' ? [coordinates] : coordinates;
  const path = polygons
    ? polygons.flatMap((rings) => rings.map((ring) => pathFor(ring, true))).join('')
    : lines.map((line) => pathFor(line, false)).join('');
  group.append(createSVG('path', {
    d: path, fill: polygons ? styling.fill : 'none', stroke: styling.stroke,
    'fill-rule': 'evenodd', 'stroke-width': styling.width, 'stroke-dasharray': styling.dash || 'none',
    'vector-effect': 'non-scaling-stroke', 'stroke-linejoin': 'round', 'stroke-linecap': 'round',
  }));
}

function fitMap() {
  if (!sheet || !map.clientHeight) return;
  const aspect = map.clientWidth / map.clientHeight;
  const main = 1.12 * sheet.state.radius / zoom;
  const halfWidth = main * Math.max(1, aspect);
  const halfHeight = main * Math.max(1, 1 / aspect);
  map.setAttribute('viewBox', `${-halfWidth} ${-halfHeight} ${halfWidth * 2} ${halfHeight * 2}`);
  const ratio = map.clientHeight / (2 * halfHeight);
  const bar = [500, 200, 100, 50, 20, 10, 5, 2, 1].find((value) => value * ratio < 96) || 1;
  $('scale-text').textContent = `${bar} m`;
  $('scale-bar').style.width = `${bar * ratio}px`;
}

function renderMap() {
  map.replaceChildren();
  const extent = createSVG('circle', {
    cx: 0, cy: 0, r: sheet.state.radius, fill: '#14271e29', stroke: '#98ae8675',
    'stroke-dasharray': '3 5', 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke',
  });
  map.append(extent);
  for (const key of sheet.state.layers) {
    const info = project.layers[key];
    const group = createSVG('g', { 'data-layer': key });
    const styling = {
      stroke: info.color, fill: key === 'boundary' ? '#33433866' : info.fill || 'none',
      width: { terrain: 0.75, boundary: 1.3, water: 2, drainage: 1.6 }[key] || 1.1,
      dash: { boundary: '5 4', drainage: '4 4' }[key],
    };
    for (const geometry of sheet.features[key]) paint(geometry, styling, group);
    map.append(group);
  }
  paint(sheet.subject, { stroke: '#f7c18b', fill: '#8a623e', width: 2 }, map);
  const tag = sheet.asset.tag || sheet.asset.name;
  const labelSize = sheet.state.radius / 42;
  const text = createSVG('text', {
    x: 0, y: 0, fill: '#fff2de', stroke: '#293227', 'stroke-width': 2.8,
    'paint-order': 'stroke', 'text-anchor': 'middle', 'dominant-baseline': 'central',
    'font-size': labelSize, 'font-family': 'Consolas, monospace', 'font-weight': '700',
  });
  text.textContent = tag;
  map.append(text);
  fitMap();
}

function renderSheet() {
  const { asset, state, features } = sheet;
  const name = asset.tag && asset.tag !== asset.name ? `${asset.name} · ${asset.tag}` : asset.name;
  $('sheet-title').textContent = name;
  $('sheet-radius').textContent = `${state.radius} m radius`;
  for (const id of ['sheet-radius', 'scale', 'map-label']) $(id).hidden = false;
  $('map-empty').hidden = true;
  $('project-label').textContent = project.project === 'demo' ? 'DEMOWORKS / SITE PLAN' : 'SITE PLAN';
  $('model-file').textContent = project.ifc;
  $('gis-file').textContent = project.gis;
  $('project-crs').textContent = project.crs;
  assetSelect.value = state.guid;
  for (const label of layerList.querySelectorAll('label')) {
    const checkbox = label.querySelector('input');
    checkbox.checked = state.layers.includes(checkbox.value);
    label.querySelector('.layer-count').textContent = checkbox.checked ? features[checkbox.value].length : '—';
  }
  renderMap();
}

function showProject(data) {
  project = data;
  assetSelect.replaceChildren(...data.assets.map(({ guid, tag, name }) => new Option(tag && tag !== name ? `${tag} · ${name}` : name, guid)));
  layerList.replaceChildren();
  for (const key of ['boundary', 'buildings', 'roads', 'water', 'drainage', 'terrain', 'other']) {
    const info = project.layers[key];
    if (!info) continue;
    const item = document.createElement('label');
    item.className = 'layer';
    const swatch = document.createElement('span');
    swatch.className = 'layer-swatch';
    swatch.style.setProperty('--swatch', info.color);
    const text = document.createElement('span');
    text.textContent = info.label;
    const count = document.createElement('span');
    count.className = 'layer-count';
    count.setAttribute('aria-hidden', 'true');
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.value = key;
    item.append(swatch, text, count, checkbox);
    layerList.append(item);
  }
}

function drawingStatus() {
  const { state, asset, features } = sheet;
  const count = Object.values(features).filter((layer) => layer.length).length;
  const siteLayers = state.layers.filter((key) => key !== 'buildings');
  if (siteLayers.length && siteLayers.every((key) => !features[key].length)) {
    status('No selected layer features intersect this area. Check the site extent or model coordinates.', true);
    return;
  }
  status(`${asset.tag || asset.name} · ${state.radius} m · ${count} visible ${count === 1 ? 'layer' : 'layers'}. Drawing ready to export.`);
}

async function load(data) {
  const first = data.assets.find((asset) => asset.tag === 'P-101') || data.assets[0];
  const result = await post('/api/sheet', { project: data.project, state: { guid: first.guid } });
  const nextSheet = await result.json();
  zoom = 1;
  showProject(data);
  sheet = nextSheet;
  renderSheet();
  drawingStatus();
}

function loadDemo() {
  return withBusy(async () => {
    status('Preparing the example drawing…');
    await load(await (await request('/api/demo')).json());
  }, (error) => {
    if (!sheet) $('map-empty').textContent = 'The example could not be loaded.';
    status(error.message, true);
  });
}

function run(prompt, changes = {}) {
  return withBusy(async () => {
    status('Clipping selected layers and updating the drawing…');
    const state = { ...sheet.state, ...changes.state };
    const response = await post('/api/sheet', { project: project.project, state, prompt, layers: changes.layers });
    const result = await response.json();
    if (sheet.state.radius !== result.state.radius || sheet.state.guid !== result.state.guid) zoom = 1;
    sheet = result;
    renderSheet();
    drawingStatus();
  }, (error) => {
    renderSheet();
    status(error.message, true);
  });
}

async function download(format) {
  await withBusy(async () => {
    const response = await post(`/api/export/${format}`, { project: project.project, state: sheet.state });
    const file = await response.blob();
    const url = URL.createObjectURL(file);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = response.headers.get('content-disposition')?.match(/filename="?([^";]+)"?/)?.[1] || `site-sheet.${format}`;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    status(`${format.toUpperCase()} exported for ${sheet.asset.tag || sheet.asset.name}.`);
  });
}

$('assistant-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const prompt = $('command').value.trim();
  if (prompt) run(prompt);
});
for (const control of document.querySelectorAll('[data-command]')) {
  control.addEventListener('click', () => {
    $('command').value = control.dataset.command;
    run(control.dataset.command);
  });
}
assetSelect.addEventListener('change', () => run('', { state: { guid: assetSelect.value } }));
layerList.addEventListener('change', () => run('', { layers: [...layerList.querySelectorAll('input:checked')].map((input) => input.value) }));
$('load-demo').addEventListener('click', loadDemo);
$('download-pdf').addEventListener('click', () => download('pdf'));
$('download-dxf').addEventListener('click', () => download('dxf'));
$('zoom-in').addEventListener('click', () => { zoom = Math.min(3, zoom * 1.25); fitMap(); });
$('zoom-out').addEventListener('click', () => { zoom = Math.max(0.65, zoom / 1.25); fitMap(); });
$('zoom-reset').addEventListener('click', () => { zoom = 1; fitMap(); });
new ResizeObserver(fitMap).observe(map);
$('show-import').addEventListener('click', () => { $('import-error').hidden = true; dialog.showModal(); });
$('close-import').addEventListener('click', () => dialog.close());
dialog.addEventListener('cancel', (event) => { if (busy) event.preventDefault(); });
$('import-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  withBusy(async () => {
    $('import-error').hidden = true;
    const response = await request('/api/projects', { method: 'POST', body: new FormData(form) });
    await load(await response.json());
    form.reset();
    dialog.close();
  }, (error) => {
    $('import-error').textContent = error.message;
    $('import-error').hidden = false;
  });
});
loadDemo();
