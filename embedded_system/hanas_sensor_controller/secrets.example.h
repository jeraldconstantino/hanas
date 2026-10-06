#pragma once

// Primary Wi-Fi and backend URL.
#define WIFI_SSID "your-primary-wifi-name"
#define WIFI_PASSWORD "your-primary-wifi-password"

// Use one SERVER_URL. For local testing, use your backend IP on the active network.
// For Azure, use your App Service sensor endpoint.
#define SERVER_URL "http://<primary-network-backend-ip>:8080/api/sensor-data"
// #define SERVER_URL "https://<app-name>.azurewebsites.net/api/sensor-data"

// Optional backup Wi-Fi. Leave WIFI_BACKUP_SSID empty to disable fallback.
#define WIFI_BACKUP_SSID "your-backup-wifi-name"
#define WIFI_BACKUP_PASSWORD "your-backup-wifi-password"

// Leave empty for local testing without DEVICE_API_TOKEN.
// Set this to the same value as the backend/Azure DEVICE_API_TOKEN when enabled.
#define DEVICE_API_TOKEN ""
