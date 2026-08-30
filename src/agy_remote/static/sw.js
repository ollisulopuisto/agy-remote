// Service Worker with Web Push Notifications for agy-remote PWA
const CACHE_NAME = 'agy-remote-v3';

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(clients.claim());
});

self.addEventListener('fetch', (event) => {
  if (event.request.url.includes('/ws') || event.request.url.includes('/api/')) {
    return;
  }
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});

// Handle incoming Web Push Notifications (Lock Screen / System Banners)
self.addEventListener('push', (event) => {
  let payload = { title: 'Agent Remote', body: 'New session event', data: {} };
  if (event.data) {
    try {
      payload = event.data.json();
    } catch (e) {
      payload.body = event.data.text();
    }
  }

  const options = {
    body: payload.body,
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/favicon-96x96.png',
    vibrate: [100, 50, 100],
    data: payload.data,
    actions: [
      { action: 'open', title: 'Open Session' },
    ],
  };

  event.waitUntil(
    self.registration.showNotification(payload.title, options)
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  const convId = data.conversation_id || data.session_id || '';
  const approvalId = data.approval_id || '';

  const params = [];
  if (convId) params.push(`session=${encodeURIComponent(convId)}`);
  if (approvalId) params.push(`focus=${encodeURIComponent(approvalId)}`);
  const targetUrl = params.length > 0 ? `/#${params.join('&')}` : '/';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ('focus' in client) {
          if (client.navigate && targetUrl !== '/') {
            client.navigate(targetUrl);
          } else {
            client.postMessage({ type: 'NAVIGATE', session: convId, focus: approvalId });
          }
          return client.focus();
        }
      }
      if (clients.openWindow) {
        return clients.openWindow(targetUrl);
      }
    })
  );
});
