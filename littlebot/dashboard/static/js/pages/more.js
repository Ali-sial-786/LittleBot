// "More" (phone): big tiles for the sections that don't fit in the tab bar.

import { NAV } from '../app.js';
import { h, icon, pageHead } from '../ui.js';

export default {
  mount(view) {
    view.append(
      pageHead('More', 'Everything else you can do with LittleBot.'),
      h('nav', { class: 'tiles', 'aria-label': 'More sections' },
        NAV.filter((n) => !n.tab).map((n) => h('a', { class: 'tile', href: `#/${n.id}` },
          h('span', { class: 'ico' }, icon(n.icon)), h('strong', { text: n.label }), n.blurb ? h('span', { text: n.blurb }) : null))));
    return {};
  },
};
