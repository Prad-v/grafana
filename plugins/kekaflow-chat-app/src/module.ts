import { AppPlugin } from '@grafana/data';

import { App } from './App';
import { ConfigPage } from './components/ConfigPage';

export const plugin = new AppPlugin().setRootPage(App).addConfigPage({
  title: 'Settings',
  icon: 'cog',
  body: ConfigPage,
  id: 'settings',
});
