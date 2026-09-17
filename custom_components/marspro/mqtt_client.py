"""MQTT client for Mars Pro cloud broker."""
import asyncio
import json
import logging
import ssl
import time
import paho.mqtt.client as mqtt

from .const import MQTT_HOST, MQTT_PORT, MQTT_TOPIC_UP, MQTT_TOPIC_DOWN, RECONNECT_BASE, RECONNECT_MAX

_LOGGER = logging.getLogger(__name__)

class MarsProMQTT:
    """Manages MQTT connection to Mars Pro cloud broker."""

    def __init__(self, hass, user: str, password: str, devices: list[dict], message_callback, on_reconnect=None):
        self.hass = hass
        self._user = user
        self._password = password
        self._devices = devices
        self._callback = message_callback
        self._on_reconnect_callback = on_reconnect
        self._client: mqtt.Client | None = None
        self._reconnect_delay = RECONNECT_BASE

    def _build_client(self) -> mqtt.Client:
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"ha-marspro-{int(time.time())}",
            protocol=mqtt.MQTTv311,
        )
        client.username_pw_set(self._user, self._password)
        # The Mars Pro broker uses a self-signed certificate, so verification is
        # disabled. We build the SSL context ourselves instead of using
        # client.tls_set(): paho's tls_set() calls load_default_certs() which
        # reads ~119 CA files from disk *inside the event loop*, and HA reports
        # it as a blocking call. Since nothing is verified here, loading the
        # system trust store would be pointless anyway.
        # (tls_set_context() sets _tls_insecure=True automatically when
        # verify_mode is CERT_NONE.)
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        client.tls_set_context(ssl_context)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        return client

    def _on_connect(self, client, userdata, flags, rc, props=None):
        if rc == 0:
            _LOGGER.info("Mars Pro MQTT connected")
            self._reconnect_delay = RECONNECT_BASE
            for dev in self._devices:
                topic = MQTT_TOPIC_UP.format(model=dev["model"], serial=dev["serial"])
                client.subscribe(topic, qos=0)
                _LOGGER.debug("Subscribed to %s", topic)
            # Notify HA that reconnection is complete (for state restore)
            if self._on_reconnect_callback:
                def _schedule_reconnect():
                    result = self._on_reconnect_callback()
                    if asyncio.iscoroutine(result):
                        self.hass.async_create_task(result)
                self.hass.loop.call_later(2, _schedule_reconnect)
        else:
            _LOGGER.error("MQTT connect failed: rc=%s", rc)

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except Exception:
            return
        self.hass.loop.call_soon_threadsafe(self._callback, msg.topic, data)

    def _on_disconnect(self, client, userdata, flags, rc, props=None):
        if rc != 0:
            _LOGGER.warning("MQTT disconnected (rc=%s). Reconnecting in %ss...", rc, self._reconnect_delay)
            self.hass.loop.call_later(self._reconnect_delay, self.reconnect)
            self._reconnect_delay = min(self._reconnect_delay * 2, RECONNECT_MAX)

    async def connect(self):
        """Connect to MQTT broker."""
        # Build the client in an executor so any blocking setup (SSL context,
        # socket options) stays off the event loop.
        self._client = await self.hass.async_add_executor_job(self._build_client)
        await self.hass.async_add_executor_job(
            self._client.connect, MQTT_HOST, MQTT_PORT, 30
        )
        self._client.loop_start()

    def reconnect(self):
        """Reconnect with backoff."""
        if self._client:
            try:
                self._client.reconnect()
            except Exception:
                _LOGGER.debug("Reconnect attempt failed, retrying...")

    def publish(self, serial: str, model: str, method: str, params: dict | None = None):
        """Publish a command to a device."""
        if not self._client:
            return
        topic = MQTT_TOPIC_DOWN.format(model=model, serial=serial)
        payload = {"method": method, "params": params or {}}
        self._client.publish(topic, json.dumps(payload), qos=1)

    def disconnect(self):
        """Disconnect and clean up."""
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None
