"""The HID gamepad report descriptor, shared by the USB and Bluetooth transports.

Both transports send byte-identical reports; only the wrapping differs. USB
publishes this descriptor through a HID class descriptor on ep0, Bluetooth
publishes it inside its SDP record.

16 digital buttons, an 8-way hat for the d-pad, and two signed 8-bit axes that
mirror the hat. Five report bytes: buttons low, buttons high, hat, X, Y.

Sending the d-pad as both a hat and an axis pair is deliberate: hosts turn the
hat into a real d-pad and the axes into a left stick, so games that read only one
of the two still work.
"""

import struct

REPORT_DESC = bytes([
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x05,        # Usage (Game Pad)
    0xA1, 0x01,        # Collection (Application)
    0xA1, 0x00,        #   Collection (Physical)
    0x05, 0x09,        #     Usage Page (Button)
    0x19, 0x01,        #     Usage Minimum (Button 1)
    0x29, 0x10,        #     Usage Maximum (Button 16)
    0x15, 0x00,        #     Logical Minimum (0)
    0x25, 0x01,        #     Logical Maximum (1)
    0x75, 0x01,        #     Report Size (1)
    0x95, 0x10,        #     Report Count (16)
    0x81, 0x02,        #     Input (Data, Variable, Absolute)
    0x05, 0x01,        #     Usage Page (Generic Desktop)
    0x09, 0x39,        #     Usage (Hat Switch)
    0x15, 0x00,        #     Logical Minimum (0)
    0x25, 0x07,        #     Logical Maximum (7)
    0x75, 0x04,        #     Report Size (4)
    0x95, 0x01,        #     Report Count (1)
    0x81, 0x42,        #     Input (Data, Variable, Absolute, Null State)
    0x75, 0x04,        #     Report Size (4)
    0x95, 0x01,        #     Report Count (1)
    0x81, 0x03,        #     Input (Constant) - pad the hat out to a whole byte
    0x09, 0x30,        #     Usage (X)
    0x09, 0x31,        #     Usage (Y)
    0x15, 0x81,        #     Logical Minimum (-127)
    0x25, 0x7F,        #     Logical Maximum (127)
    0x75, 0x08,        #     Report Size (8)
    0x95, 0x02,        #     Report Count (2)
    0x81, 0x02,        #     Input (Data, Variable, Absolute)
    0xC0,              #   End Collection
    0xC0,              # End Collection
])

REPORT_LEN = 5
HAT_CENTER = 8  # outside the logical range, so the host reads it as "released"
IDLE_REPORT = bytes([0, 0, HAT_CENTER, 0, 0])

# USB HID class descriptor types
HID_DT_HID = 0x21
HID_DT_REPORT = 0x22

# The USB HID class descriptor that advertises REPORT_DESC.
HID_DESCRIPTOR = struct.pack(
    "<BBHBBBH",
    9, HID_DT_HID,
    0x0111,          # bcdHID 1.11
    0,               # bCountryCode
    1,               # bNumDescriptors
    HID_DT_REPORT,
    len(REPORT_DESC),
)
