"""Deterministic input selection from a PortAudio device inventory."""

from numbers import Integral


class AudioDeviceSelector:
    """Resolve names or a configured default without accessing audio hardware."""

    def __init__(self, devices, default_input=None):
        self.devices = tuple(devices)
        self.default_input = default_input

    def select(self, name: str) -> int:
        """Return a unique input index or explain the available alternatives."""
        query = name.strip().casefold()
        if query == "default":
            index = self.default_input
            if isinstance(index, str) and index.strip().casefold() != "default":
                try:
                    return self.select(index)
                except RuntimeError as exc:
                    raise RuntimeError(f"Invalid default input: {exc}") from exc
            if (isinstance(index, bool) or not isinstance(index, Integral)
                    or not 0 <= index < len(self.devices)):
                raise self._error("No valid default input device is configured")
            return self._require_input(int(index))
        if not query:
            raise self._error("The input device name must not be blank")
        exact = [index for index, device in enumerate(self.devices)
                 if device["name"].casefold() == query]
        matches = exact or [index for index, device in enumerate(self.devices)
                            if query in device["name"].casefold()
                            and device["max_input_channels"] > 0]
        if not matches:
            raise self._error(f"Input device {name!r} was not found")
        if len(matches) > 1:
            choices = "; ".join(self.describe(index) for index in matches)
            raise self._error(f"Input device {name!r} is ambiguous: {choices}")
        return self._require_input(matches[0])

    def describe(self, index: int) -> str:
        """Include the device index and input capability in a diagnostic."""
        device = self.devices[index]
        channels = device["max_input_channels"]
        return (f"{index}: {device['name']} ({channels} input "
                f"{'channel' if channels == 1 else 'channels'})")

    def _require_input(self, index):
        if self.devices[index]["max_input_channels"] < 1:
            raise self._error(f"Device {self.devices[index]['name']!r} has no input channels")
        return index

    def _error(self, reason):
        available = "; ".join(self.describe(index) for index, device in enumerate(self.devices)
                              if device["max_input_channels"] > 0) or "none"
        return RuntimeError(
            f"{reason}. Available input devices: {available}. "
            'Select one with --device "NAME" (or TRANSLATOR_DEVICE), or configure '
            'your system input and use --device default.'
        )
