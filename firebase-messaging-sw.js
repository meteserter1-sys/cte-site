// CTE — push-alert service worker (Firebase Cloud Messaging).
// Registered by the News tab at scope /fcm/ so the app's own sw.js keeps its scope untouched.
// Place this file next to index.html in public/.
importScripts('https://www.gstatic.com/firebasejs/10.13.0/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/10.13.0/firebase-messaging-compat.js');

firebase.initializeApp({
  apiKey: "AIzaSyDd4ldLMPWLdy2AigeeEU5dtNnmvQh2NQk",
  authDomain: "commodity-trading-engine.firebaseapp.com",
  projectId: "commodity-trading-engine",
  storageBucket: "commodity-trading-engine.firebasestorage.app",
  messagingSenderId: "684017312104",
  appId: "1:684017312104:web:d64852f01affff58fef6dd"
});

const messaging = firebase.messaging();

// Background messages: show a notification; tapping it opens the article (or the app).
messaging.onBackgroundMessage((payload) => {
  const n = payload.notification || {};
  const d = payload.data || {};
  self.registration.showNotification(n.title || 'CTE market alert', {
    body: n.body || '',
    icon: '/icon-192.png',
    tag: d.id || undefined,
    data: { url: d.url || 'https://commodity-trading-engine.web.app/' }
  });
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || 'https://commodity-trading-engine.web.app/';
  event.waitUntil(clients.openWindow(url));
});
