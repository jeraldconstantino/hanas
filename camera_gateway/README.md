# HANAS Camera Gateway

This folder contains the local video gateway configuration for connecting a
TP-Link Tapo C310 camera to HANAS.

The Tapo camera exposes an RTSP stream, but browsers cannot play RTSP directly.
`go2rtc` converts the local RTSP feed into browser-friendly WebRTC/HLS streams
that can later be embedded in the HANAS frontend.

## Architecture

```text
Tapo C310 camera  ->  go2rtc gateway  ->  HANAS frontend
RTSP              ->  WebRTC/HLS      ->  browser view
```

The gateway must run on the same local network as the camera. For development,
it can run on a laptop or desktop. For production, an always-on Raspberry Pi or
mini PC near the hydroponic system is recommended.

## Requirements

- TP-Link Tapo C310 camera
- Local camera username and password created in the Tapo app
- Docker installed on the gateway machine
- Camera and gateway machine connected to the same LAN/Wi-Fi

## Configuration

Create a local config file:

```bash
cp camera_gateway/go2rtc.example.yaml camera_gateway/go2rtc.yaml
```

Update `camera_gateway/go2rtc.yaml` with the camera credentials and IP address:

```text
TAPO_USERNAME
TAPO_PASSWORD
TAPO_CAMERA_IP
```

Example stream URL format:

```text
rtsp://username:password@192.168.1.50:554/stream1
```

## Running Locally

Start the gateway from the repository root:

```bash
docker run --rm --name hanas-go2rtc --network host \
  -v "$PWD/camera_gateway/go2rtc.yaml:/config/go2rtc.yaml:ro" \
  alexxit/go2rtc
```

Open the go2rtc dashboard:

```text
http://localhost:1984/
```

Open the main camera stream:

```text
http://localhost:1984/stream.html?src=lettuce_cam
```

Open the lower-bandwidth stream:

```text
http://localhost:1984/stream.html?src=lettuce_cam_low
```

## Raspberry Pi Production Layout

For production, run go2rtc as the single direct client of the Tapo camera. The
24-hour recorder and HANAS live view then consume the local go2rtc streams.

```text
Tapo C310 stream1  ->  go2rtc  ->  ffmpeg recorder  ->  HDD segments
                               ->  HANAS live view  ->  browser over Tailscale
```

This avoids opening multiple direct RTSP sessions to the camera and keeps the
recording pipeline independent from frontend viewers.

### 1. Install Docker On The Pi

Install Docker:

```bash
curl -fsSL https://get.docker.com | sh
```

Allow the current user to run Docker:

```bash
sudo usermod -aG docker "$USER"
```

Log out and log back in, then confirm Docker works:

```bash
docker --version
docker compose version
```

### 2. Create The Gateway Folder

Create the Pi gateway folder:

```bash
sudo mkdir -p /opt/hanas-camera
sudo chown "$USER:$USER" /opt/hanas-camera
```

### 3. Copy The Gateway Files

From this repository, copy the example files to the Pi gateway folder:

```bash
cp camera_gateway/go2rtc.example.yaml /opt/hanas-camera/go2rtc.yaml
cp camera_gateway/docker-compose.example.yml /opt/hanas-camera/docker-compose.yml
```

Edit `/opt/hanas-camera/go2rtc.yaml` with the Tapo camera credentials and IP.

Example:

```yaml
streams:
  lettuce_cam:
    - rtsp://camera-user:camera-password@192.168.50.50:554/stream1
  lettuce_cam_low:
    - rtsp://camera-user:camera-password@192.168.50.50:554/stream2
```

### 4. Start go2rtc

Start go2rtc:

```bash
cd /opt/hanas-camera
docker compose up -d
```

Check logs:

```bash
docker compose logs -f
```

Open the Pi stream from another machine on the same network:

```text
http://<pi-lan-ip>:1984/stream.html?src=lettuce_cam
```

### 5. Install Tailscale On The Pi

Install Tailscale:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Approve the Pi in the browser login flow, then get the Pi Tailscale IP:

```bash
tailscale ip -4
```

The remote stream URL will be:

```text
http://<pi-tailscale-ip>:1984/stream.html?src=lettuce_cam
```

Install and connect Tailscale on every phone or laptop that needs to view the
camera in the HANAS frontend. Azure can serve the dashboard without Tailscale;
the viewer's browser still needs Tailscale because it loads the Pi stream
directly.

### 6. Point The Recorder At go2rtc

The recorder should read from go2rtc instead of the Tapo camera directly:

```text
rtsp://127.0.0.1:8554/lettuce_cam
```

An example recorder script is available at:

```text
camera_gateway/record_24h_from_go2rtc.example.sh
```

Copy the example, then preserve your existing cron job path:

```bash
cp camera_gateway/record_24h_from_go2rtc.example.sh /path/to/your/current/record_24h.sh
chmod +x /path/to/your/current/record_24h.sh
```

If your existing cron job already calls `/path/to/your/current/record_24h.sh`,
no cron change is needed. Restart the recorder after go2rtc is running.

### 7. Health Checks

Check go2rtc:

```bash
docker ps
docker logs hanas-go2rtc --tail 100
```

Check the recorder:

```bash
tail -f /media/pi/hdd/$(date +%F)/system.log
```

Check Pi health:

```bash
htop
df -h
vcgencmd measure_temp
```

### 8. HANAS Frontend URL

The HANAS frontend can use:

```text
http://<pi-ip>:1984/stream.html?src=lettuce_cam
```

or, through Tailscale:

```text
http://<pi-tailscale-ip>:1984/stream.html?src=lettuce_cam
```

## Security

Keep `go2rtc.yaml` local because it contains the camera RTSP credentials. Do
not commit it to Git and do not expose the raw RTSP stream to the public
internet.

For the current Azure frontend setup, use Tailscale between the viewer device
and Raspberry Pi. Do not expose port `1984` through router port forwarding.

## Remote Access With Tailscale

Tailscale can provide private remote access to the camera gateway without
opening router ports or exposing the RTSP stream publicly.

Recommended production layout:

```text
Tapo C310 camera  ->  Raspberry Pi go2rtc gateway  ->  Tailscale private IP  ->  HANAS camera page
```

For local testing, install Tailscale on the same machine running go2rtc:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

The login command opens a browser approval flow. After approval, confirm the
machine is connected:

```bash
tailscale status
```

Get the machine's private Tailscale IP:

```bash
tailscale ip -4
```

If the command returns `100.95.12.34`, the camera stream can be reached from
other approved devices in the same Tailnet at:

```text
http://100.95.12.34:1984/stream.html?src=lettuce_cam
```

When moving to Raspberry Pi, install Tailscale on the Pi, run go2rtc there, and
use the Pi's Tailscale IP in the HANAS camera configuration.

In HANAS, save the stream URL in Settings -> Camera, or set it at frontend build
time:

```bash
VITE_CAMERA_STREAM_URL=https://<private-camera-gateway>/stream.html?src=lettuce_cam
```

The direct `http://<pi-tailscale-ip>:1984/...` URL works when the HANAS page is
also served over HTTP. An Azure-hosted HTTPS dashboard cannot embed an HTTP
stream because browsers block active mixed content. Put go2rtc behind a private
HTTPS-enabled gateway or reverse proxy for embedded production viewing; the
direct HTTP address can still be opened in a separate browser tab.

The Camera page is intentionally operator-facing. Technical stream configuration
belongs in Settings.
