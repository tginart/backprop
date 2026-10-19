/** Preserve browser-owned exploration state across a render of the same view.
 * Logical keys distinguish panels, runs and values; local DOM paths cover new
 * scroll containers without another selector allowlist. Nothing is persisted.
 */
const identityAttributes = ['id', 'data-view-key', 'data-metric', 'data-section-id',
  'data-media-key', 'data-plotly-key', 'data-vega-key', 'data-state', 'data-rich-step', 'data-table-search'];

function identity(node) {
  const attribute = identityAttributes.find(name => node.hasAttribute(name));
  return attribute ? [attribute, node.getAttribute(attribute)] : [node.tagName, node.classList[0] || ''];
}
const sameIdentity = (node, key) => JSON.stringify(identity(node)) === JSON.stringify(key);
function pathTo(root, node) {
  const path = [];
  for (; node && node !== root; node = node.parentElement) {
    const key = identity(node);
    const index = [...node.parentElement.children].filter(child => sameIdentity(child, key)).indexOf(node);
    path.unshift({key, index});
  }
  return path;
}
function find(root, path) {
  let node = root;
  for (const {key, index} of path) {
    node = [...node.children].filter(child => sameIdentity(child, key))[index];
    if (!node) return null;
  }
  return node;
}

/**
 * Purpose: Capture browser state without a traversal of chart SVG marks.
 * Input: The HTML view root.
 * Output: The scroll positions, disclosures, and active control state.
 * Note: All HTML containers remain eligible, including nested chart controls.
 */
export function captureViewState(root) {
    const scroll = [], details = [], active = document.activeElement;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT, {
        // Chart SVG mark trees contain no scroll or disclosure state.
        acceptNode: node => node.namespaceURI === 'http://www.w3.org/2000/svg'
            ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
    });
    for (let node = root; node; node = walker.nextNode()) {
        const top = node.scrollTop, left = node.scrollLeft;
        if (top || left) scroll.push({path: pathTo(root, node), top, left});
        if (node.tagName === 'DETAILS') {
            details.push({path: pathTo(root, node), open: node.open});
        }
    }
    const page = document.scrollingElement;
    return {
        scroll, details,
        page: {node: page, top: page.scrollTop, left: page.scrollLeft},
        focus: root.contains(active) ? {
            path: pathTo(root, active), start: active.selectionStart,
            end: active.selectionEnd, direction: active.selectionDirection,
        } : null,
    };
}

export function restoreViewState(root, snapshot) {
  for (const item of snapshot.details) {
    const node = find(root, item.path);
    if (node) node.open = item.open;
  }
  const focus = snapshot.focus, active = focus && find(root, focus.path);
  if (active) {
    active.focus({preventScroll:true});
    if (typeof focus.start === 'number' && active.setSelectionRange) active.setSelectionRange(focus.start, focus.end, focus.direction);
  }
  const positions = snapshot.scroll.map(item => ({...item, node:find(root,item.path)}));
  positions.push(snapshot.page);
  // Loading a changed rich value can temporarily shrink its scroll container.
  // Retry after hydration only if it is still mounted and the user has not
  // moved it since our last restoration. Never jump an active reader back.
  const restoreScroll = () => {
    for (const item of positions) {
      const {node} = item;
      if (!node?.isConnected || item.cancelled) continue;
      if (item.restored && (node.scrollTop !== item.restored.top || node.scrollLeft !== item.restored.left)) { item.cancelled = true; continue; }
      node.scrollTop = item.top; node.scrollLeft = item.left;
      item.restored = {top:node.scrollTop, left:node.scrollLeft};
    }
  };
  restoreScroll();
  return restoreScroll;
}

/**
 * Purpose: Apply a refresh after the active user interaction ends.
 * Input: The view root and the function that replaces its content.
 * Output: Functions that request or cancel a deferred refresh.
 * Note: Control focus alone does not indicate an open native picker.
 */
export function createRefreshRenderer(root, render) {
  let pending = false, pointer = false, dragging = false, composing = false, timer;
  const hasOpenSelector = CSS.supports('selector(:open)');
  const blocked = () => {
    const selection = document.getSelection();
    return pointer || dragging || composing || document.querySelector('dialog[open],.action-menu:popover-open,.chart-tooltip:popover-open') ||
      selection && !selection.isCollapsed && root.contains(selection.anchorNode);
  };
  const flush = () => {
    if (!pending) return;
    if (hasOpenSelector && root.querySelector('select:open,input:open')) {
      // Native popups can consume pointerup and close without a DOM event.
      // Check only the open popup until it closes, then apply the pending data.
      pointer = false;
      clearTimeout(timer);
      timer = setTimeout(flush, 100);
      return;
    }
    if (!blocked()) { pending = false; render(); }
  };
  const schedule = () => { if (pending) { clearTimeout(timer); timer = setTimeout(flush, 0); } };
  document.addEventListener('pointerdown', event => { if (root.contains(event.target)) pointer = true; }, true);
  for (const type of ['pointerup','pointercancel']) document.addEventListener(type, () => { pointer = false; schedule(); }, true);
  document.addEventListener('dragstart', event => { if (root.contains(event.target)) dragging = true; }, true);
  document.addEventListener('dragend', () => { dragging = false; pointer = false; schedule(); }, true);
  document.addEventListener('compositionstart', event => { if (root.contains(event.target)) composing = true; }, true);
  document.addEventListener('compositionend', () => { composing = false; schedule(); }, true);
  for (const type of ['change','cancel']) document.addEventListener(type, event => {
    // The picker can consume pointerup before any refresh becomes pending.
    if (root.contains(event.target) && event.target.matches('select,input[type="color"]')) pointer = false;
    schedule();
  }, true);
  for (const type of ['close','toggle','focusout','click','keydown','keyup','selectionchange']) document.addEventListener(type, schedule, true);
  window.addEventListener('blur', () => { pointer = false; dragging = false; composing = false; schedule(); });
  return {request:() => { pending = true; flush(); }, cancel:() => { pending = false; clearTimeout(timer); }};
}
