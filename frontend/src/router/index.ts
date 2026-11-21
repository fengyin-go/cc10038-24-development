import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import FormatAdapterView from '@/views/FormatAdapterView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    {
      path: '/format_adapter',
      name: 'format_adapter',
      component: FormatAdapterView,
      props: { moduleKey: 'format_adapter' },
    },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
