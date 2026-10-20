import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  { path: '/', name: 'chat', component: () => import('../pages/ChatPage.vue') },
  { path: '/chat', name: 'conversations', component: () => import('../pages/ChatPage.vue') },
  { path: '/chat/:id', name: 'conversation', component: () => import('../pages/ChatPage.vue'), props: true },
  { path: '/analysis/:id', name: 'report', component: () => import('../pages/ReportPage.vue') },
  { path: '/dashboard', name: 'dashboard', component: () => import('../pages/DashboardPage.vue') },
  { path: '/history', name: 'history', redirect: '/chat' },
  { path: '/about', name: 'about', component: () => import('../pages/AboutPage.vue') },
]

export default createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: (to, _from, savedPosition) => savedPosition ?? (to.hash ? { el: to.hash } : { top: 0 }),
})
