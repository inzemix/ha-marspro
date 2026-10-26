"""When to wake an iHub Pro up after a power outage.

An iHub Pro restarts with ``outlet.masterOn = 0`` and, in that state, ignores
*every* actuator command — lights, blower, humidifier. Switching outlet 0 back
on (``setOutlet num=0 on=1``, the command the vendor app sends when the user
touches a switch) brings the device back to life. Writing ``masterOn = 1``
instead does **nothing**: ``masterOn`` is a read-only field, and the broker
acknowledges the write before the device discards it.

That wake used to be attempted only when **our own** MQTT client reconnected
(``on_mqtt_reconnect`` in ``__init__.py``). After a power outage both sides
restart, but we come back first: on 2026-10-10 Home Assistant logged in to the
broker at 12:42:57 and the iHub only reappeared at 12:43:29 — the command was
published into the void, never retried, and the grow cabinet stayed dark for
two hours until the user opened the vendor app.

This module holds the decision logic — when a device that reports ``masterOn=0``
should be woken, and when to stop trying. It imports nothing from Home
Assistant on purpose: the policy is exercised by doubles, outside the HA
container (see the ``hacs-dev-workflow`` skill).

Nothing here writes anything: the caller performs the publish.
"""

# An iHub pushes an unsolicited getDevSta every 2 to 7 seconds. A silence this
# long therefore means the device went away, not a slow link — and its next
# message marks its return. The value must stay BELOW the device's boot time
# (~50-60 s), otherwise a plain reboot would never be detected: measured on
# 2026-10-10, the iHub reported a boot transient at 12:43:29 and its real state
# (masterOn=0) at 12:44:29, i.e. a 60 s hole between two messages.
SILENCE_GAP = 45.0

# After *our* restart the device usually reappears minutes later, i.e. after the
# single attempt made at reconnection time. Within this window, the first
# message received from a device counts as "it has just come back".
STARTUP_GRACE = 600.0

# Delay before each further verification/attempt. The spacing matters: the iHub
# applies a command with a delay of up to ~1 minute, so a first negative check
# does NOT mean the command was lost — hence several spaced attempts rather than
# one.
#
# 2026-10-24: the 5-write / 4-minute schedule gave up while the device was still
# asleep (19:56→20:00), and the cabinet stayed dark until the user opened the
# vendor app 4 minutes later. The schedule now spans ~28 minutes (9 writes) so a
# slow or busy device is still retried long after the first failures.
RETRY_DELAYS = (20.0, 45.0, 90.0, 120.0, 180.0, 300.0, 300.0, 300.0, 300.0)


class WakeTracker:
    """Decides masterOn wake attempts for the iHubs of one config entry."""

    def __init__(self, silence_gap: float = SILENCE_GAP,
                 startup_grace: float = STARTUP_GRACE,
                 retry_delays=RETRY_DELAYS):
        self._silence_gap = silence_gap
        self._startup_grace = startup_grace
        self._retry_delays = tuple(retry_delays)
        self._started_at: float | None = None
        self._last_seen: dict[str, float] = {}
        self._attempts: dict[str, int] = {}

    def observe(self, serial: str, now: float, master_on: bool) -> bool:
        """Record a message from an iHub; return True to start a wake sequence.

        ``master_on`` is what the device itself reported. A device already awake
        is never woken (and an interrupted sequence is forgotten), so a
        deliberate power-off — made in the vendor app or from Home Assistant —
        is not fought as long as the device keeps talking to us.
        """
        if self._started_at is None:
            self._started_at = now
        previous = self._last_seen.get(serial)
        self._last_seen[serial] = now

        if master_on:
            self._attempts.pop(serial, None)
            return False
        if serial in self._attempts:
            return False  # a sequence is already running for this device

        just_appeared = (
            (previous is not None and now - previous >= self._silence_gap)
            or (previous is None and now - self._started_at <= self._startup_grace)
        )
        if not just_appeared:
            return False
        self._attempts[serial] = 1
        return True

    def next_delay(self, serial: str, master_on: bool) -> float | None:
        """Delay (seconds) before the next check, or None to stop trying."""
        if master_on:
            self._attempts.pop(serial, None)
            return None
        attempts = self._attempts.get(serial)
        if attempts is None:
            return None
        if attempts > len(self._retry_delays):
            self._attempts.pop(serial, None)
            return None
        delay = self._retry_delays[attempts - 1]
        self._attempts[serial] = attempts + 1
        return delay

    def attempt_count(self, serial: str) -> int:
        """Number of writes already made in the running sequence (0 = none)."""
        return self._attempts.get(serial, 0)

    def tracking(self, serial: str) -> bool:
        """True while a wake sequence is in progress for this device."""
        return serial in self._attempts
