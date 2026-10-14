/**
 * Architecture: labelled controls -> delegated pointer/focus events -> one
 * top-layer tooltip; action buttons -> bounded top-layer menu -> callbacks.
 * Main callers: app boot and workspace navigation/section/panel controls.
 * Why this exists: native titles have inconsistent delay and no keyboard
 * behavior. Overlays must escape clipping without changing page layout.
 */
export function installTooltips(root = document) {
  const tip = document.createElement('div');
  tip.id = 'control-tooltip'; tip.className = 'control-tooltip';
  tip.setAttribute('role', 'tooltip'); tip.setAttribute('popover', 'manual');
  document.body.append(tip);
  let current = null, changing = false, focusFrame = 0;
  const hide = () => {
    if(changing) return;
    changing=true;
    const previous=current;current=null;
    previous?.removeAttribute('aria-describedby');
    try { if(tip.matches(':popover-open'))tip.hidePopover(); } finally { changing=false; }
  };
  const show = control => {
    if (changing || !control || control.disabled || control.closest('[role="menu"]')) return;
    if (current === control) return;
    hide(); current = control;
    // Native title and our tooltip must not compete. Text remains accessible.
    const label = control.dataset.tooltip || control.getAttribute('title') || control.getAttribute('aria-label');
    if (!label) { current = null; return; }
    control.dataset.tooltip = label; control.removeAttribute('title');
    tip.textContent = label; control.setAttribute('aria-describedby', tip.id);
    // Popover focus restoration can synchronously emit focus events. Ignore
    // those during the native transition instead of recursively showing it.
    changing=true;
    try { (control.closest('dialog') || document.body).append(tip);tip.showPopover(); } finally { changing=false; }
    const box = control.getBoundingClientRect(), size = tip.getBoundingClientRect();
    const rail = control.matches('.nav-item') && getComputedStyle(control.querySelector('.nav-label')).display === 'none';
    const x = rail ? box.right + 10 : box.left + (box.width - size.width) / 2;
    const y = rail ? box.top + (box.height - size.height) / 2 : box.bottom + 8;
    tip.style.left = `${Math.max(8, Math.min(x, innerWidth - size.width - 8))}px`;
    tip.style.top = `${Math.max(8, Math.min(y, innerHeight - size.height - 8))}px`;
  };
  const candidate = node => node.closest?.('[data-tooltip],.nav-item,.icon-btn[aria-label],.panel-drag[aria-label],.panel-resizer[aria-label]');
  root.addEventListener('pointerover', event => show(candidate(event.target)));
  root.addEventListener('pointerout', event => { if (current && !current.contains(event.relatedTarget)) hide(); });
  root.addEventListener('focusin', event => {
    if(changing) return;
    const control=candidate(event.target);cancelAnimationFrame(focusFrame);
    // Keyboard focus can scroll a control into view. Position after that scroll.
    focusFrame=requestAnimationFrame(()=>{if(control?.contains(document.activeElement))show(control);});
  });
  root.addEventListener('focusout', () => {cancelAnimationFrame(focusFrame);hide();});
  root.addEventListener('pointerdown', hide);
  root.addEventListener('keydown', event => { if (event.key === 'Escape') {cancelAnimationFrame(focusFrame);hide();} });
  window.addEventListener('resize', hide);
  document.addEventListener('scroll', hide, true);
  return hide;
}

/** Open a small action menu with focus movement and automatic dismissal.
 * Main callers: section/panel/workspace action buttons. Actions receive no DOM
 * strings; their callbacks use captured identifiers, never evaluated markup.
 */
export function openActionMenu(anchor, items, onError) {
  document.querySelector('.action-menu')?.remove();
  const menu = document.createElement('div');
  menu.className = 'action-menu'; menu.setAttribute('popover', 'auto'); menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', anchor.getAttribute('aria-label') || 'Actions');
  anchor.setAttribute('aria-expanded', 'true');
  const close = (focus = true) => {
    if (menu.matches(':popover-open')) menu.hidePopover();
    menu.remove(); anchor.setAttribute('aria-expanded', 'false');
    if (focus && anchor.isConnected) anchor.focus();
  };
  for (const item of items) {
    if (!item) { menu.append(document.createElement('hr')); continue; }
    const button = document.createElement('button');
    button.type = 'button'; button.setAttribute('role', 'menuitem');
    button.textContent = item.label; button.disabled = !!item.disabled;
    button.addEventListener('click', async () => { close(); try { await item.run(); } catch(error) { onError(error.message); } });
    menu.append(button);
  }
  (anchor.closest('dialog') || document.body).append(menu); menu.showPopover();
  const box = anchor.getBoundingClientRect(), size = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(box.right-size.width, innerWidth-size.width-8))}px`;
  menu.style.top = `${Math.max(8, Math.min(box.bottom+6, innerHeight-size.height-8))}px`;
  menu.querySelector('button:not(:disabled)')?.focus();
  menu.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); }
    if (event.key === 'Tab') { close(false); }
    const buttons = [...menu.querySelectorAll('button:not(:disabled)')];
    if (['ArrowDown','ArrowUp','Home','End'].includes(event.key)) {
      event.preventDefault(); const index = buttons.indexOf(document.activeElement);
      buttons[event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length-1 : (index+(event.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length]?.focus();
    }
  });
  menu.addEventListener('toggle', event => { if (event.newState === 'closed') { anchor.setAttribute('aria-expanded','false'); menu.remove(); } });
}
